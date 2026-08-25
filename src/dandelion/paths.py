"""Where an installation lives, and how it bridges upstream's path assumptions.

Upstream resolves paths from the package location and from config-file locations, never from
an environment variable we could simply set (see `drivers/anchoring.py`). So the install
layout is not a matter of taste: it is the shape that makes an unmodified research codebase
find a 26 GB data directory that is not inside it.

    %LOCALAPPDATA%\\Dandelion\\
      app\\            the executable, current and previous, plus the update cache
      runtime\\        uv-managed CPython (UV_PYTHON_INSTALL_DIR points here)
      cache\\          uv's download cache (UV_CACHE_DIR points here)
      envs\\<tag>\\     one virtual environment per installed code release
      code\\<tag>\\     the extracted release: a working tree whose *code files* are immutable
      store\\<tag>\\    fitted models and reports, seeded from the release, shared by all runs
      runs\\<run-id>\\  one directory per Studio run: workbook, config overlay, outputs, logs
      logs\\           job logs
      settings.json   preferences. Secrets go to Windows Credential Manager, never here.
      install_manifest.json

`data\\` is deliberately NOT under the app directory by default: it is the largest thing on the
machine and users often want it on another drive.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_FOLDER_NAME = "Dandelion"

#: Free space we ask for before letting an install proceed. Measured composition:
#: data 26 GB + MaStR bulk 7.5 GB (in the user profile, not the data dir) + weathergen output
#: 1.3 GB + Monte-Carlo scratch 1.5 GB + runtime and venv ~2 GB, plus headroom.
REQUIRED_FREE_GB = 45

#: Below this we refuse rather than warn.
MINIMUM_FREE_GB = 30


def local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


@dataclass(frozen=True)
class Install:
    """Every path an installation uses. Constructed once and passed around."""

    app_root: Path
    data_root: Path

    # ---------------------------------------------------------------- app-side
    @property
    def app_dir(self) -> Path:
        return self.app_root / "app"

    @property
    def runtime_dir(self) -> Path:
        """uv-managed CPython. UV_PYTHON_INSTALL_DIR points here so uninstall is complete;
        left to itself uv installs interpreters into a global per-user directory that would
        survive an uninstall and confuse the next one."""
        return self.app_root / "runtime"

    @property
    def cache_dir(self) -> Path:
        """UV_CACHE_DIR. Same reasoning as runtime_dir."""
        return self.app_root / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.app_root / "logs"

    @property
    def run_configs_dir(self) -> Path:
        """Generated config overlays.

        Deliberately OUTSIDE the code tree, so the extracted release stays byte-identical to
        the archive it came from. The overlays carry absolute paths for exactly this reason
        — see drivers.config_overlay.
        """
        return self.app_root / "configs"

    @property
    def runs_dir(self) -> Path:
        return self.app_root / "runs"

    @property
    def settings_file(self) -> Path:
        return self.app_root / "settings.json"

    @property
    def manifest_file(self) -> Path:
        return self.app_root / "install_manifest.json"

    # ---------------------------------------------------------------- per release
    def env_dir(self, tag: str) -> Path:
        return self.app_root / "envs" / tag

    def python(self, tag: str) -> Path:
        return self.env_dir(tag) / "Scripts" / "python.exe"

    def code_dir(self, tag: str) -> Path:
        """The extracted release tree.

        The seven packages are installed editable FROM here, because pricemodeling anchors
        PROJECT_ROOT to its own parent directory (`config.py:15`). A regular install puts the
        package in site-packages, PROJECT_ROOT becomes site-packages, and `config/settings.yaml`
        is never found again.
        """
        return self.app_root / "code" / tag

    def store_dir(self, tag: str) -> Path:
        """Fitted models and reports, shared by every run of this release.

        Seeded from the release tree at install time, because `dispatch_model/reports/`
        contains `markup_model.json` — a TRACKED file the code READS for every projected year.
        Relocating that directory without seeding it makes `apply_markup` fall back to clipped
        SMC silently, so the user's prices differ from the reference with no error anywhere.
        """
        return self.app_root / "store" / tag

    # ---------------------------------------------------------------- data-side
    @property
    def data_dir(self) -> Path:
        return self.data_root / "data"

    @property
    def scratchpad_dir(self) -> Path:
        """Monte-Carlo weather cubes. `run_montecarlo.py` builds this path relative to its
        PROCESS cwd, so the junction must sit at `code/<tag>/dispatch_model/scratchpad` and
        that script must run with cwd = `code/<tag>/dispatch_model`."""
        return self.data_root / "scratchpad"

    @property
    def era5_cache_dir(self) -> Path:
        return self.data_root / "era5_cache"

    @property
    def weathergen_output_dir(self) -> Path:
        return self.data_root / "weathergen-output"

    @property
    def mastr_dir(self) -> Path:
        """Not relocatable: `registries/mastr.py` uses os.path.expanduser directly. Recorded
        so the disk estimate is honest and the uninstaller can offer to remove it."""
        return Path.home() / ".open-MaStR"


@dataclass(frozen=True)
class Junction:
    """One NTFS directory junction: a path inside the code tree that must resolve elsewhere.

    Junctions rather than symlinks because a junction needs no elevation and no Developer
    Mode; a directory symlink on Windows needs one or the other.
    """

    link: Path            # inside code/<tag>/ - must NOT exist when the junction is created
    target: Path          # under the data root - created first
    why: str


def junctions_for(install: Install, tag: str) -> list[Junction]:
    """Every junction an installed release needs.

    Note what is absent: `*/reports` and `*/models`. Those hold files that ship with the
    release, so they are seeded into `store/<tag>` and reached through a config overlay
    instead — a junction would hide them.
    """
    code = install.code_dir(tag)
    return [
        Junction(
            link=code / "data",
            target=install.data_dir,
            why="pricemodeling anchors data/ to PROJECT_ROOT and offers no override; "
                "26 GB cannot live inside the code tree",
        ),
        Junction(
            link=code / "weathergen" / "output",
            target=install.weathergen_output_dir,
            why="simulation.nc is 1.3 GB and config-anchored to the code tree",
        ),
        Junction(
            link=code / "dispatch_model" / "scratchpad",
            target=install.scratchpad_dir,
            why="Monte-Carlo weather cubes, 472 MB per retained draw, cwd-anchored",
        ),
        Junction(
            link=code / "res_model" / "era5_cache",
            target=install.era5_cache_dir,
            why="raw ARCO/CDS download cache, config-anchored to the code tree",
        ),
    ]


#: Files that ship inside the release and must be copied into `store/<tag>` before any run.
#: Verified against `git ls-files` on the release: these are the only tracked files living
#: inside a directory the product relocates.
SEEDED_FROM_RELEASE: tuple[str, ...] = (
    "dispatch_model/reports/markup_model.json",
    "availability_model/reports/methodology.md",
)


def is_sync_root(path: Path) -> str | None:
    """Whether a path sits inside a cloud-sync folder. Returns the provider, or None.

    A 16.5 GB SQLite database under active write inside a sync root is a corruption and a
    re-upload hazard, and the sync client's file handles break directory operations — this
    project hit exactly that while moving its own repository out of OneDrive.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    parts_lower = [p.lower() for p in resolved.parts]

    for env_var, provider in (("OneDrive", "OneDrive"), ("OneDriveCommercial", "OneDrive"),
                              ("OneDriveConsumer", "OneDrive")):
        root = os.environ.get(env_var)
        if root:
            try:
                resolved.relative_to(Path(root).resolve())
                return provider
            except (ValueError, OSError):
                pass

    for marker, provider in (("onedrive", "OneDrive"), ("dropbox", "Dropbox"),
                             ("google drive", "Google Drive"), ("googledrive", "Google Drive"),
                             ("icloakdrive", "iCloud"), ("icloud drive", "iCloud"),
                             ("box sync", "Box")):
        if any(marker in part for part in parts_lower):
            return provider

    for parent in [resolved, *resolved.parents]:
        for marker, provider in ((".dropbox", "Dropbox"), (".icloud", "iCloud")):
            if (parent / marker).exists():
                return provider
    return None


def is_network_path(path: Path) -> bool:
    """UNC share or mapped network drive. SQLite over SMB is a documented corruption risk."""
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    drive = os.path.splitdrive(text)[0]
    if not drive or os.name != "nt":
        return False
    try:
        import ctypes

        # 4 == DRIVE_REMOTE
        return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") == 4
    except Exception:                                    # noqa: BLE001 - never block on this
        return False


def free_gb(path: Path) -> float | None:
    """Free space on the volume that would hold `path`, walking up to the first real parent."""
    import shutil

    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free / (1024 ** 3)
    except OSError:
        return None


@dataclass
class LocationVerdict:
    ok: bool
    blocking: list[str]
    warnings: list[str]
    free_gb: float | None


def check_data_location(path: Path) -> LocationVerdict:
    """Judge a candidate data directory before 26 GB goes into it."""
    blocking: list[str] = []
    warnings: list[str] = []

    if is_network_path(path):
        blocking.append(
            "This is a network location. The model writes a 16.5 GB SQLite database with "
            "many small transactions, and SQLite over a network share is a known corruption "
            "risk. Choose a local disk."
        )

    provider = is_sync_root(path)
    if provider:
        blocking.append(
            f"This folder is inside {provider}. A large database under constant write would be "
            f"re-uploaded endlessly, and the sync client keeps file handles open that break "
            f"directory operations. Choose a folder outside {provider}."
        )

    space = free_gb(path)
    if space is None:
        warnings.append("Could not read the free space on this drive.")
    elif space < MINIMUM_FREE_GB:
        blocking.append(
            f"Only {space:.0f} GB free. A full rebuild needs about {REQUIRED_FREE_GB} GB "
            f"(26 GB of databases, plus caches and model output)."
        )
    elif space < REQUIRED_FREE_GB:
        warnings.append(
            f"{space:.0f} GB free, and a full rebuild wants about {REQUIRED_FREE_GB} GB. "
            f"You can start, but the ingest may run out of room."
        )

    if any(ord(ch) > 127 for ch in str(path)):
        warnings.append(
            "This path contains accented or non-ASCII characters. That is supported, but it "
            "is a less-travelled path through the toolchain - tell us if anything misbehaves."
        )

    return LocationVerdict(not blocking, blocking, warnings, space)


def default_install() -> Install:
    root = local_appdata() / APP_FOLDER_NAME
    return Install(app_root=root, data_root=root / "data-root")
