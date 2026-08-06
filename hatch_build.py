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
