"""What must stay true of every GitHub Actions workflow in this repository.

Workflows are the one thing here that cannot be tested by running them until
the repository exists, so these pin the invariants a later edit could quietly
break. They are deliberately textual: parsing the YAML would let a malformed
file pass as "no jobs found", which is the vacuous-assertion trap this project
has already been bitten by three times.
"""

import re
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# `uses: owner/repo@<40 hex>` with an optional trailing `# v1.2.3` comment.
# re.MULTILINE: this is also `.search()`-ed against a whole multi-workflow
# blob (see test_at_least_one_action_is_actually_used below), where `^`/`$`
# must anchor to each LINE rather than the start/end of the entire blob -
# without it that search can never match, no matter what the file contains.
# Per-line `.match()` on an already-split line (the primary pinning check) is
# unaffected either way, since each line is already its own string.
PINNED = re.compile(r"^\s*-?\s*uses:\s+\S+@[0-9a-f]{40}\s*(#.*)?$", re.MULTILINE)
USES = re.compile(r"^\s*-?\s*uses:\s+")


def _workflows():
    return sorted(WORKFLOW_DIR.glob("*.yml"))


class TestThereAreWorkflowsAtAll:
    """Every assertion below iterates over the workflow files. An empty glob
    would make all of them pass while testing nothing - so this one comes
    first and fails loudly instead."""

    def test_the_workflow_directory_exists(self):
        assert WORKFLOW_DIR.is_dir(), f"no {WORKFLOW_DIR}"

    def test_ci_is_one_of_them(self):
        assert (WORKFLOW_DIR / "ci.yml").is_file()


class TestEveryActionIsPinnedToASha:
    """A tag or branch ref is mutable: whoever controls it can change what runs
    with this repository's token. Dependabot updates the SHAs; this keeps them
    SHAs."""

    def test_no_uses_line_carries_a_tag_or_branch(self):
        offenders = []
        for path in _workflows():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if USES.match(line) and not PINNED.match(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert not offenders, "unpinned action refs:\n  " + "\n  ".join(offenders)

    def test_at_least_one_action_is_actually_used(self):
        """Guards the guard: a workflow set with no `uses:` at all would pass
        the test above without exercising it."""
        text = "\n".join(p.read_text(encoding="utf-8") for p in _workflows())
        assert PINNED.search(text)


class TestNothingStillNamesMaster:
    """The default branch is `main`. A workflow left pointing at `master`
    simply never triggers - silently."""

    def test_no_workflow_mentions_master(self):
        offenders = [f"{p.name}" for p in _workflows()
                     if "master" in p.read_text(encoding="utf-8")]
        assert not offenders, f"workflows still naming master: {offenders}"


class TestTheJobNamesTheRulesetDependsOn:
    """`main`'s ruleset requires these checks by name. A renamed job does not
    fail a PR - it deadlocks it, waiting forever for a check that can no
    longer report. Renaming a job here means updating the ruleset too."""

    REQUIRED = ("lint:", "frontend:", "test:", "binary-smoke:")

    @pytest.mark.parametrize("job", REQUIRED)
    def test_ci_defines_the_job(self, job):
        text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
        assert f"\n  {job}" in text, f"ci.yml no longer defines {job!r}"


class TestTheTestMatrixStaysFiveLegs:
    """Three Python versions on ubuntu, the ship version alone on macOS and
    Windows. `exclude` (not `include`) is what keeps the job names shaped
    `test (<os>, <version>)`, which is what the ruleset matches on."""

    def test_the_matrix_excludes_four_combinations(self):
        text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
        assert text.count("- {os: macos-latest") + text.count("- {os: windows-latest") == 4

    def test_the_ship_version_is_3_13(self):
        text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
        assert 'python-version: "3.13"' in text or "'3.13'" in text


class TestTheEndToEndGuard:
    """tests/test_studio_e2e.py skips itself when Chromium is absent. That is
    what makes the matrix cheap, and it is also how a green run could hide 124
    missing tests. ci.yml must fail when they skip on the one runner that
    installs the browser."""

    def test_ci_greps_the_summary_for_skips_and_exits_nonzero(self):
        # Deliberately specific: asserting merely that the word "skipped"
        # appears would pass on the explanatory COMMENT alone, with the guard
        # itself deleted. Pin the mechanism, not a word.
        text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
        assert "grep -qE '[0-9]+ skipped'" in text, \
            "ci.yml no longer greps the E2E summary for skips"
        assert "set -o pipefail" in text, \
            "without pipefail the piped pytest exit code is lost and a failing E2E run reads as green"
