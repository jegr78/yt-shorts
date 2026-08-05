#!/usr/bin/env python3
"""Run the ruff linter over the repo (config: ruff.toml at the repo root), plus two
small in-house AST guards for defect classes ruff has no rule for.

    python3 tools/lint.py          # check only
    python3 tools/lint.py --fix    # auto-fix what ruff can (e.g. unused imports)

Ruff is a single external binary, NOT vendored — install once:
  macOS:  brew install ruff      Windows:  winget install astral-sh.ruff
  Linux:  pipx/pip install ruff (or the distro package)
This needs no PYTHONPATH and no virtualenv: ruff and the guards read source
statically, they never import the project.

The two guards below (both pure, unit-tested in tests/test_lint.py):

  empty-except — a handler whose whole body is `pass`/`...` with NO explanatory
  comment silently swallows the error. This is a bug most of the time and a
  deliberate idiom occasionally, so the guard flags the silent case while
  EXEMPTING the idioms that are genuinely fine: an explanatory comment in the
  handler, a deliberate `raise` somewhere in the try body (the assert-raises
  test shape), and handlers that catch only benign control/optional-import
  signals (ImportError/ModuleNotFoundError/SystemExit/StopIteration/
  GeneratorExit). A comment-free swallow fails the gate; adding one word of
  why clears it.

  procedure-return-value-used — `x = proc()` or `return proc()` where the callee
  `proc` is a same-file function that only ever returns None (a 'procedure').
  Using its result is always a mistake (the value is None). Scoped to same-file,
  bare-name calls — the recurring `return helper(args)` shape — so it stays
  false-positive-free; cross-module and method calls are out of scope.
"""
import ast, io, os, shutil, subprocess, sys, tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Caught types for which a silent `pass` is an accepted idiom (optional imports
# and interpreter/iterator-control signals). KeyboardInterrupt is deliberately
# NOT here: a comment-free Ctrl-C swallow in a handler whose try can also exit
# normally is a real bug, so it must carry a reason like any other swallow.
_BENIGN_EXC = frozenset({"ImportError", "ModuleNotFoundError",
                         "SystemExit", "StopIteration", "GeneratorExit"})


def _comment_lines(source):
    """1-based line numbers carrying a `#` comment (best-effort)."""
    lines = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass  # tokenizer gave up; the ast.parse below is the authority anyway
    return lines


def _caught_names(handler):
    """The simple names of the exception types a handler catches (empty = bare)."""
    typ = handler.type
    if typ is None:
        return set()
    items = typ.elts if isinstance(typ, ast.Tuple) else [typ]
    out = set()
    for it in items:
        if isinstance(it, ast.Name):
            out.add(it.id)
        elif isinstance(it, ast.Attribute):
            out.add(it.attr)
    return out


def find_empty_excepts(source):
    """Line numbers of `except` handlers that silently swallow — body is only
    `pass`/`...` with no comment in the handler — EXCLUDING the accepted idioms: a
    deliberate `raise` anywhere in the try body (assert-raises tests) and handlers
    catching only benign types (ImportError/SystemExit/...). Returns [] for
    unparseable source. Pure -> unit-tested in tests/test_lint.py."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    comments = _comment_lines(source)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        try_raises = any(isinstance(n, ast.Raise)
                         for s in node.body for n in ast.walk(s))
        for h in node.handlers:
            if len(h.body) != 1:
                continue
            stmt = h.body[0]
            empty = isinstance(stmt, ast.Pass) or (
                isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is Ellipsis)
            if not empty:
                continue
            end = getattr(stmt, "end_lineno", stmt.lineno)
            if any(h.lineno <= c <= end for c in comments):
                continue                       # has an explanatory comment
            if try_raises:
                continue                       # deliberate raise in try (assert-raises idiom)
            names = _caught_names(h)
            if names and names <= _BENIGN_EXC:
                continue                       # optional-import / control-signal handler
            hits.append(h.lineno)
    return hits


def _scope_returns_yields(fn):
    """The Return nodes and whether a yield appears in *fn*'s OWN scope (a nested
    def/lambda owns its own returns, so we stop at those)."""
    rets, has_yield, stack = [], False, list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue                       # nested scope — not ours
        if isinstance(n, ast.Return):
            rets.append(n)
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            has_yield = True
        stack.extend(ast.iter_child_nodes(n))
    return rets, has_yield


def _always_terminates(stmts):
    """True iff this statement block can NOT fall through its end — it always
    raises, returns, or sys.exit()s. Bounded (matches the trailing-raise / both-
    branches-raise shapes); anything more exotic conservatively counts as falling
    through. Used only for the no-`return` case below."""
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, (ast.Raise, ast.Return)):
        return True
    if isinstance(last, ast.Expr) and isinstance(last.value, ast.Call):
        f = last.value.func                # sys.exit(...) / exit(...)
        if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) == "exit":
            return True
    if isinstance(last, ast.If) and last.body and last.orelse:
        return _always_terminates(last.body) and _always_terminates(last.orelse)
    return False


def _is_procedure(fn):
    """True iff *fn* is a 'procedure': it only ever returns None *implicitly* — via
    a bare `return` or by falling off the end. A `return <expr>` (INCLUDING an
    explicit `return None`, treated as a deliberate value) or a yield disqualifies
    it; a function that always raises/exits is not a procedure either (it never
    returns None)."""
    rets, has_yield = _scope_returns_yields(fn)
    if has_yield:
        return False
    if any(r.value is not None for r in rets):
        return False                       # returns a value (incl. `return None`)
    if any(r.value is None for r in rets):
        return True                        # has a bare `return` -> implicit-None exit
    return not _always_terminates(fn.body)  # no returns -> procedure iff it can fall through


def find_proc_return_value_uses(source):
    """(lineno, name) for every call whose result is USED but whose callee is a
    same-file procedure (returns only None). Same-file, bare-name calls only (no
    cross-module / method resolution), which is the recurring `return helper(args)`
    / `x = helper()` shape; this keeps it precise (no false positives). A call is
    'used' unless it is a statement on its own (`helper()` as an expression
    statement). Returns [] for unparseable source. Pure -> unit-tested."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    procs = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_procedure(n)}
    standalone = {id(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)}
    hits = [(n.lineno, n.func.id) for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in procs and id(n) not in standalone]
    return sorted(hits)


def _python_files(root):
    """Tracked Python files: every `*.py`, PLUS extensionless git-tracked files
    whose first line is a python shebang (this is how `bin/yt-shorts`, the CLI,
    gets linted — it has no `.py` suffix). A plain walk is the fallback when git
    is unavailable."""
    try:
        out = subprocess.check_output(["git", "-C", root, "ls-files"], text=True)
        tracked = [p for p in out.splitlines() if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        skip = {".git", "__pycache__", ".venv", ".pytest_cache", "node_modules"}
        tracked = []
        for dirpath, dirnames, names in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip]
            rel = os.path.relpath(dirpath, root)
            tracked += [os.path.join(rel, n) for n in names]
    files = []
    for rel in tracked:
        path = os.path.join(root, rel)
        if rel.endswith(".py"):
            files.append(path)
        elif "." not in os.path.basename(rel) and _has_python_shebang(path):
            files.append(path)
    return files


def _has_python_shebang(path):
    """True iff the file's first line is a `#!...python` shebang."""
    try:
        with open(path, encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return False
    return first.startswith("#!") and "python" in first


def check_empty_excepts(root):
    """(relpath, lineno) for every uncommented silent-swallow except in the repo."""
    bad = []
    for path in _python_files(root):
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except OSError:
            continue
        for ln in find_empty_excepts(src):
            bad.append((os.path.relpath(path, root), ln))
    return sorted(bad)


def check_proc_return_value_uses(root):
    """(relpath, lineno, name) for every use of a same-file procedure's return
    value in the repo."""
    bad = []
    for path in _python_files(root):
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except OSError:
            continue
        for lineno, name in find_proc_return_value_uses(src):
            bad.append((os.path.relpath(path, root), lineno, name))
    return sorted(bad)


def main():
    if not shutil.which("ruff"):
        sys.exit("lint: ruff not found on PATH.\n"
                 "  install: brew install ruff  (macOS) | winget install astral-sh.ruff"
                 "  (Windows) | pipx install ruff  (Linux)")
    files = _python_files(ROOT)
    rc = subprocess.call(["ruff", "check", *files, *sys.argv[1:]])
    bad = check_empty_excepts(ROOT)
    if bad:
        print("\nempty-except — add a short reason in the handler body, "
              "e.g. `pass  # already gone`:")
        for relpath, lineno in bad:
            print(f"  {relpath}:{lineno}: except body is only pass/... with no comment")
        rc = rc or 1
    proc_bad = check_proc_return_value_uses(ROOT)
    if proc_bad:
        print("\nprocedure-return-value-used — this callee only ever returns None; "
              "drop the assignment / `return`, or give the callee an explicit "
              "`return <value>`:")
        for relpath, lineno, name in proc_bad:
            print(f"  {relpath}:{lineno}: result of {name}() is used but it returns None")
        rc = rc or 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
