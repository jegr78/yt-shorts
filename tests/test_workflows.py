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
import yaml

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


def _matrix_legs(ci_path):
    """Parse the `test` job's matrix into the actual (os, python-version) leg
    set: the os x python-version cross product minus `exclude`. Asserts the
    shape it depends on BEFORE computing anything, so a malformed or
    restructured matrix fails loudly instead of silently yielding an empty -
    and therefore vacuously non-matching, but for the wrong reason - set.
    """
    doc = yaml.safe_load(Path(ci_path).read_text(encoding="utf-8"))
    matrix = doc["jobs"]["test"]["strategy"]["matrix"]
    assert "os" in matrix and "python-version" in matrix, (
        "the test job's matrix lost its os/python-version keys"
    )
    oses = matrix["os"]
    versions = matrix["python-version"]
    assert oses and versions, "the test job's matrix has an empty os or python-version list"
    excluded = {(entry["os"], entry["python-version"]) for entry in matrix.get("exclude", [])}
    legs = {(os_, version) for os_ in oses for version in versions} - excluded
    assert legs, "matrix parse yielded no legs at all"
    return legs


class TestTheTestMatrixStaysFiveLegs:
    """Three Python versions on ubuntu, the ship version alone on macOS and
    Windows. `exclude` (not `include`) is what keeps the job names shaped
    `test (<os>, <version>)`, which is what the ruleset matches on.

    This PARSES the YAML rather than grepping for fragments, because two
    textual checks that did exactly that could not fail on the mutation they
    exist to catch: counting `- {os: macos-latest` / `- {os: windows-latest`
    occurrences never inspects WHICH python-version each entry excludes, and a
    bare substring search for `python-version: "3.13"` is satisfied
    unconditionally by the `lint` and `binary-smoke` jobs alone - it is true
    no matter what the `test` job's matrix says. Measured: a `ci.yml` with
    3.13 excluded on macOS/Windows instead of 3.12/3.14 - the ship version
    left untested on either non-Linux runner, exactly the defect this class
    exists to catch - passed both of the old assertions.
    """

    EXPECTED_LEGS = {
        ("ubuntu-latest", "3.12"), ("ubuntu-latest", "3.13"), ("ubuntu-latest", "3.14"),
        ("macos-latest", "3.13"), ("windows-latest", "3.13"),
    }

    def test_the_matrix_is_exactly_these_five_legs(self):
        assert _matrix_legs(WORKFLOW_DIR / "ci.yml") == self.EXPECTED_LEGS


class TestTheEndToEndGuard:
    """tests/test_studio_e2e.py skips itself when Chromium is absent. That is
    what makes the matrix cheap, and it is also how a green run could hide 124
    missing tests. ci.yml must fail when they skip on the one runner that
    installs the browser.

    Finds the guard by its STEP NAME rather than searching the whole file:
    two fragments appearing anywhere in ci.yml would still pass if a refactor
    moved one of them into an unrelated step, or split the guard's own step so
    the pipefail and the grep no longer share a shell. Requiring both inside
    the SAME step's `run:` is what actually proves the wiring, not just that
    both strings still exist somewhere in the document.
    """

    STEP_NAME = "The E2E suite must actually run, not skip"

    def _guard_step(self):
        doc = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
        steps = doc["jobs"]["test"]["steps"]
        matches = [step for step in steps if step.get("name") == self.STEP_NAME]
        assert len(matches) == 1, (
            f"expected exactly one {self.STEP_NAME!r} step in the test job, found {len(matches)}"
        )
        return matches[0]

    def test_ci_greps_the_summary_for_skips_and_exits_nonzero(self):
        # Deliberately specific: asserting merely that the word "skipped"
        # appears would pass on the explanatory COMMENT alone, with the guard
        # itself deleted. Pin the mechanism, not a word - and pin it to THIS
        # step's own run block, not the file at large.
        run = self._guard_step().get("run", "")
        assert "grep -qE '[0-9]+ skipped'" in run, (
            "the E2E guard step no longer greps its own summary for skips"
        )
        assert "set -o pipefail" in run, (
            "without pipefail in the SAME step, the piped pytest exit code is "
            "lost and a failing E2E run reads as green"
        )
