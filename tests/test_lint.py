"""Unit checks for the two in-house guards in tools/lint.py — the empty-except
guard and the procedure-return-value guard — plus its file discovery. These run
inside the normal pytest suite, so a change that breaks a guard fails here rather
than silently letting a real defect through the `python3 tools/lint.py` gate.

lint.py lives in tools/ (not on the package path) and imports nothing from the
project, so it is loaded by path here rather than as a `yt_shorts` module.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("lintmod", ROOT / "tools" / "lint.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


# --- empty-except guard ---------------------------------------------------
# Fixtures are SOURCE STRINGS (not live code) so this test file stays clean.
SWALLOW = "try:\n    f()\nexcept OSError:\n    pass\n"
SWALLOW_ELLIPSIS = "try:\n    f()\nexcept OSError:\n    ...\n"
COMMENT_INLINE = "try:\n    f()\nexcept OSError:\n    pass  # already gone\n"
COMMENT_LINE = "try:\n    f()\nexcept OSError:\n    # already gone\n    pass\n"
NON_EMPTY = "try:\n    f()\nexcept OSError:\n    log()\n"
RAISE_IN_TRY = "try:\n    f()\n    raise AssertionError\nexcept ValueError:\n    pass\n"
BENIGN_IMPORT = "try:\n    import x\nexcept ImportError:\n    pass\n"
# KeyboardInterrupt is NOT a blanket-benign type: a silent Ctrl-C swallow in a
# handler whose try can also exit normally is a real bug, so it must carry a reason.
KBD_NO_COMMENT = "try:\n    loop()\nexcept KeyboardInterrupt:\n    pass\n"
KBD_COMMENT = "try:\n    loop()\nexcept KeyboardInterrupt:\n    pass  # Ctrl-C\n"
BARE_EXCEPT = "try:\n    f()\nexcept:\n    pass\n"
MIXED_BENIGN = "try:\n    f()\nexcept (ImportError, OSError):\n    pass\n"
DOTTED = "try:\n    f()\nexcept asyncio.TimeoutError:\n    pass\n"


def test_flags_uncommented_swallow():
    assert lint.find_empty_excepts(SWALLOW) == [3]          # the `except` line


def test_flags_uncommented_ellipsis_body():
    assert lint.find_empty_excepts(SWALLOW_ELLIPSIS) == [3]


def test_inline_comment_suppresses():
    assert lint.find_empty_excepts(COMMENT_INLINE) == []


def test_comment_line_in_body_suppresses():
    assert lint.find_empty_excepts(COMMENT_LINE) == []


def test_non_empty_handler_not_flagged():
    assert lint.find_empty_excepts(NON_EMPTY) == []


def test_raise_in_try_is_excluded():
    # assert-raises test idiom — a deliberate raise in the try is not a swallow.
    assert lint.find_empty_excepts(RAISE_IN_TRY) == []


def test_benign_caught_types_excluded():
    assert lint.find_empty_excepts(BENIGN_IMPORT) == []      # optional-import guard


def test_keyboardinterrupt_swallow_needs_comment():
    assert lint.find_empty_excepts(KBD_NO_COMMENT) == [3]
    assert lint.find_empty_excepts(KBD_COMMENT) == []


def test_bare_except_is_flagged():
    assert lint.find_empty_excepts(BARE_EXCEPT) == [3]


def test_mixed_benign_and_real_is_flagged():
    # catches a real error type alongside a benign one -> still a silent swallow
    assert lint.find_empty_excepts(MIXED_BENIGN) == [3]


def test_dotted_exception_name_uses_attr():
    assert lint.find_empty_excepts(DOTTED) == [3]            # asyncio.TimeoutError, no comment


def test_syntax_error_source_is_safe():
    assert lint.find_empty_excepts("def (:\n  pass\n") == []


def test_empty_except_repo_is_clean():
    # The whole repo must already satisfy the guard; every silent swallow carries
    # an explanatory comment. This is the regression that keeps it that way.
    assert lint.check_empty_excepts(ROOT) == [], lint.check_empty_excepts(ROOT)


# --- procedure-return-value-used guard ------------------------------------
# A procedure (returns only None) whose result is USED.
PROC_USED_RETURN = "def p():\n    print(1)\n\ndef c():\n    return p()\n"
PROC_USED_ASSIGN = "def p():\n    print(1)\n\ndef c():\n    x = p()\n    return x\n"
PROC_BARE_RETURN = "def p():\n    if a:\n        return\n    print(1)\n\nx = p()\n"
# Standalone call (result discarded) — fine.
PROC_STANDALONE = "def p():\n    print(1)\n\ndef c():\n    p()\n"
# `return None` is a deliberate value -> NOT a procedure.
RETURNS_NONE = "def p():\n    return None\n\ndef c():\n    return p()\n"
# Returns a real value -> not a procedure.
RETURNS_VALUE = "def p():\n    return 5\n\ndef c():\n    x = p()\n"
# Always raises / exits -> never returns None -> not a procedure.
ALWAYS_RAISES = "def p():\n    raise SystemExit(1)\n\ndef c():\n    return p()\n"
ALWAYS_EXITS = "import sys\ndef p():\n    sys.exit(1)\n\ndef c():\n    return p()\n"
# A generator is not a procedure.
GENERATOR = "def p():\n    yield 1\n\ndef c():\n    x = p()\n"


def test_proc_return_flags_used_return():
    assert lint.find_proc_return_value_uses(PROC_USED_RETURN) == [(5, "p")]


def test_proc_return_flags_used_assignment():
    assert lint.find_proc_return_value_uses(PROC_USED_ASSIGN) == [(5, "p")]


def test_proc_return_flags_bare_return_procedure():
    assert lint.find_proc_return_value_uses(PROC_BARE_RETURN) == [(6, "p")]


def test_proc_return_standalone_call_ok():
    assert lint.find_proc_return_value_uses(PROC_STANDALONE) == []


def test_proc_return_explicit_none_not_a_procedure():
    assert lint.find_proc_return_value_uses(RETURNS_NONE) == []


def test_proc_return_value_returner_not_a_procedure():
    assert lint.find_proc_return_value_uses(RETURNS_VALUE) == []


def test_proc_return_always_raising_not_a_procedure():
    assert lint.find_proc_return_value_uses(ALWAYS_RAISES) == []
    assert lint.find_proc_return_value_uses(ALWAYS_EXITS) == []


def test_proc_return_generator_not_a_procedure():
    assert lint.find_proc_return_value_uses(GENERATOR) == []


def test_proc_return_syntax_error_source_is_safe():
    assert lint.find_proc_return_value_uses("def (:\n  pass\n") == []


def test_proc_return_repo_is_clean():
    assert lint.check_proc_return_value_uses(ROOT) == [], lint.check_proc_return_value_uses(ROOT)


# --- absolute-home-path guard ---------------------------------------------
# Every sample path below is ASSEMBLED at runtime rather than written out as a
# literal. A literal would be a real hardcoded home path sitting in a tracked
# file, so this module would fail its own repo-is-clean test — the guard cannot
# be tested with the very thing it forbids.
_U = "/Users/"
_H = "/home/"
# Windows needs the same treatment, and needs it twice: a raw `C:\Users\` and
# the `C:\\Users\\` a Python string literal carries are two distinct spellings
# the guard must catch, and writing either one out here would trip it.
_W = "C:" + "\\" + "Users" + "\\"
_W2 = "C:" + "\\\\" + "Users" + "\\\\"


def test_home_path_flags_a_mac_home():
    assert lint.find_absolute_home_paths(f"x = '{_U}alice/proj'") == [(1, f"{_U}alice")]


def test_home_path_flags_a_linux_home():
    assert lint.find_absolute_home_paths(f"cd {_H}bob/src") == [(1, f"{_H}bob")]


def test_home_path_flags_a_windows_home():
    assert lint.find_absolute_home_paths(_W + "bob") == [(1, _W + "bob")]


def test_home_path_flags_an_escaped_windows_home():
    assert lint.find_absolute_home_paths(_W2 + "bob") == [(1, _W2 + "bob")]


def test_home_path_flags_a_shebang():
    # The exact shape bin/yt-shorts used to carry.
    hit = lint.find_absolute_home_paths(f"#!{_U}jane/repo/.venv/bin/python")
    assert hit == [(1, f"{_U}jane")]


def test_home_path_reports_every_line():
    text = f"a {_U}alice/x\nb\nc {_H}bob/y\n"
    assert lint.find_absolute_home_paths(text) == [(1, f"{_U}alice"), (3, f"{_H}bob")]


def test_home_path_ignores_a_url():
    # The lookbehind's job: a URL path is not a filesystem home.
    assert lint.find_absolute_home_paths("https://example.org/home/alice") == []


def test_home_path_ignores_the_bare_word():
    # Prose about the directory itself, with no user component, is not a path.
    assert lint.find_absolute_home_paths(f"the {_U} tree on macOS") == []
    assert lint.find_absolute_home_paths("a/home/b relative path") == []


def test_home_path_ignores_a_tilde():
    # `~` is the supported way to say the same thing, so it must stay clean.
    assert lint.find_absolute_home_paths("~/YT-Shorts-Data") == []


def test_home_path_repo_is_clean():
    # The gate itself: no tracked file may carry a hardcoded home directory.
    # This repository is public, and such a path works on exactly one machine.
    assert lint.check_absolute_home_paths(ROOT) == [], lint.check_absolute_home_paths(ROOT)


# --- file discovery -------------------------------------------------------

def test_tracked_files_include_non_python():
    # The home-path guard reads every tracked file, not just Python ones —
    # the paths it exists to keep out lived in JSON and Markdown too.
    files = lint._tracked_files(ROOT)
    assert any(f.endswith("README.md") for f in files)
    assert any(f.endswith(".json") for f in files)


def test_discovers_extensionless_shebang_cli():
    # bin/yt-shorts is Python with no .py suffix; the guards and ruff must still
    # see it (it is the CLI). Discovery finds it via its python shebang.
    files = lint._python_files(ROOT)
    assert any(f.endswith("bin/yt-shorts") for f in files)


def test_discovers_only_python():
    # No non-Python file (README.md, ruff.toml, ...) sneaks into the file list.
    files = lint._python_files(ROOT)
    assert all(f.endswith(".py") or lint._has_python_shebang(f) for f in files)
