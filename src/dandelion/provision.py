"""Turn an empty folder into a working Dandelion installation.

This is the part of the product that has to be exactly right, because everything else assumes
it worked. It reproduces, on a stranger's machine with no Python and no admin rights, the
environment `release_code.py` qualified on the owner's.

Two constraints shape the order of operations, both discovered in Phase 0:

**Junctions before invocations.** `pricemodeling.load_settings()` calls `ensure_dirs()`, so the
first upstream command of any kind creates `data/` and `data/raw/` inside the code tree. NTFS
refuses to create a junction where a directory already exists, so a single early invocation
would poison the install. Junctions are therefore created immediately after extraction and
before anything is run.

**Editable installs, in ADR-8 order.** `pricemodeling` derives PROJECT_ROOT from its own file
location, so a regular install relocates the whole project to site-packages and
`config/settings.yaml` is never found again. Verified: a non-editable install makes
`load_settings()` raise FileNotFoundError.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dandelion.download import DownloadError, download, sha256_file  # noqa: E402
from dandelion.paths import SEEDED_FROM_RELEASE, Install, junctions_for  # noqa: E402
from drivers.inventory import CONSOLE_SCRIPTS, INSTALL_ORDER  # noqa: E402

#: Decision D3: the code archive is published on a PUBLIC repository, because the installer
#: runs on machines we do not control and can therefore carry no credential. Any token baked
#: into a distributed binary is a published token.
RELEASE_REPO = "pjere/Dandelion-app"
RELEASE_BASE = f"https://github.com/{RELEASE_REPO}/releases/download"

PYTHON_VERSION = "3.12"


class ProvisionError(RuntimeError):
    """Carries a message written for the person running the installer."""


@dataclass
class Step:
    key: str
    title: str
    detail: str = ""
    ok: bool | None = None


@dataclass
class Report:
    tag: str
    steps: list[Step] = field(default_factory=list)

    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if s.ok is False]


Listener = Callable[[Step], None]


def _run(argv: list[str], *, cwd: Path | None = None, env: dict | None = None,
         timeout: int = 3600) -> tuple[int, str]:
    proc = subprocess.run(
        argv, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True,
        timeout=timeout, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def find_uv() -> list[str]:
    """The pinned uv, bundled beside the executable when frozen.

    Bundling matters: uv is what provisions CPython, so it cannot itself depend on a Python
    being present. In a source checkout we fall back to whatever uv is available.
    """
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "uv.exe"
        if bundled.is_file():
            return [str(bundled)]
    found = shutil.which("uv")
    if found:
        return [found]
    rc, _ = _run([sys.executable, "-m", "uv", "--version"], timeout=60)
    if rc == 0:
        return [sys.executable, "-m", "uv"]
    raise ProvisionError(
        "The environment manager (uv) is missing from this installation. Reinstall "
        "Dandelion Studio; if that does not help, the download may have been incomplete."
    )


def uv_env(install: Install) -> dict:
    """uv's environment, redirected so an uninstall is actually complete.

    Left to its own devices uv puts interpreters and its cache in global per-user directories
    that survive uninstalling and confuse the next install.
    """
    env = dict(os.environ)
    env["UV_PYTHON_INSTALL_DIR"] = str(install.runtime_dir)
    env["UV_CACHE_DIR"] = str(install.cache_dir)
    env["UV_NO_MODIFY_PATH"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


# --------------------------------------------------------------------------------------
# the steps
# --------------------------------------------------------------------------------------

def prepare_directories(install: Install, tag: str) -> None:
    for path in (install.app_dir, install.runtime_dir, install.cache_dir, install.logs_dir,
                 install.runs_dir, install.env_dir(tag).parent, install.code_dir(tag).parent,
                 install.store_dir(tag), install.data_root):
        path.mkdir(parents=True, exist_ok=True)


def provision_python(install: Install, uv: list[str]) -> str:
    rc, out = _run([*uv, "python", "install", PYTHON_VERSION],
                   env=uv_env(install), timeout=1800)
    if rc != 0:
        raise ProvisionError(
            f"Could not install Python {PYTHON_VERSION}.\n\n{out.strip()[-600:]}\n\n"
            "This step downloads from github.com. If you are behind a corporate proxy, set "
            "HTTPS_PROXY in your environment and run the installer again."
        )
    return out.strip().splitlines()[-1] if out.strip() else f"Python {PYTHON_VERSION}"


def create_venv(install: Install, tag: str, uv: list[str]) -> None:
    env_dir = install.env_dir(tag)
    if env_dir.exists():
        shutil.rmtree(env_dir, ignore_errors=True)
    rc, out = _run([*uv, "venv", "--python", PYTHON_VERSION, str(env_dir)],
                   env=uv_env(install), timeout=900)
    if rc != 0:
        raise ProvisionError(f"Could not create the Python environment.\n\n{out.strip()[-600:]}")


def fetch_release(install: Install, tag: str, *, on_progress=None,
                  local_archive: Path | None = None,
                  local_manifest: Path | None = None) -> Path:
    """Download and extract the code archive. Returns the extracted tree.

    `local_archive` installs from a file already on disk instead of downloading. That serves
    two purposes: rehearsing the installer before a release is published, and an eventual
    offline install from media.
    """
    archive_name = f"dandelion-code-{tag}.zip"
    archive = install.app_dir / archive_name

    expected: str | None = None
    manifest_path = local_manifest or install.app_dir / f"code_manifest-{tag}.json"
    if local_manifest is None and local_archive is None:
        try:
            download(f"{RELEASE_BASE}/{tag}/code_manifest.json", manifest_path)
        except DownloadError:
            manifest_path = None                        # verified below by its absence
    if manifest_path and Path(manifest_path).is_file():
        try:
            expected = json.loads(Path(manifest_path).read_text(encoding="utf-8")).get(
                "archive_sha256")
        except (json.JSONDecodeError, OSError):
            expected = None

    if local_archive is not None:
        if not local_archive.is_file():
            raise ProvisionError(f"No code archive at {local_archive}.")
        if expected and sha256_file(local_archive) != expected:
            raise ProvisionError(
                f"{local_archive.name} does not match the checksum in its manifest, so it is "
                f"corrupt or does not belong to {tag}."
            )
        shutil.copy2(local_archive, archive)
    else:
        try:
            download(f"{RELEASE_BASE}/{tag}/{archive_name}", archive, sha256=expected,
                     on_progress=on_progress)
        except DownloadError as exc:
            raise ProvisionError(str(exc)) from exc

    code_dir = install.code_dir(tag)
    if code_dir.exists():
        shutil.rmtree(code_dir, ignore_errors=True)
    code_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        prefix = f"dandelion-{tag}/"
        if not names or not all(n.startswith(prefix) for n in names):
            raise ProvisionError(
                f"{archive_name} does not look like a Dandelion code release: its contents "
                f"are not under {prefix}."
            )
        zf.extractall(code_dir.parent / f"_extract-{tag}")

    extracted = code_dir.parent / f"_extract-{tag}" / f"dandelion-{tag}"
    for item in extracted.iterdir():
        shutil.move(str(item), str(code_dir / item.name))
    shutil.rmtree(code_dir.parent / f"_extract-{tag}", ignore_errors=True)

    if not (code_dir / "config" / "settings.yaml").is_file():
        raise ProvisionError("The extracted release is missing config/settings.yaml.")
    return code_dir


def create_junctions(install: Install, tag: str) -> list[str]:
    """Bridge the code tree to the data root. MUST run before any upstream invocation."""
    made: list[str] = []
    for junction in junctions_for(install, tag):
        junction.target.mkdir(parents=True, exist_ok=True)
        junction.link.parent.mkdir(parents=True, exist_ok=True)

        if junction.link.exists():
            # An empty directory here is the ensure_dirs() footprint: safe to remove. A
            # non-empty one means somebody put data in the code tree, and guessing would be
            # worse than stopping.
            if junction.link.is_dir() and not any(junction.link.iterdir()):
                junction.link.rmdir()
            else:
                raise ProvisionError(
                    f"{junction.link} already exists and is not empty, so the link to "
                    f"{junction.target} cannot be created. Remove or move it and try again."
                )

        rc, out = _run(["cmd", "/c", "mklink", "/J", str(junction.link), str(junction.target)],
                       timeout=120)
        if rc != 0 or not junction.link.exists():
            raise ProvisionError(
                f"Could not link {junction.link.name} to {junction.target}.\n\n"
                f"{out.strip()[-400:]}\n\nThis needs both paths on an NTFS drive. Network "
                f"drives and non-NTFS formats (exFAT, FAT32) cannot do it."
            )
        made.append(f"{junction.link.name} -> {junction.target}")
    return made


def seed_store(install: Install, tag: str) -> list[str]:
    """Copy the fitted artifacts that ship inside the release into the shared store.

    `dispatch_model/reports/markup_model.json` is the one that matters: the code loads it for
    every projected year, and without it `apply_markup` silently falls back to clipped SMC.
    """
    code_dir = install.code_dir(tag)
    store = install.store_dir(tag)
    seeded: list[str] = []
    for relative in SEEDED_FROM_RELEASE:
        source = code_dir / relative
        if not source.is_file():
            raise ProvisionError(
                f"The release is missing {relative}, which the model reads at run time. "
                f"This release cannot be installed."
            )
        destination = store / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        seeded.append(f"{relative} ({sha256_file(destination)[:12]})")
    return seeded


def install_dependencies(install: Install, tag: str, uv: list[str], lock: Path) -> str:
    rc, out = _run([*uv, "pip", "install", "--python", str(install.python(tag)),
                    "-r", str(lock)], env=uv_env(install), timeout=3600)
    if rc != 0:
        raise ProvisionError(
            f"Could not install the model's dependencies.\n\n{out.strip()[-800:]}\n\n"
            "This step downloads from pypi.org."
        )
    return f"{count_pinned(lock)} packages"


def clear_download_cache(install: Install) -> int:
    """Drop uv's download cache. Returns bytes it occupied on disk before removal.

    Safe, and measured: uv HARDLINKS packages from its cache into the environment, so removing
    the cache leaves every import working — verified by re-running the console scripts and
    importing the whole stack afterwards. It also reclaims less than its apparent size, because
    most of those bytes are shared with the environment and only stop being counted twice.

    Measured on a v0.1.0 install: cache 998 MB apparent, environment 989 MB real, total on
    disk 1.1 GB either way.
    """
    if not install.cache_dir.exists():
        return 0
    occupied = sum(f.stat().st_size for f in install.cache_dir.rglob("*") if f.is_file())
    shutil.rmtree(install.cache_dir, ignore_errors=True)
    return occupied


def count_pinned(lock: Path) -> int:
    """Distributions pinned by a lock file.

    `uv pip compile` writes indented `# via ...` provenance comments under each pin, so the
    line must be stripped before testing for a comment — otherwise the count triples.
    """
    total = 0
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith(("#", "-")):
            total += 1
    return total


def install_packages(install: Install, tag: str, uv: list[str],
                     listener: Listener | None = None) -> None:
    """The seven packages, editable, in dependency order."""
    code_dir = install.code_dir(tag)
    for package in INSTALL_ORDER:
        rc, out = _run([*uv, "pip", "install", "--python", str(install.python(tag)),
                        "--no-deps", "-e", str(code_dir / package)],
                       env=uv_env(install), timeout=900)
        if rc != 0:
            raise ProvisionError(
                f"Could not install {package}.\n\n{out.strip()[-600:]}"
            )
        if listener:
            listener(Step(key=f"install:{package}", title=f"Installed {package}", ok=True))


def verify(install: Install, tag: str) -> list[Step]:
    """Prove the install works before telling the user it does."""
    python = install.python(tag)
    code_dir = install.code_dir(tag)
    steps: list[Step] = []

    rc, out = _run([str(python), "-c",
                    "import pricemodeling.config as c; print(c.PROJECT_ROOT)"], timeout=300)
    anchored = rc == 0 and out.strip() and Path(out.strip()) == code_dir.resolve()
    steps.append(Step("anchor", "Model resolves its own project root",
                      f"{out.strip() or rc}", bool(anchored)))

    rc, out = _run([str(python), "-c",
                    "from pricemodeling.config import load_settings as l; print(l().db_path)"],
                   cwd=code_dir, timeout=300)
    steps.append(Step("settings", "Model configuration loads", out.strip()[:160], rc == 0))

    # The junction is only proven by writing through it.
    data_marker = code_dir / "data" / ".dandelion-write-test"
    try:
        data_marker.parent.mkdir(parents=True, exist_ok=True)
        data_marker.write_text("ok", encoding="utf-8")
        through = (install.data_dir / ".dandelion-write-test").is_file()
        data_marker.unlink(missing_ok=True)
        (install.data_dir / ".dandelion-write-test").unlink(missing_ok=True)
    except OSError as exc:
        through = False
        steps.append(Step("junction-io", "Data folder is writable", str(exc), False))
    else:
        steps.append(Step("junction-io", "Data folder is writable through the link",
                          str(install.data_dir), through))

    for name in CONSOLE_SCRIPTS:
        exe = install.env_dir(tag) / "Scripts" / f"{name}.exe"
        steps.append(Step(f"script:{name}", f"Command '{name}' available", str(exe),
                          exe.is_file()))

    for module, distribution in (("cdsapi", "cdsapi"), ("entsoe", "entsoe-py"),
                                 ("open_mastr", "open-mastr")):
        rc, _ = _run([str(python), "-c", f"import {module}"], timeout=300)
        steps.append(Step(f"import:{module}", f"Data connector '{distribution}' installed",
                          "", rc == 0))

    for relative in SEEDED_FROM_RELEASE:
        seeded = install.store_dir(tag) / relative
        steps.append(Step(f"seed:{Path(relative).name}",
                          f"Fitted input {Path(relative).name} in place",
                          str(seeded), seeded.is_file()))
    return steps


# --------------------------------------------------------------------------------------

def provision(install: Install, tag: str, *, listener: Listener | None = None,
              on_download=None, lock: Path | None = None,
              local_archive: Path | None = None,
              local_manifest: Path | None = None) -> Report:
    """Run the whole sequence. Raises ProvisionError with a user-readable message."""
    report = Report(tag=tag)

    def emit(step: Step) -> Step:
        report.steps.append(step)
        if listener:
            listener(step)
        return step

    uv = find_uv()

    emit(Step("dirs", "Preparing folders", str(install.app_root), True))
    prepare_directories(install, tag)

    detail = provision_python(install, uv)
    emit(Step("python", f"Installed Python {PYTHON_VERSION}", detail, True))

    create_venv(install, tag, uv)
    emit(Step("venv", "Created the model environment", str(install.env_dir(tag)), True))

    code_dir = fetch_release(install, tag, on_progress=on_download,
                             local_archive=local_archive, local_manifest=local_manifest)
    emit(Step("code", f"Downloaded and extracted {tag}", str(code_dir), True))

    made = create_junctions(install, tag)
    emit(Step("junctions", "Linked the data folders", "; ".join(made), True))

    seeded = seed_store(install, tag)
    emit(Step("seed", "Placed the fitted inputs", "; ".join(seeded), True))

    lock_path = lock or install.app_dir / f"constraints-{tag}.lock"
    if lock is None:
        try:
            download(f"{RELEASE_BASE}/{tag}/constraints.lock", lock_path)
        except DownloadError as exc:
            raise ProvisionError(str(exc)) from exc
    detail = install_dependencies(install, tag, uv, lock_path)
    emit(Step("deps", "Installed the model's dependencies", detail, True))

    install_packages(install, tag, uv, listener)
    emit(Step("packages", "Installed the seven model packages", "", True))

    checks = verify(install, tag)
    for step in checks:
        emit(step)

    failed = [s for s in checks if s.ok is False]
    if failed:
        raise ProvisionError(
            "The installation completed but did not pass its own checks:\n\n"
            + "\n".join(f"  - {s.title}: {s.detail}" for s in failed)
        )
    return report
