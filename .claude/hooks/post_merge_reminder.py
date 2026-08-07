#!/usr/bin/env python3
"""PostToolUse (Bash) hook: after a real `gh pr merge`, inject the post-merge
repository security follow-up reminder.

Self-gates on the Bash command string. The settings.json `if` field
(`"if": "Bash(gh pr merge*)"`) is not honored in every Claude Code build - when
it is ignored the reminder fires on *every* Bash call (curl, ls, git ...), which
is noise and, worse, injects a "a merge just completed" instruction when nothing
was merged. Deciding here on `.tool_input.command` is version-independent.

The match is anchored to a COMMAND POSITION rather than being a substring
search: a plain `"gh pr merge" in cmd` fires for any command that merely quotes
the phrase, which was measured - this hook's own test payload, an `echo` of a
JSON string, injected "a merge just completed" with nothing merged. That is the
same false trigger the paragraph above rejects the `if` field for.

Reads the PostToolUse JSON payload on stdin; on a match, prints the
additionalContext stdout JSON that Claude Code injects into the transcript.
Emits nothing (exit 0) for any other command.
"""
import json
import re
import sys

# Start of string, or after a shell separator - never inside a quoted string.
MERGE_RE = re.compile(r"(?:^|[;&|(]\s*|\n\s*)gh\s+pr\s+merge\b")

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
    if not MERGE_RE.search(cmd):
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
