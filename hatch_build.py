"""Builds the studio frontend into studio/static/ before a wheel or sdist.

An existing build is reused as is - CI builds it once and hands it over as an
artifact. Missing npm with nothing to reuse is fatal: a wheel without the
studio installs cleanly and then 404s every page.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

ROOT = Path(__file__).parent
WEB = ROOT / "src" / "yt_shorts" / "studio" / "web"
STATIC = ROOT / "src" / "yt_shorts" / "studio" / "static"
TIMEOUT_SECONDS = 900


def built_before_its_sources():
    """True when studio/static/ predates web/src/, i.e. the build looks stale.

    What used to police this was a CI step that rebuilt the frontend and diffed
    the result against the committed copy. Nothing does that any more, so a
    developer who edits web/src/ and forgets to rebuild would package the
    PREVIOUS bundle without a word. An mtime comparison cannot prove a build is
    current - it is not that gate coming back - but it turns "without a word"
    into a line in the build output. False when there is nothing to compare: an
    sdist carries static/ without web/, and that is the normal case for the
    wheel built from it.
    """
    built, sources = STATIC / "index.html", WEB / "src"
    if not built.is_file() or not sources.is_dir():
        return False
    built_at = built.stat().st_mtime
    return any(p.stat().st_mtime > built_at for p in sources.rglob("*") if p.is_file())


class FrontendBuildHook(BuildHookInterface):
    PLUGIN_NAME = "frontend"

    def initialize(self, version, build_data):
        if (STATIC / "index.html").is_file():
            self.app.display_info(f"frontend: using the existing build in {STATIC}")
            if built_before_its_sources():
                self.app.display_warning(
                    f"frontend: {STATIC} is older than {WEB / 'src'} - packaging the "
                    "previous bundle. Run `npm ci && npm run build` there to refresh it."
                )
            return
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError(
                "npm is required to build the studio frontend and was not found.\n"
                f"  install Node, or run `npm ci && npm run build` in {WEB}\n"
                "  and build again - an existing studio/static/ is reused as is."
            )
        for command in (["ci"], ["run", "build"]):
            self.app.display_info(f"frontend: npm {' '.join(command)}")
            subprocess.run([npm, *command], cwd=WEB, check=True, timeout=TIMEOUT_SECONDS)
        if not (STATIC / "index.html").is_file():
            raise RuntimeError(f"frontend: npm run build produced no {STATIC / 'index.html'}")
