"""`yt-shorts install-tools` - install and update the external runtime tools
(ffmpeg, yt-dlp) through the platform's own package manager: winget on Windows,
brew on macOS, apt or pacman on Linux.

Ported from the racecast broadcast project's src/scripts/install_tools.py and
installer_common.py, reduced to this project's two installable packages. Pure
decision helpers live at the top and are unit-tested without a package manager
anywhere near them; main() (added later in this module) performs the installs.

It never elevates privileges itself: winget and brew prompt for what they need,
and apt/pacman get an explicit `sudo` prefix because - unlike those two - they
need root and will not ask.

STDLIB ONLY, and no FastAPI: this is reachable from the CLI, which runs in a
venv that may never have installed it (see CLAUDE.md's list).
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import atomicwrite, ownermode

# Three names are checked for presence; only two are installable. ffprobe has no
# package of its own anywhere - it ships with ffmpeg - so a missing ffprobe is
# reported as a missing ffmpeg install rather than as its own problem.
TOOLS = ("ffmpeg", "ffprobe", "yt-dlp")
PACKAGES = ("ffmpeg", "yt-dlp")

WINGET_IDS = {"ffmpeg": "Gyan.FFmpeg", "yt-dlp": "yt-dlp.yt-dlp"}
BREW_FORMULAE = {"ffmpeg": "ffmpeg", "yt-dlp": "yt-dlp"}
# apt gets ffmpeg only. yt-dlp on Linux is a pinned, checksum-verified direct
# download instead (see install_ytdlp_binary): apt's package lags upstream far
# enough that it no longer passes YouTube's bot check - and YouTube is where
# every clip in this project comes from.
APT_PACKAGES = {"ffmpeg": "ffmpeg"}
# Arch is the opposite case: both tools are current in the official `extra`
# repo, so pacman does all the work and no managed install applies.
PACMAN_PACKAGES = {"ffmpeg": "ffmpeg", "yt-dlp": "yt-dlp"}

# Standard Homebrew locations: Apple Silicon, then Intel. A fresh bootstrap is
# NOT on the current process's PATH (shellenv only runs in new shells), so brew
# must be invoked through the absolute path find_brew() returns.
BREW_PATHS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
BREW_INSTALLER = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"

# winget result codes that mean "the package is already there" - not failures:
# 0x8A15002B UPDATE_NOT_APPLICABLE (installed, no newer version in the source),
# 0x8A150061 PACKAGE_ALREADY_INSTALLED. subprocess reports them as unsigned
# DWORDs; PowerShell shows them signed - normalise through the 32-bit mask.
WINGET_ALREADY_INSTALLED = (0x8A15002B, 0x8A150061)


def confirmed(answer: str) -> bool:
    """True iff the operator's reply means yes."""
    return answer.strip().lower().startswith("y")


def install_exit_ok(manager: str, code: int) -> bool:
    """True iff this exit code means the package is (already) installed."""
    if code == 0:
        return True
    return manager == "winget" and (code & 0xFFFFFFFF) in WINGET_ALREADY_INSTALLED


def find_brew(which=shutil.which, exists=os.path.exists) -> str | None:
    """Absolute brew invocation path, or None. PATH first, then the standard
    install locations - which covers both a fresh bootstrap and an
    unconfigured PATH."""
    hit = which("brew")
    if hit:
        return hit
    for path in BREW_PATHS:
        if exists(path):
            return path
    return None


def pick_manager(platform: str, which=shutil.which) -> str | None:
    """The package manager for this platform, or None (-> the manual guide).
    On Linux apt wins when both are present, so a Debian box that happens to
    carry a pacman port keeps the path it has always used."""
    if platform.startswith("win"):
        return "winget" if which("winget") else None
    if platform == "darwin":
        return "brew" if which("brew") else None
    if which("apt-get"):
        return "apt"
    return "pacman" if which("pacman") else None


def missing_tools(which=shutil.which) -> list[str]:
    """The tools that are not resolvable, in TOOLS order."""
    return [t for t in TOOLS if not which(t)]


def install_commands(manager, packages, brew_path="brew", sudo=False) -> list[list[str]]:
    """The argv list(s) to install `packages` with `manager`. `sudo` prepends
    sudo to apt/pacman (Linux, non-root) - they need root and, unlike winget
    and brew, do not prompt for it."""
    packages = list(packages)
    if manager == "winget":
        return [["winget", "install", "--id", WINGET_IDS[p], "-e",
                 "--accept-source-agreements", "--accept-package-agreements"]
                for p in packages if p in WINGET_IDS]
    if manager == "brew":
        formulae = [BREW_FORMULAE[p] for p in packages if p in BREW_FORMULAE]
        return [[brew_path, "install"] + formulae] if formulae else []
    if manager == "apt":
        pkgs = [APT_PACKAGES[p] for p in packages if p in APT_PACKAGES]
        if not pkgs:
            return []
        pre = ["sudo"] if sudo else []
        # `apt-get update` first - a fresh or stale index (a just-created cloud
        # VM, say) cannot locate the packages otherwise.
        return [pre + ["apt-get", "update"],
                pre + ["apt-get", "install", "-y"] + pkgs]
    if manager == "pacman":
        pkgs = [PACMAN_PACKAGES[p] for p in packages if p in PACMAN_PACKAGES]
        if not pkgs:
            return []
        pre = ["sudo"] if sudo else []
        # Deliberately NOT `-Sy`: refreshing the package list without upgrading
        # the system is Arch's partial-upgrade trap (new packages link against
        # libraries the machine does not have yet). `-S --needed` installs
        # against the existing database; if that database is stale pacman says
        # "target not found", and the fix is the operator's own `pacman -Syu` -
        # never ours to force.
        return [pre + ["pacman", "-S", "--needed", "--noconfirm"] + pkgs]
    return []


def update_commands(manager, packages, brew_path="brew", sudo=False) -> list[list[str]]:
    """The argv list(s) to UPGRADE already-installed `packages`. winget's "no
    applicable update" exit code is whitelisted in install_exit_ok; brew exits
    0 for an up-to-date formula."""
    packages = list(packages)
    if manager == "winget":
        return [["winget", "upgrade", "--id", WINGET_IDS[p], "-e",
                 "--accept-source-agreements", "--accept-package-agreements"]
                for p in packages if p in WINGET_IDS]
    if manager == "brew":
        formulae = [BREW_FORMULAE[p] for p in packages if p in BREW_FORMULAE]
        return [[brew_path, "upgrade"] + formulae] if formulae else []
    if manager == "apt":
        pkgs = [APT_PACKAGES[p] for p in packages if p in APT_PACKAGES]
        if not pkgs:
            return []
        pre = ["sudo"] if sudo else []
        return [pre + ["apt-get", "update"],
                pre + ["apt-get", "install", "-y", "--only-upgrade"] + pkgs]
    if manager == "pacman":
        # Nothing to emit. Upgrading individual packages on a rolling release IS
        # the partial upgrade to avoid, and the correct action - `pacman -Syu` -
        # upgrades the whole machine, which is the operator's call and not a
        # side effect of `install-tools --update`. main() prints that pointer.
        return []
    return []


def manual_guide(platform: str, manager: str | None = None) -> str:
    """What to type when this module could not do it."""
    if manager == "pacman":
        return ("Install manually:  sudo pacman -S --needed ffmpeg yt-dlp\n"
                "  (if pacman reports 'target not found', its package list is stale -\n"
                "   run `sudo pacman -Syu` first; never `pacman -Sy` on its own)")
    if platform.startswith("win"):
        return ("Install manually with winget (one per line):\n"
                "  winget install --id Gyan.FFmpeg -e   # ffmpeg\n"
                "  winget install --id yt-dlp.yt-dlp -e")
    if platform == "darwin":
        return "Install manually:  brew install ffmpeg yt-dlp"
    return ("Install manually:  sudo apt-get update && sudo apt-get install -y ffmpeg\n"
            "yt-dlp is a managed install (apt's lags upstream and fails YouTube's\n"
            "bot check) - install-tools sets it up automatically. Manually:\n"
            "  https://github.com/yt-dlp/yt-dlp#installation")


# yt-dlp on Linux: apt's package lags upstream badly and no longer passes
# YouTube's bot check - and YouTube is where every clip in this project comes
# from. So Linux gets a pinned, SHA-256-verified standalone binary straight from
# yt-dlp's GitHub releases, into the managed tool dir. The release asset is a
# BARE executable (no archive), so there is no extraction step. Windows (winget)
# and macOS (brew) keep their yt-dlp package.
#
# To bump: read the new tag and its two hashes out of the release's own
# SHA2-256SUMS, never from anywhere else -
#   TAG=$(gh release view --repo yt-dlp/yt-dlp --json tagName -q .tagName)
#   curl -sSfL ".../releases/download/${TAG}/SHA2-256SUMS" | grep yt-dlp_linux
YTDLP_VERSION = "2026.07.04"
YTDLP_BIN_NAME = "yt-dlp"
YTDLP_URL_TMPL = ("https://github.com/yt-dlp/yt-dlp/releases/download/"
                  "{ver}/yt-dlp_{tag}")
YTDLP_DOWNLOADS = {
    "linux":         "6bbb3d314cde4febe36e5fa1d55462e29c974f63444e707871834f6d8cc210ae",
    "linux_aarch64": "b6ce97646773070d7a7ffd6bbbdcaecb47c48483909c54c915bf08a7a9b5e0b1",
}


def ytdlp_asset_tag(platform: str, machine: str | None) -> str | None:
    """Map (sys.platform, platform.machine()) to a YTDLP_DOWNLOADS tag, or None
    for Windows/macOS (their package managers ship a current yt-dlp) and for
    unsupported architectures. Pure."""
    if platform.startswith("linux"):
        m = (machine or "").lower()
        if m in ("x86_64", "amd64"):
            return "linux"
        if m in ("aarch64", "arm64"):
            return "linux_aarch64"
    return None


def ytdlp_download_url(tag: str, ver: str = YTDLP_VERSION) -> str:
    return YTDLP_URL_TMPL.format(ver=ver, tag=tag)


def install_ytdlp_binary(dest_dir, tag, opener=None, downloads=None) -> str:
    """Download yt-dlp's standalone Linux binary for `tag`, verify its SHA-256
    against the pinned value, write it to dest_dir/yt-dlp and make it
    executable. Returns the path. Raises RuntimeError on a checksum mismatch -
    BEFORE anything is written, so a refusal never leaves an unverified file for
    ensure_tool_path to put on PATH. `opener` (url -> bytes) is the injectable
    seam for tests; it defaults to a cert-verified HTTPS GET."""
    downloads = downloads or YTDLP_DOWNLOADS
    want = downloads[tag]
    if opener is None:
        def opener(url):
            with urllib.request.urlopen(url, timeout=120) as response:  # nosec - pinned host, checksum-verified below
                return response.read()
    blob = opener(ytdlp_download_url(tag))
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        raise RuntimeError(f"yt-dlp download checksum mismatch for {tag}: {got} != {want}")
    os.makedirs(dest_dir, exist_ok=True)
    binpath = os.path.join(dest_dir, YTDLP_BIN_NAME)
    # Replaced, not rewritten in place: another process may be EXECUTING
    # this binary while an update runs.
    atomicwrite.write_bytes(binpath, blob)
    ownermode.restrict(binpath)
    if os.name != "nt":
        os.chmod(binpath, 0o700)   # owner rwx: this tool executes it
    return binpath


# Homebrew's bin dirs, prepended only for a FROZEN macOS launch - see
# ensure_tool_path.
BREW_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def managed_tools_dir(workspace_root) -> Path:
    """Where install-tools drops the binaries it downloads itself.

    `.tools`, not `bin` and not `tools`: workspace.resolve()'s last resort
    returns the REPOSITORY root, so under that fallback `<workspace>/bin` IS
    this repo's bin/ (holding bin/yt-shorts) and `<workspace>/tools` is its
    tools/. Either name would drop a downloaded binary into the source tree.
    A dotted name collides with nothing and follows the precedent .studio.lock
    already sets for a runtime file at the workspace root."""
    return Path(workspace_root) / ".tools"


def augment_path(current, candidates, exists=os.path.isdir) -> str | None:
    """The PATH `current` should become so the dirs in `candidates` are
    reachable: each candidate that exists on disk but is missing from PATH,
    prepended in candidate order ahead of the existing entries. Returns None
    when nothing needs adding, so the caller can leave the environment
    untouched. Pure. Ported from racecast."""
    have = current.split(os.pathsep) if current else []
    add = [d for d in candidates if exists(d) and d not in have]
    if not add:
        return None
    return os.pathsep.join(add + have)


def ensure_tool_path(workspace_root, environ=None, frozen=None, platform=None,
                     exists=None) -> None:
    """Prepend the tool dirs this module writes to but that are not on this
    process's PATH, so every subprocess spawned from here on resolves them.

    Two sources:
    * The managed tool dir (`<workspace>/.tools`), where install_ytdlp_binary
      drops its download. It is NEVER on the operator's shell PATH, so it is
      added on every platform.
    * Frozen on macOS only: a binary launched from Finder or the Dock inherits a
      truncated PATH (/usr/bin:/bin:/usr/sbin:/sbin) that omits Homebrew, so a
      brew-installed ffmpeg or yt-dlp looks missing (racecast's issue #38). A
      terminal launch already has a full PATH and is left alone.

    Only genuinely-missing dirs that exist on disk are added (augment_path), so
    this is a no-op in the normal case. `environ`, `frozen`, `platform` and
    `exists` are the injectable seams for tests. `exists` must be threaded
    through: `augment_path`'s default binds os.path.isdir at DEFINITION time,
    so monkeypatching it does nothing."""
    environ = os.environ if environ is None else environ
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    platform = sys.platform if platform is None else platform
    exists = os.path.isdir if exists is None else exists
    candidates = [str(managed_tools_dir(workspace_root))]
    if frozen and platform == "darwin":
        candidates += list(BREW_BIN_DIRS)
    new = augment_path(environ.get("PATH", ""), candidates, exists=exists)
    if new:
        environ["PATH"] = new


def _registry_path_values():
    """The system and user Path values from the Windows registry."""
    import winreg
    values = []
    for root, key in ((winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(root, key) as handle:
                values.append(winreg.QueryValueEx(handle, "Path")[0])
        except OSError:
            pass  # key or value absent (e.g. no user Path) - skip that hive
    return values


def windows_fresh_path(read_values=None) -> str | None:
    """The PATH a NEW shell would get (system + user, from the registry).
    winget updates the registry, not running processes - this process's PATH
    predates anything installed during this run. None when there is nothing to
    read (non-Windows)."""
    if read_values is None:
        if not sys.platform.startswith("win"):
            return None
        read_values = _registry_path_values
    parts = [os.path.expandvars(v) for v in read_values() if v]
    return os.pathsep.join(parts) or None


def _which_with_fresh_path(fresh_path):
    """A which() that falls back to the registry PATH (Windows): a
    just-installed tool is not on THIS process's PATH yet, but a new shell
    will see it."""
    def probe(name):
        return shutil.which(name) or (
            shutil.which(name, path=fresh_path) if fresh_path else None)
    return probe


def _which_with_managed_dir(managed_dir, brew=None):
    """A which() that also looks in the managed tool dir (never on the user's
    shell PATH) and, on macOS, in brew's own bin dir (not on PATH right after a
    fresh bootstrap). ensure_tool_path does this for real runs; this probe lets
    install-tools confirm its own work without it."""
    prefix_bin = os.path.dirname(brew) if brew else None

    def probe(name):
        hit = shutil.which(name)
        if hit:
            return hit
        for directory in (str(managed_dir), prefix_bin):
            if directory:
                candidate = os.path.join(directory, name)
                if os.path.exists(candidate):
                    return candidate
        return None
    return probe


def _run_remote_script(url, argv):
    """Download `url` over cert-verified HTTPS to a temp file and run it
    visibly. No shell pipe - the operator saw the URL and confirmed first."""
    print("Downloading:", url)
    with urllib.request.urlopen(url, timeout=30) as response:  # nosec - operator-confirmed official URL
        body = response.read()
    # delete=False: the file must outlive the handle so the runner can read it.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".sh") as tmp:
        tmp.write(body)
        path = tmp.name
    try:
        return subprocess.call(list(argv) + [path])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass  # a temp file we could not remove is not worth failing over


def bootstrap_brew(assume_yes, input_fn=input, run=None, find=None) -> str | None:
    """Offer the official brew.sh installer (macOS). Returns the absolute brew
    path on success, None if declined or failed. The installer runs as the
    current user, prompts for sudo itself, and may download the Xcode Command
    Line Tools - a one-time setup that can take a while."""
    run = _run_remote_script if run is None else run
    find = find_brew if find is None else find
    print("Homebrew is required but not installed. Official installer:")
    print(" ", BREW_INSTALLER)
    print("  (runs as your user, asks for sudo; may download the Xcode Command")
    print("   Line Tools - this one-time setup can take a while)")
    if not assume_yes and not confirmed(input_fn("Bootstrap Homebrew now? [y/N] ")):
        print("aborted.")
        return None
    if run(BREW_INSTALLER, ["/bin/bash"]) != 0:
        return None
    return find()


def run(workspace_root, *, update=False, assume_yes=False, platform=None,
        machine=None, which=None, call=None, printer=print) -> int:
    """Install (and optionally upgrade) the external tools. Returns 0 when
    everything required is present afterwards, 1 otherwise. Every seam is
    injectable so this is testable without a package manager: `which` resolves
    tools, `call` runs a command, `printer` receives the operator-facing text."""
    platform = sys.platform if platform is None else platform
    if machine is None:
        import platform as _platform
        machine = _platform.machine()
    call = subprocess.call if call is None else call
    managed = managed_tools_dir(workspace_root)

    probe = which or _which_with_fresh_path(windows_fresh_path())
    missing = missing_tools(which=probe)
    if not missing and not update:
        printer("All external tools already installed: " + ", ".join(TOOLS))
        printer("  (run `yt-shorts install-tools --update` to upgrade them)")
        return 0
    if missing:
        printer("Missing tools: " + ", ".join(missing))

    brew = None
    if platform == "darwin":
        # find_brew goes through `probe`, not shutil.which: a test that injects
        # `which` must not be able to reach the real Homebrew - or, worse, a
        # machine without it, where bootstrap_brew would prompt on stdin.
        brew = find_brew(which=probe)
        if not brew:
            brew = bootstrap_brew(assume_yes)
        if not brew:
            printer("No supported package manager found.")
            printer(manual_guide(platform))
            return 1
        manager = "brew"
    else:
        manager = pick_manager(platform, which=probe)
        if manager is None:
            printer("No supported package manager found.")
            printer(manual_guide(platform))
            return 1

    # apt/pacman need root and, unlike winget/brew, will not prompt for it.
    sudo = manager in ("apt", "pacman") and hasattr(os, "geteuid") and os.geteuid() != 0
    # ffprobe has no package of its own; a missing one means ffmpeg is missing.
    wanted = [p for p in PACKAGES
              if p in missing or (p == "ffmpeg" and "ffprobe" in missing)]
    present = [p for p in PACKAGES if p not in wanted]

    cmds = []
    if update and present:
        printer("Updating installed tools: " + ", ".join(present))
        cmds += update_commands(manager, present, brew_path=brew or "brew", sudo=sudo)
        if manager == "pacman":
            printer("NOTE: on Arch these are repository packages - upgrade them with")
            printer("      the system:  sudo pacman -Syu   (a per-package upgrade would")
            printer("      leave the machine in a partial-upgrade state)")
    cmds += install_commands(manager, wanted, brew_path=brew or "brew", sudo=sudo)

    failed = []
    for cmd in cmds:
        printer("Running: " + " ".join(cmd))
        if not install_exit_ok(manager, call(cmd)):
            failed.append(" ".join(cmd))

    # yt-dlp on Linux: the pinned binary, not apt. Refreshed on --update too, so
    # the pre-session `install-tools --update` bumps it to the pinned-current
    # version - it goes stale in weeks.
    if manager in ("apt",) and ("yt-dlp" in missing or update):
        tag = ytdlp_asset_tag(platform, machine)
        if tag is None:
            printer("NOTE: no prebuilt yt-dlp for this OS/arch - install it manually:")
            printer("  https://github.com/yt-dlp/yt-dlp#installation")
        else:
            printer(f"Installing yt-dlp {YTDLP_VERSION} -> {managed} ...")
            try:
                install_ytdlp_binary(str(managed), tag)
                printer("  yt-dlp installed.")
            except Exception as error:   # network, checksum or write - report, do not crash
                failed.append(f"yt-dlp download ({error})")

    still = missing_tools(which=which or _which_with_managed_dir(managed, brew))
    if failed or still:
        printer("Some installs did not complete.")
        if failed:
            printer("Failed: " + "; ".join(failed))
        if still:
            printer("Still missing: " + ", ".join(still))
        printer(manual_guide(platform, manager))
        return 1
    printer("All tools " + ("up to date" if update else "installed")
            + ". Run `yt-shorts doctor` to verify.")
    return 0
