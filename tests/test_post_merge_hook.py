"""Which command strings `.claude/hooks/post_merge_reminder.py` counts as a
merge.

The hook injects "a merge to main just completed" into the transcript, so a
false positive sends a session off to do a security follow-up for a merge that
never happened. Both of the ways it got that wrong are pinned below, because
both were reached by ordinary work rather than by an exotic input.
"""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "post_merge_reminder.py"
_spec = importlib.util.spec_from_file_location("post_merge_reminder", HOOK)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def fires(command, monkeypatch):
    """Run main() against a payload carrying `command`; True if it emitted."""
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(json.dumps({"tool_input": {"command": command}})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert hook.main() == 0
    return "hookSpecificOutput" in out.getvalue()


class TestARealMergeFires:
    @pytest.mark.parametrize("command", [
        "gh pr merge 19 --squash --delete-branch",
        "cd /repo && gh pr merge 19 --squash",
        "set -e; gh pr merge 19",
        "echo starting\ngh pr merge 19",
    ])
    def test_it_fires(self, command, monkeypatch):
        assert fires(command, monkeypatch)


class TestAQuotedPhraseDoesNot:
    """The first measured false positive: this hook's own test payload, an
    `echo` of a JSON string, claimed a merge had happened."""

    @pytest.mark.parametrize("command", [
        'echo "{\\"command\\":\\"gh pr merge 19\\"}" | cat',
        'grep -rn "gh pr merge" .claude/',
        "ls -la",
        "git merge main",
    ])
    def test_it_stays_quiet(self, command, monkeypatch):
        assert not fires(command, monkeypatch)


class TestAHeredocBodyDoesNot:
    """The second one, and the reason anchoring to a shell separator was not
    enough: a heredoc body is PROSE inside the command string. Writing the
    commit message and PR body for the first fix - both of which quoted the
    compound form as an example - fired the hook twice in two minutes."""

    def test_a_commit_message_quoting_the_phrase_stays_quiet(self, monkeypatch):
        assert not fires("git commit -F - <<'MSG'\n"
                         "See `cd x && gh pr merge`.\nMSG", monkeypatch)

    def test_a_pr_body_quoting_the_phrase_stays_quiet(self, monkeypatch):
        assert not fires('gh pr create --body-file - <<"BODY"\n'
                         "set -e; gh pr merge 1\nBODY", monkeypatch)

    def test_only_the_body_goes_not_the_rest_of_the_command(self, monkeypatch):
        # The over-dropping guard. Without it the fix is a silent off switch,
        # which costs the security follow-up rather than a spurious one.
        assert fires("cat <<'EOF'\nnothing to see\nEOF\ngh pr merge 19",
                     monkeypatch)

    def test_an_arithmetic_shift_does_not_open_a_heredoc(self, monkeypatch):
        assert fires("python3 -c 'print(1 << 3)'\ngh pr merge 19", monkeypatch)


class TestAPayloadItCannotRead:
    def test_malformed_json_is_survived_silently(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        assert hook.main() == 0
        assert out.getvalue() == ""

    def test_an_empty_payload_is_not_a_merge(self, monkeypatch):
        assert not fires("", monkeypatch)
