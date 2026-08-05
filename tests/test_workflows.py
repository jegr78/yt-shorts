"""What must stay true of every GitHub Actions workflow in this repository.

Workflows are the one thing here that cannot be tested by running them until
the repository exists, so these pin the invariants a later edit could quietly
break. They are deliberately textual: parsing the YAML would let a malformed
file pass as "no jobs found", which is the vacuous-assertion trap this project
has already been bitten by three times.
"""

import itertools
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


def _pr_check_names():
    """Every status-check name a pull request can produce, across ALL workflows.

    This is what the ruleset's required-check list must equal. Computed rather
    than listed, so it tracks the workflows: a matrix job yields one name per
    leg, a `name:` interpolating `matrix.<key>` yields one per value, and a
    workflow that does not trigger on a pull request yields none (release.yml
    runs on tags — a check that can never report on a PR would deadlock every
    PR forever if it were ever required).
    """
    names = set()
    for path in _workflows():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        # `on:` parses as the boolean True in YAML 1.1 — hence the `.get(True)`.
        triggers = doc.get(True, doc.get("on"))
        triggers = set(triggers) if isinstance(triggers, (dict, list)) else {triggers}
        if not triggers & {"pull_request", "pull_request_target"}:
            continue
        jobs = doc.get("jobs") or {}
        assert jobs, f"{path.name} triggers on pull requests but defines no jobs"
        for job_id, job in jobs.items():
            matrix = (job.get("strategy") or {}).get("matrix")
            name = job.get("name")
            if name and "matrix." in str(name) and matrix:
                key = re.search(r"matrix\.(\w+)", name).group(1)
                names |= {re.sub(r"\$\{\{.*?\}\}", value, name) for value in matrix[key]}
            elif matrix and not name:
                keys = [k for k in matrix if k not in ("include", "exclude")]
                # strict=True: product() is taken over exactly `keys`, so every
                # combo has that length — a mismatch would mean the matrix was
                # restructured underneath us, and should raise rather than
                # silently drop a leg (and with it a required check).
                legs = [dict(zip(keys, combo, strict=True))
                        for combo in itertools.product(*(matrix[k] for k in keys))]
                for excluded in matrix.get("exclude", []):
                    legs = [leg for leg in legs
                            if not all(leg.get(k) == v for k, v in excluded.items())]
                names |= {f"{job_id} ({', '.join(str(leg[k]) for k in keys)})"
                          for leg in legs}
            else:
                names.add(name or job_id)
    return names


class TestEveryPullRequestCheckIsInTheRuleset:
    """`main`'s ruleset requires TWELVE checks, and the list is a contract with
    these workflows in both directions.

    A renamed job deadlocks every PR — the ruleset waits forever for a check
    that can no longer report. A NEW pull-request job that nobody adds to the
    ruleset is the opposite failure and the quieter one: it can go red without
    blocking a merge.

    Both halves were got wrong once. The plan's first draft required only
    `ci.yml`'s eight jobs, leaving `gitleaks`, both CodeQL legs and
    `Validate PR title` unenforced — and the last of those is the one that
    matters most, since a free-text PR title merges fine and then produces no
    release at all, silently.
    """

    EXPECTED = {
        "lint",
        "frontend",
        "test (ubuntu-latest, 3.12)",
        "test (ubuntu-latest, 3.13)",
        "test (ubuntu-latest, 3.14)",
        "test (macos-latest, 3.13)",
        "test (windows-latest, 3.13)",
        "binary-smoke",
        "gitleaks",
        "Analyze (python)",
        "Analyze (javascript-typescript)",
        "Validate PR title",
    }

    def test_the_check_set_is_exactly_what_the_ruleset_requires(self):
        actual = _pr_check_names()
        assert actual == self.EXPECTED, (
            f"the pull-request check set changed.\n"
            f"  no longer produced (ruleset would deadlock): "
            f"{sorted(self.EXPECTED - actual)}\n"
            f"  newly produced (unenforced unless added):    "
            f"{sorted(actual - self.EXPECTED)}\n"
            f"Update the ruleset in docs/superpowers/plans/"
            f"2026-08-05-ci-release-and-public-readiness.md AND on GitHub."
        )

    def test_release_workflows_contribute_no_checks(self):
        # release.yml and release-please.yml do not run on pull requests, so
        # none of their job names may appear. Requiring one would block every
        # PR on a check that never reports.
        assert not {"create-release", "wheel", "release-please"} & _pr_check_names()


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
