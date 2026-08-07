#!/usr/bin/env python3
"""PostToolUse (Bash) hook: after a real `gh pr merge`, inject the post-merge
repository security follow-up reminder.

Self-gates on the Bash command string. The settings.json `if` field
(`"if": "Bash(gh pr merge*)"`) is not honored in every Claude Code build - when
it is ignored the reminder fires on *every* Bash call (curl, ls, git ...), which
is noise and, worse, injects a "a merge just completed" instruction when nothing
was merged. Deciding here on `.tool_input.command` is version-independent.

Two things stand between the phrase and the reminder, and both were measured
rather than feared - each is the same false trigger the paragraph above rejects
the `if` field for, one level further down.

A plain `"gh pr merge" in cmd` fires for any command that merely QUOTES the
phrase: this hook's own test payload, an `echo` of a JSON string, injected "a
merge just completed" with nothing merged. So the match is anchored to a
COMMAND POSITION.

That is not enough on its own, because a heredoc body is PROSE inside a command
string and shell syntax does not apply to it - writing this fix fired the hook
twice in two minutes, off a commit message and a PR body that quoted
`cd x && gh pr merge` as an example. Heredoc bodies are therefore dropped
before matching. Over-dropping only costs a reminder; matching prose invents a
merge.

Reads the PostToolUse JSON payload on stdin; on a match, prints the
additionalContext stdout JSON that Claude Code injects into the transcript.
Emits nothing (exit 0) for any other command.
"""
import json
import re
import sys

# Start of string, or after a shell separator - never inside a quoted string.
MERGE_RE = re.compile(r"(?:^|[;&|(]\s*|\n\s*)gh\s+pr\s+merge\b")
# `<<WORD`, `<<'WORD'`, `<<"WORD"`, `<<-WORD`. A digit-led token is excluded so
# an arithmetic left-shift (`1 << 3`) is not read as opening a heredoc.
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def without_heredoc_bodies(command: str) -> str:
    """The command with every heredoc BODY removed, its command lines kept."""
    lines = command.split("\n")
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        # Several heredocs may open on one line; their bodies follow in order.
        for match in HEREDOC_RE.finditer(line):
            marker = match.group(2)
            while i < len(lines) and lines[i].strip() != marker:
                i += 1
            i += 1        # the terminator line itself
    return "\n".join(kept)

REMINDER = (
    "A merge to main just completed. Before treating the task as done, do the "
    "repository security follow-up: (1) Wait for the CodeQL run this merge "
    "triggered on main to finish (poll: gh run list --workflow=CodeQL "
    "--branch=main --limit=3, then gh run watch the newest id). (2) List OPEN "
    "Code Scanning alerts: gh api repos/:owner/:repo/code-scanning/alerts "
    "--paginate, and keep the ones whose state is open. (3) For each open "
    "alert, judge whether it is a real issue or a false positive. Fix the REAL "
    "ones on a NEW branch and open a PR (run PYTHONPATH=src .venv/bin/pytest "
    "-q and python3 tools/lint.py first); never commit to main directly. "
    "Record any false positives in the PR body instead of auto-dismissing "
    "them. If there are zero open alerts, report that briefly and stop."
)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        # A missing/malformed payload - nothing to react to.
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    # Only fire for an actual PR merge, not every Bash call.
    if not MERGE_RE.search(without_heredoc_bodies(cmd)):
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": REMINDER,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
