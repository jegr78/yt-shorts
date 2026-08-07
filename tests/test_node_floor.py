"""The frontend's Node requirement, pinned from both sides.

Since static/ stopped being committed, building the frontend is a prerequisite
for this Python suite too - so a Node version the dependencies reject is no
longer a frontend-only problem. jsdom raised that floor once already (29 -> 30,
in #7) with nothing in the repository noticing.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

WEB = Path(__file__).resolve().parent.parent / "src" / "yt_shorts" / "studio" / "web"
WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

_CLAUSE = re.compile(r"^(\^|~|>=|>|=)?\s*v?(\d+(?:\.\d+){0,2})$")


def _parts(version: str) -> tuple[int, int, int]:
    numbers = [int(n) for n in version.split(".")]
    return tuple(numbers + [0] * (3 - len(numbers)))


def _bounds(clause: str) -> tuple[tuple, tuple | None]:
    """One comparator as [low, high) - high None meaning unbounded. Raises on
    anything it does not understand, so an unparsed range fails the test rather
    than passing it."""
    match = _CLAUSE.match(clause.strip())
    if not match:
        raise ValueError(f"unsupported node engines clause: {clause!r}")
    operator, version = match.group(1), match.group(2)
    low = _parts(version)
    given = len(version.split("."))
    if operator == ">=":
        return low, None
    if operator == ">":
        return (low[0], low[1], low[2] + 1), None
    if operator == "^":
        return low, (low[0] + 1, 0, 0)
    if operator == "~" or (operator in (None, "=") and given == 2):
        return low, (low[0], low[1] + 1, 0)
    if operator in (None, "=") and given == 1:
        return low, (low[0] + 1, 0, 0)
    return low, (low[0], low[1], low[2] + 1)


def satisfies(version: str, spec: str) -> bool:
    wanted = _parts(version)
    for clause in spec.split("||"):
        low, high = _bounds(clause)
        if low <= wanted and (high is None or wanted < high):
            return True
    return False


def _declared() -> str:
    return json.loads((WEB / "package.json").read_text(encoding="utf-8"))["engines"]["node"]


def _lowest_supported() -> list[str]:
    """The lowest Node version each clause of our own range admits - the exact
    versions every dependency has to keep accepting."""
    return [".".join(str(n) for n in _bounds(clause)[0])
            for clause in _declared().split("||")]


class TestTheRangeParser:
    """Guards the guard: satisfies() decides both tests below, so a parser that
    said True to everything would make them vacuous."""

    @pytest.mark.parametrize("version,spec,expected", [
        ("26.4.0", "^22.22.2 || ^24.15.0 || >=26.0.0", True),
        ("25.0.0", "^22.22.2 || ^24.15.0 || >=26.0.0", False),
        ("22.19.0", "^22.22.2 || ^24.15.0 || >=26.0.0", False),
        ("24.14.0", "^24.15.0", False),
        ("22.22.2", ">=22.19.0", True),
        ("26.0.0", "20 || >=22", True),
        ("21.0.0", "20 || >=22", False),
        ("12.22.7", ">=v12.22.7", True),
    ])
    def test_it_decides_known_cases(self, version, spec, expected):
        assert satisfies(version, spec) is expected

    def test_an_unparseable_clause_raises_rather_than_passing(self):
        with pytest.raises(ValueError):
            satisfies("26.4.0", "not-a-version")


class TestTheDeclaredFloor:
    def test_package_json_declares_one(self):
        assert _declared(), "src/yt_shorts/studio/web/package.json has no engines.node"

    def test_engine_strict_makes_it_binding(self):
        # Without engine-strict npm only warns, which is the failure mode the
        # declaration exists to prevent.
        npmrc = (WEB / ".npmrc").read_text(encoding="utf-8")
        assert "engine-strict=true" in npmrc

    def test_no_dependency_rejects_a_version_we_claim_to_support(self):
        lock = json.loads((WEB / "package-lock.json").read_text(encoding="utf-8"))
        offenders = []
        for name, package in lock["packages"].items():
            spec = (package.get("engines") or {}).get("node")
            if not name or not spec:
                continue
            for version in _lowest_supported():
                if not satisfies(version, spec):
                    offenders.append(f"{name.removeprefix('node_modules/')} needs "
                                     f"{spec!r}, which rejects Node {version}")
        assert not offenders, (
            "engines.node in package.json is looser than its own dependencies:\n  "
            + "\n  ".join(sorted(set(offenders))))


class TestEveryWorkflowPinsASupportedNode:
    """A workflow pinning a Node the dependencies reject would fail every job
    that installs them, with engine-strict on."""

    def _pinned(self):
        found = []
        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in (doc.get("jobs") or {}).values():
                for step in job.get("steps") or []:
                    version = (step.get("with") or {}).get("node-version")
                    if version:
                        found.append((path.name, str(version)))
        return found

    def test_at_least_one_workflow_pins_node(self):
        assert self._pinned(), "no workflow pins a node-version any more"

    def test_each_pinned_version_satisfies_package_json(self):
        spec = _declared()
        offenders = [f"{name}: {version} does not satisfy {spec!r}"
                     for name, version in self._pinned()
                     if not satisfies(version, spec)]
        assert not offenders, "\n  ".join(offenders)
