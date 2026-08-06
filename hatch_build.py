"""Builds the studio frontend into studio/static/ before a wheel or sdist.

studio/static/ is Vite's output and is NOT committed. It used to be, so that a
clone worked without Node - but the delivery path for operators is a binary or
a GitHub release, not a checkout, and `pip install -e .` is a developer action.
A developer already needs ruff, ffmpeg and playwright; npm joins that list
rather than becoming a burden on anyone using the tool.

Two things follow, and both are load-bearing:

  Skipped when studio/static/ already holds a build. CI builds the frontend
  once in its own job and hands it to the others as an artifact, so a wheel
  built there must not spend a minute rebuilding what it was given.

  Fails loudly when npm is missing and there is nothing to fall back on. A
  wheel without the studio would install cleanly and then 404 every page.
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


class FrontendBuildHook(BuildHookInterface):
    PLUGIN_NAME = "frontend"

    def initialize(self, version, build_data):
        if (STATIC / "index.html").is_file():
            self.app.display_info(f"frontend: using the existing build in {STATIC}")
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
