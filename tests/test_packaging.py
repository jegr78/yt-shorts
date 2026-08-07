"""What the built artefacts must and must not contain.

These tests BUILD the package (hatchling, into a temp dir) rather than reading
pyproject.toml and trusting it. The include/exclude patterns are the part that
is easy to get subtly wrong, and a pattern that silently matches nothing looks
identical to one that works - right up until an sdist ships a node_modules tree.
"""

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The wheel and sdist, built once for this module. Skips rather than fails
    when `build` is not installed - packaging is not every contributor's venv."""
    pytest.importorskip("build", reason="pip install build")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run([sys.executable, "-m", "build", "--outdir", str(out), str(ROOT)],
                   check=True, capture_output=True)
    wheels = list(out.glob("*.whl"))
    sdists = list(out.glob("*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1, f"unexpected artefacts: {list(out.iterdir())}"
    return wheels[0], sdists[0]


def _wheel_names(wheel):
    with zipfile.ZipFile(wheel) as zf:
        return zf.namelist()


def _sdist_names(sdist):
    with tarfile.open(sdist) as tf:
        return tf.getnames()


class TestTheStudioFrontendShips:
    """studio/static/ is the BUILT frontend - git-ignored, and produced by
    hatch_build.py on the way into the wheel so nobody installing it needs
    npm. That is what these tests check. If it misses the wheel, the studio
    serves a blank page and every assertion downstream dies of 'element(s) not
    found' - with no error anywhere."""

    def test_the_wheel_carries_the_spa_entry_point(self, built):
        wheel, _ = built
        assert "yt_shorts/studio/static/index.html" in _wheel_names(wheel)

    def test_the_wheel_carries_the_static_assets(self, built):
        wheel, _ = built
        assets = [n for n in _wheel_names(wheel)
                  if n.startswith("yt_shorts/studio/static/assets/")]
        assert assets, "no built frontend assets in the wheel"


class TestTheFrontendSourceDoesNot:
    """src/yt_shorts/studio/web/ sits INSIDE the package directory and holds
    node_modules. Without an explicit exclude it rides along in every artefact."""

    def test_no_web_sources_in_the_wheel(self, built):
        wheel, _ = built
        assert not [n for n in _wheel_names(wheel) if "studio/web/" in n]

    def test_no_web_sources_in_the_sdist(self, built):
        _, sdist = built
        assert not [n for n in _sdist_names(sdist) if "studio/web/" in n]

    def test_no_node_modules_anywhere(self, built):
        # This one does NOT pin the `exclude` pattern above: node_modules/ is
        # itself gitignored, and hatchling's default file selection already
        # drops gitignored paths, so this assertion holds even with the
        # exclude line deleted entirely. The two tests above it are what
        # actually pin the pattern (studio/web/ has plenty of non-gitignored
        # source files that only the explicit exclude keeps out); this one
        # pins the belt-and-braces OUTCOME - that node_modules never ships,
        # by whichever mechanism keeps it out.
        wheel, sdist = built
        assert not [n for n in _wheel_names(wheel) + _sdist_names(sdist)
                    if "node_modules" in n]


class TestTheConsoleScript:
    def test_the_wheel_declares_the_yt_shorts_entry_point(self, built):
        wheel, _ = built
        with zipfile.ZipFile(wheel) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".dist-info/entry_points.txt"))
            text = zf.read(name).decode("utf-8")
        assert "yt-shorts = yt_shorts.cli:main" in text


class TestTheVersionIsNotDeclaredTwice:
    def test_the_wheel_version_matches_the_declared_version(self, built):
        wheel, _ = built
        from yt_shorts.__version__ import __version__ as declared

        assert f"-{declared}-" in wheel.name or wheel.name.startswith(f"yt_shorts-{declared}")


class TestTheLicenseShips:
    def test_a_license_file_exists_in_the_repository(self):
        assert (ROOT / "LICENSE").is_file()

    def test_the_wheel_carries_it(self, built):
        wheel, _ = built
        assert [n for n in _wheel_names(wheel) if n.endswith("LICENSE")]
