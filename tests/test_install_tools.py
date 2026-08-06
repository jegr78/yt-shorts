"""The install-tools decision helpers: which manager, which packages, which
commands. Every seam is injected - no test here touches a real package manager,
and none may. This machine's ffmpeg is deliberately a specific build the
racecast project depends on (CLAUDE.md, "Hard constraints")."""

import hashlib
import os

import pytest

from yt_shorts import ownermode
from yt_shorts import install_tools as it


class TestTheToolSet:
    def test_three_names_are_checked_for_presence(self):
        assert it.TOOLS == ("ffmpeg", "ffprobe", "yt-dlp")

    def test_but_only_two_packages_are_installable(self):
        """ffprobe is not separately installable - it ships with ffmpeg."""
        assert it.PACKAGES == ("ffmpeg", "yt-dlp")


class TestPickingTheManager:
    def test_windows_uses_winget_when_present(self):
        assert it.pick_manager("win32", which=lambda n: "/w/winget") == "winget"

    def test_windows_without_winget_has_none(self):
        assert it.pick_manager("win32", which=lambda n: None) is None

    def test_macos_uses_brew_when_present(self):
        assert it.pick_manager("darwin", which=lambda n: "/opt/homebrew/bin/brew") == "brew"

    def test_macos_without_brew_has_none(self):
        """The caller then offers the Homebrew bootstrap - see Task 7."""
        assert it.pick_manager("darwin", which=lambda n: None) is None

    def test_linux_prefers_apt(self):
        assert it.pick_manager("linux", which=lambda n: "/usr/bin/" + n) == "apt"

    def test_linux_falls_back_to_pacman(self):
        which = lambda n: "/usr/bin/pacman" if n == "pacman" else None
        assert it.pick_manager("linux", which=which) == "pacman"

    def test_linux_with_neither_has_none(self):
        assert it.pick_manager("linux", which=lambda n: None) is None


class TestMissingTools:
    def test_all_present_is_empty(self):
        assert it.missing_tools(which=lambda n: "/usr/bin/" + n) == []

    def test_only_the_absent_ones_are_reported(self):
        which = lambda n: None if n == "yt-dlp" else "/usr/bin/" + n
        assert it.missing_tools(which=which) == ["yt-dlp"]


class TestInstallCommands:
    def test_winget_installs_one_package_per_command(self):
        cmds = it.install_commands("winget", ["ffmpeg", "yt-dlp"])
        assert len(cmds) == 2
        assert cmds[0][:4] == ["winget", "install", "--id", "Gyan.FFmpeg"]
        assert cmds[1][3] == "yt-dlp.yt-dlp"

    def test_brew_installs_everything_in_one_command(self):
        assert it.install_commands("brew", ["ffmpeg", "yt-dlp"]) == \
            [["brew", "install", "ffmpeg", "yt-dlp"]]

    def test_brew_is_invoked_by_absolute_path_when_given_one(self):
        """A freshly bootstrapped brew is not on this process's PATH."""
        cmds = it.install_commands("brew", ["ffmpeg"], brew_path="/opt/homebrew/bin/brew")
        assert cmds[0][0] == "/opt/homebrew/bin/brew"

    def test_apt_installs_only_ffmpeg(self):
        """yt-dlp on Linux is a managed download, not an apt package - apt's
        lags upstream far enough to fail YouTube's bot check."""
        cmds = it.install_commands("apt", ["ffmpeg", "yt-dlp"])
        assert cmds[-1][-1:] == ["ffmpeg"]
        assert not any("yt-dlp" in c for cmd in cmds for c in cmd)

    def test_apt_refreshes_its_index_first(self):
        """A fresh cloud VM cannot locate the package otherwise."""
        cmds = it.install_commands("apt", ["ffmpeg"])
        assert cmds[0][-2:] == ["apt-get", "update"]

    def test_apt_prepends_sudo_when_asked(self):
        cmds = it.install_commands("apt", ["ffmpeg"], sudo=True)
        assert all(cmd[0] == "sudo" for cmd in cmds)

    def test_apt_with_nothing_to_install_emits_nothing(self):
        assert it.install_commands("apt", ["yt-dlp"]) == []

    def test_pacman_installs_both_without_refreshing(self):
        """Deliberately NOT -Sy: refreshing without upgrading is Arch's
        partial-upgrade trap."""
        cmds = it.install_commands("pacman", ["ffmpeg", "yt-dlp"])
        assert len(cmds) == 1
        assert "-Sy" not in cmds[0]
        assert cmds[0][-2:] == ["ffmpeg", "yt-dlp"]

    def test_an_unknown_manager_emits_nothing(self):
        assert it.install_commands("nix", ["ffmpeg"]) == []


class TestUpdateCommands:
    def test_winget_upgrades_per_package(self):
        cmds = it.update_commands("winget", ["ffmpeg"])
        assert cmds[0][:2] == ["winget", "upgrade"]

    def test_brew_upgrades_in_one_command(self):
        assert it.update_commands("brew", ["ffmpeg", "yt-dlp"]) == \
            [["brew", "upgrade", "ffmpeg", "yt-dlp"]]

    def test_apt_upgrades_only_what_it_owns(self):
        cmds = it.update_commands("apt", ["ffmpeg", "yt-dlp"])
        assert "--only-upgrade" in cmds[-1]
        assert cmds[-1][-1] == "ffmpeg"

    def test_pacman_emits_nothing(self):
        """A per-package upgrade on a rolling release IS the partial upgrade to
        avoid; the correct action is the operator's own `pacman -Syu`."""
        assert it.update_commands("pacman", ["ffmpeg"]) == []


class TestTheManualGuide:
    @pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
    def test_every_platform_gets_both_tools_named(self, platform):
        # Case-insensitive: the Windows guide names ffmpeg only as the winget
        # publisher id `Gyan.FFmpeg`, so a case-sensitive "ffmpeg" in guide
        # would pass today only because an independent `# ffmpeg` annotation
        # happens to also be present in that string - deleting that comment
        # would redden a still-correct guide.
        guide = it.manual_guide(platform).lower()
        assert "ffmpeg" in guide and "yt-dlp" in guide

    def test_the_pacman_guide_warns_against_a_bare_sy(self):
        assert "-Syu" in it.manual_guide("linux", manager="pacman")


class TestInstallExitCodes:
    def test_zero_is_success(self):
        assert it.install_exit_ok("brew", 0)

    def test_a_nonzero_code_is_a_failure(self):
        assert not it.install_exit_ok("brew", 1)

    @pytest.mark.parametrize("code", [0x8A15002B, 0x8A150061])
    def test_wingets_already_installed_codes_count_as_success(self, code):
        """subprocess reports them as unsigned DWORDs; PowerShell shows them
        signed. Normalised through the 32-bit mask."""
        assert it.install_exit_ok("winget", code)

    def test_those_codes_mean_nothing_for_another_manager(self):
        assert not it.install_exit_ok("apt", 0x8A15002B)


class TestConfirmed:
    @pytest.mark.parametrize("answer", ["y", "Y", "yes", " Yes "])
    def test_a_yes_is_a_yes(self, answer):
        assert it.confirmed(answer)

    @pytest.mark.parametrize("answer", ["", "n", "no", "later"])
    def test_everything_else_is_not(self, answer):
        assert not it.confirmed(answer)


class TestFindingBrew:
    def test_path_wins(self):
        assert it.find_brew(which=lambda n: "/usr/local/bin/brew",
                            exists=lambda p: False) == "/usr/local/bin/brew"

    def test_the_standard_locations_are_the_fallback(self):
        """A fresh bootstrap is not on this process's PATH - shellenv only runs
        in new shells - so brew must be found by absolute path."""
        assert it.find_brew(which=lambda n: None,
                            exists=lambda p: p == "/opt/homebrew/bin/brew") \
            == "/opt/homebrew/bin/brew"

    def test_apple_silicon_is_preferred_over_intel(self):
        assert it.find_brew(which=lambda n: None, exists=lambda p: True) \
            == "/opt/homebrew/bin/brew"

    def test_nothing_found_is_none(self):
        assert it.find_brew(which=lambda n: None, exists=lambda p: False) is None


class TestTheYtdlpAssetTag:
    """Only Linux gets the managed download. Windows and macOS have current
    packages in winget and brew."""

    def test_linux_x86_64(self):
        assert it.ytdlp_asset_tag("linux", "x86_64") == "linux"

    def test_linux_amd64_is_the_same_machine(self):
        assert it.ytdlp_asset_tag("linux", "AMD64") == "linux"

    def test_linux_aarch64(self):
        assert it.ytdlp_asset_tag("linux", "aarch64") == "linux_aarch64"

    def test_linux_arm64_is_the_same_machine(self):
        assert it.ytdlp_asset_tag("linux", "arm64") == "linux_aarch64"

    def test_an_unsupported_linux_arch_has_no_tag(self):
        assert it.ytdlp_asset_tag("linux", "riscv64") is None

    @pytest.mark.parametrize("platform", ["darwin", "win32"])
    def test_the_platforms_with_a_package_have_no_tag(self, platform):
        assert it.ytdlp_asset_tag(platform, "x86_64") is None

    def test_a_missing_machine_string_does_not_crash(self):
        assert it.ytdlp_asset_tag("linux", None) is None


class TestTheDownloadUrl:
    def test_it_names_the_pinned_version_and_the_asset(self):
        url = it.ytdlp_download_url("linux")
        assert it.YTDLP_VERSION in url
        assert url.endswith("/yt-dlp_linux")

    def test_every_pinned_tag_has_a_checksum(self):
        assert set(it.YTDLP_DOWNLOADS) == {"linux", "linux_aarch64"}
        assert all(len(h) == 64 for h in it.YTDLP_DOWNLOADS.values())


class TestInstallingTheBinary:
    def test_a_matching_checksum_writes_an_executable(self, tmp_path):
        blob = b"#!/not-really-yt-dlp\n"
        digest = hashlib.sha256(blob).hexdigest()

        path = it.install_ytdlp_binary(str(tmp_path), "linux",
                                       opener=lambda url: blob,
                                       downloads={"linux": digest})

        with open(path, "rb") as f:
            assert f.read() == blob
        assert os.access(path, os.X_OK)

    def test_the_binary_is_named_yt_dlp(self, tmp_path):
        blob = b"x"
        path = it.install_ytdlp_binary(
            str(tmp_path), "linux", opener=lambda url: blob,
            downloads={"linux": hashlib.sha256(blob).hexdigest()})

        assert os.path.basename(path) == "yt-dlp"

    def test_it_is_owner_only(self, tmp_path):
        """This tool executes what it downloads; nobody else needs to."""
        blob = b"x"
        path = it.install_ytdlp_binary(
            str(tmp_path), "linux", opener=lambda url: blob,
            downloads={"linux": hashlib.sha256(blob).hexdigest()})

        assert ownermode.is_owner_only(path)

    def test_a_mismatched_checksum_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            it.install_ytdlp_binary(str(tmp_path), "linux",
                                    opener=lambda url: b"tampered",
                                    downloads={"linux": "0" * 64})

    def test_a_mismatched_checksum_writes_nothing_at_all(self, tmp_path):
        """The refusal must not leave a partial or unverified file behind for
        ensure_tool_path to later put on PATH - checked both for the final
        path (a regression could still write a partial file under another
        name) and for the whole destination tree, and against a NESTED
        destination that does not exist yet (os.makedirs is only reached
        AFTER the checksum compare, so a mismatch here must not create the
        directory either)."""
        dest = tmp_path / "nested"
        with pytest.raises(RuntimeError):
            it.install_ytdlp_binary(str(dest), "linux",
                                    opener=lambda url: b"tampered",
                                    downloads={"linux": "0" * 64})

        assert not dest.exists()
        assert list(tmp_path.iterdir()) == []

    def test_the_destination_directory_is_created(self, tmp_path):
        blob = b"x"
        dest = tmp_path / "deeper" / "still"
        it.install_ytdlp_binary(str(dest), "linux", opener=lambda url: blob,
                                downloads={"linux": hashlib.sha256(blob).hexdigest()})

        assert dest.is_dir()


class TestAugmentPath:
    def test_a_missing_existing_dir_is_prepended(self):
        result = it.augment_path("/usr/bin", ["/managed"], exists=lambda p: True)

        assert result == os.pathsep.join(["/managed", "/usr/bin"])

    def test_candidate_order_is_preserved(self):
        result = it.augment_path("/usr/bin", ["/a", "/b"], exists=lambda p: True)

        assert result == os.pathsep.join(["/a", "/b", "/usr/bin"])

    def test_a_dir_already_on_path_is_not_added_again(self):
        assert it.augment_path("/managed:/usr/bin", ["/managed"],
                               exists=lambda p: True) is None

    def test_a_dir_that_does_not_exist_is_not_added(self):
        assert it.augment_path("/usr/bin", ["/nope"], exists=lambda p: False) is None

    def test_nothing_to_add_returns_none_so_the_caller_leaves_environ_alone(self):
        assert it.augment_path("/usr/bin", [], exists=lambda p: True) is None

    def test_an_empty_path_still_works(self):
        assert it.augment_path("", ["/managed"], exists=lambda p: True) == "/managed"


class TestTheManagedDirectory:
    def test_it_is_dot_tools_under_the_workspace_root(self, tmp_path):
        assert it.managed_tools_dir(tmp_path) == tmp_path / ".tools"

    def test_it_is_not_bin(self, tmp_path):
        """workspace.resolve()'s last resort returns the REPOSITORY root, where
        bin/ already holds bin/yt-shorts - a managed 'bin' would drop a
        downloaded yt-dlp into the source tree."""
        assert it.managed_tools_dir(tmp_path).name != "bin"

    def test_it_is_not_tools(self, tmp_path):
        """Same fallback, same problem: tools/ is this repository's own."""
        assert it.managed_tools_dir(tmp_path).name != "tools"


class TestEnsureToolPath:
    def test_the_managed_dir_goes_on_path(self, tmp_path):
        (tmp_path / ".tools").mkdir()
        env = {"PATH": "/usr/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=False, platform="linux")

        assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / ".tools")

    def test_an_absent_managed_dir_changes_nothing(self, tmp_path):
        env = {"PATH": "/usr/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=False, platform="linux")

        assert env["PATH"] == "/usr/bin"

    def test_calling_it_twice_does_not_duplicate_the_entry(self, tmp_path):
        (tmp_path / ".tools").mkdir()
        env = {"PATH": "/usr/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=False, platform="linux")
        it.ensure_tool_path(tmp_path, environ=env, frozen=False, platform="linux")

        assert env["PATH"].count(str(tmp_path / ".tools")) == 1

    # These two describe a macOS filesystem and must not consult the real one.
    # Monkeypatching os.path.isdir does not work here - see ensure_tool_path.
    @staticmethod
    def _macos_dirs(tools_dir):
        return lambda p: p in (str(tools_dir), "/opt/homebrew/bin")

    def test_a_frozen_macos_launch_also_gets_the_homebrew_dirs(self, tmp_path):
        """A binary launched by double-click from Finder inherits a truncated
        PATH (/usr/bin:/bin:/usr/sbin:/sbin) with no Homebrew in it, so a
        brew-installed ffmpeg looks missing. racecast measured this as its
        issue #38; it applies here the moment the studio is double-clicked."""
        env = {"PATH": "/usr/bin:/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=True, platform="darwin",
                            exists=self._macos_dirs(tmp_path / ".tools"))

        assert "/opt/homebrew/bin" in env["PATH"].split(os.pathsep)

    def test_an_unfrozen_macos_run_does_not(self, tmp_path):
        """A terminal launch already has a full PATH; adding to it would be
        noise, and the point of augment_path is to leave it alone."""
        env = {"PATH": "/usr/bin:/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=False, platform="darwin",
                            exists=self._macos_dirs(tmp_path / ".tools"))

        assert "/opt/homebrew/bin" not in env["PATH"].split(os.pathsep)

    def test_a_frozen_linux_launch_does_not_get_homebrew_dirs(self, tmp_path):
        (tmp_path / ".tools").mkdir()
        env = {"PATH": "/usr/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=True, platform="linux")

        assert "/opt/homebrew/bin" not in env["PATH"]


class TestTheWindowsFreshPath:
    """winget updates the REGISTRY, not this running process. Without re-reading
    it, install-tools reports 'still missing' immediately after its own
    successful install."""

    def test_the_hives_are_joined_in_order(self):
        result = it.windows_fresh_path(read_values=lambda: ["C:\\sys", "C:\\user"])

        assert result == os.pathsep.join(["C:\\sys", "C:\\user"])

    def test_empty_values_are_dropped(self):
        assert it.windows_fresh_path(read_values=lambda: ["C:\\sys", ""]) == "C:\\sys"

    def test_nothing_readable_is_none(self):
        assert it.windows_fresh_path(read_values=lambda: []) is None


class TestTheBrewBootstrap:
    def test_declining_installs_nothing(self):
        ran = []
        result = it.bootstrap_brew(False, input_fn=lambda prompt: "n",
                                   run=lambda url, argv: ran.append(url) or 0,
                                   find=lambda: "/opt/homebrew/bin/brew")

        assert result is None
        assert ran == []

    def test_accepting_runs_the_official_installer_and_returns_the_path(self):
        ran = []
        result = it.bootstrap_brew(False, input_fn=lambda prompt: "y",
                                   run=lambda url, argv: ran.append(url) or 0,
                                   find=lambda: "/opt/homebrew/bin/brew")

        assert ran == [it.BREW_INSTALLER]
        assert result == "/opt/homebrew/bin/brew"

    def test_yes_skips_the_prompt_entirely(self):
        asked = []
        it.bootstrap_brew(True, input_fn=lambda prompt: asked.append(prompt) or "n",
                          run=lambda url, argv: 0, find=lambda: "/opt/homebrew/bin/brew")

        assert asked == []

    def test_a_failed_installer_is_none_rather_than_a_bogus_path(self):
        assert it.bootstrap_brew(True, input_fn=lambda p: "y",
                                 run=lambda url, argv: 1,
                                 find=lambda: "/opt/homebrew/bin/brew") is None


class TestTheCommand:
    """`run()` drives the installs. Every seam is injected: no test here invokes
    a package manager, and none may."""

    def test_everything_present_and_no_update_runs_nothing(self, tmp_path):
        calls = []
        code = it.run(tmp_path, platform="linux", machine="x86_64",
                      which=lambda n: "/usr/bin/" + n,
                      call=lambda cmd: calls.append(cmd) or 0)

        assert code == 0
        assert calls == []

    def test_a_missing_tool_is_installed(self, tmp_path):
        calls = []
        which = lambda n: None if n == "ffmpeg" else "/usr/bin/" + n
        it.run(tmp_path, platform="linux", machine="x86_64", which=which,
               call=lambda cmd: calls.append(cmd) or 0)

        assert any("ffmpeg" in cmd for cmd in calls)

    def test_update_upgrades_what_is_already_there(self, tmp_path, monkeypatch):
        # update=True alone (independent of what `which` reports missing) is
        # enough to reach the yt-dlp-refresh branch on Linux - unpatched, this
        # test performed a REAL ~40MB HTTPS download of the pinned yt-dlp
        # release binary on every run, an actual violation of "no test may
        # reach the network" that its own assertion (about --only-upgrade,
        # unrelated) never caught because run() swallows a download failure
        # into `failed` and this test never inspects it.
        monkeypatch.setattr(it, "install_ytdlp_binary", lambda dest, tag, **kw: dest)
        calls = []
        it.run(tmp_path, update=True, platform="linux", machine="x86_64",
               which=lambda n: "/usr/bin/" + n,
               call=lambda cmd: calls.append(cmd) or 0)

        assert any("--only-upgrade" in cmd for cmd in calls)

    def test_no_package_manager_is_a_failure_with_the_manual_guide(self, tmp_path):
        lines = []
        code = it.run(tmp_path, platform="linux", machine="x86_64",
                      which=lambda n: None, call=lambda cmd: 0,
                      printer=lambda *a: lines.append(" ".join(str(x) for x in a)))

        assert code == 1
        assert any("Install manually" in line for line in lines)

    def test_a_failed_command_is_reported_and_returns_one(self, tmp_path):
        lines = []
        which = lambda n: None if n == "ffmpeg" else "/usr/bin/" + n
        code = it.run(tmp_path, platform="linux", machine="x86_64", which=which,
                      call=lambda cmd: 1,
                      printer=lambda *a: lines.append(" ".join(str(x) for x in a)))

        assert code == 1
        assert any("did not complete" in line for line in lines)

    def test_linux_downloads_yt_dlp_rather_than_asking_apt_for_it(self, tmp_path, monkeypatch):
        downloaded = []
        monkeypatch.setattr(it, "install_ytdlp_binary",
                            lambda dest, tag, **kw: downloaded.append((dest, tag)) or
                            os.path.join(dest, "yt-dlp"))
        which = lambda n: None if n == "yt-dlp" else "/usr/bin/" + n
        it.run(tmp_path, platform="linux", machine="x86_64", which=which,
               call=lambda cmd: 0)

        assert downloaded == [(str(it.managed_tools_dir(tmp_path)), "linux")]

    def test_update_refreshes_the_pinned_yt_dlp_too(self, tmp_path, monkeypatch):
        """yt-dlp goes stale in weeks - `install-tools --update` before a
        session is the whole point of the flag."""
        downloaded = []
        monkeypatch.setattr(it, "install_ytdlp_binary",
                            lambda dest, tag, **kw: downloaded.append(tag) or
                            os.path.join(dest, "yt-dlp"))
        it.run(tmp_path, update=True, platform="linux", machine="x86_64",
               which=lambda n: "/usr/bin/" + n, call=lambda cmd: 0)

        assert downloaded == ["linux"]

    def test_macos_does_not_download_yt_dlp(self, tmp_path, monkeypatch):
        """brew ships a current one."""
        downloaded = []
        monkeypatch.setattr(it, "install_ytdlp_binary",
                            lambda dest, tag, **kw: downloaded.append(tag))
        which = lambda n: None if n == "yt-dlp" else "/opt/homebrew/bin/" + n
        it.run(tmp_path, platform="darwin", machine="arm64", which=which,
               call=lambda cmd: 0)

        assert downloaded == []

    def test_a_download_failure_is_reported_not_raised(self, tmp_path, monkeypatch):
        def explode(dest, tag, **kw):
            raise RuntimeError("checksum mismatch")

        monkeypatch.setattr(it, "install_ytdlp_binary", explode)
        lines = []
        which = lambda n: None if n == "yt-dlp" else "/usr/bin/" + n
        code = it.run(tmp_path, platform="linux", machine="x86_64", which=which,
                      call=lambda cmd: 0,
                      printer=lambda *a: lines.append(" ".join(str(x) for x in a)))

        assert code == 1
        assert any("checksum mismatch" in line for line in lines)
