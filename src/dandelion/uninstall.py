"""Remove Dandelion Studio, and be precise about what "remove" means.

An installation is three quite different things sitting under one folder: the software (about
1.1 GB, trivially reinstalled), the databases (up to 26 GB that took a night and the user's own
API quota to build), and their own work in `runs/`. Deleting all three because someone clicked
Uninstall would be indefensible, so each is asked about separately and the two irreplaceable
ones default to *keep*.

The dangerous detail is junctions. `code/<tag>/data` is a link to the data root, so a careless
recursive delete could walk through it and take the databases with it. Measured on Windows:
`shutil.rmtree` does not follow junctions, and the target survives. But `os.path.islink()`
returns **False** for a junction while `os.path.isjunction()` returns True — so anything that
tests the wrong one treats a link as a real directory. Links are therefore removed explicitly,
first, before any recursive delete runs near them.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dandelion.paths import Install

#: Where Windows lists installed programs for the current user. Per-user because the whole
#: product installs without administrator rights.
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\DandelionStudio"


@dataclass
class Removal:
    """One thing the uninstaller can remove, and whether it should by default."""

    key: str
    label: str
    path: Path
    why: str
    bytes: int = 0
    remove_by_default: bool = True
    #: Irreplaceable things are never pre-ticked, whatever their size.
    irreplaceable: bool = False


@dataclass
class UninstallReport:
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    freed_bytes: int = 0


def directory_size(path: Path) -> int:
    """Bytes under `path`, never following a junction into someone else's data."""
    if not path.exists():
        return 0
    total = 0
    for root, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not is_link(Path(root) / d)]
        for name in filenames:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def is_link(path: Path) -> bool:
    """A junction or a symlink. `islink` alone misses junctions, which is the whole problem."""
    try:
        return os.path.isjunction(path) or path.is_symlink()
    except OSError:
        return False


def remove_link(path: Path) -> bool:
    """Delete a junction itself, never its target."""
    if not is_link(path):
        return False
    try:
        os.rmdir(path)                                   # on a junction this drops the link
        return True
    except OSError:
        try:
            path.unlink()
            return True
        except OSError:
            return False


def remove_junctions(install: Install, manifest=None) -> list[str]:
    """Drop every link before anything recursive runs. Returns what was removed."""
    from dandelion.paths import junctions_for

    removed: list[str] = []
    candidates: list[Path] = []

    if manifest is not None and getattr(manifest, "junctions", None):
        candidates = [Path(link) for link in manifest.junctions]
    else:
        code_root = install.app_root / "code"
        if code_root.is_dir():
            for tag_dir in code_root.iterdir():
                if tag_dir.is_dir():
                    candidates += [j.link for j in junctions_for(install, tag_dir.name)]

    for link in candidates:
        if remove_link(link):
            removed.append(str(link))
    return removed


def software_paths(install: Install) -> list[Path]:
    """The parts of the app root that are *only* the program.

    Deliberately enumerated rather than "the app root": `runs/` lives there and so, by
    default, does the data root. Removing the parent would take the user's work with it
    whatever they answered, which is the one outcome an uninstaller must never produce.
    """
    return [
        install.app_dir, install.runtime_dir, install.cache_dir,
        install.app_root / "envs", install.app_root / "code", install.app_root / "store",
        install.logs_dir, install.manifest_file, install.settings_file,
        install.app_root / "wizard_state.json",
    ]


def plan(install: Install, manifest=None) -> list[Removal]:
    """Everything that could go, in the order it is offered."""
    tag = getattr(getattr(manifest, "code", None), "tag", "") or ""
    software_bytes = sum(directory_size(p) if p.is_dir() else
                         (p.stat().st_size if p.is_file() else 0)
                         for p in software_paths(install))

    items = [
        Removal(
            key="software", label="The program and its Python environment",
            path=install.app_root, bytes=software_bytes,
            why="Reinstalled in about eight minutes from the same download.",
            remove_by_default=True,
        ),
        Removal(
            key="data", label="The databases you built",
            path=install.data_root, bytes=directory_size(install.data_root),
            why=("Hours of downloading against your own API quotas. Keep these and a future "
                 "install can use them immediately."),
            remove_by_default=False, irreplaceable=True,
        ),
        Removal(
            key="runs", label="Your projection runs and their results",
            path=install.runs_dir, bytes=directory_size(install.runs_dir),
            why="Your own work: the assumptions you edited and the results they produced.",
            remove_by_default=False, irreplaceable=True,
        ),
        Removal(
            key="mastr", label="The German registry download",
            path=install.mastr_dir, bytes=directory_size(install.mastr_dir),
            why=("About 7 GB in your user profile. The model puts it there and cannot be told "
                 "otherwise, so it is listed separately - other tools may also use it."),
            remove_by_default=False,
        ),
    ]
    if tag:
        items[0].why += f" (release {tag})"
    return items


def remove_credentials() -> list[str]:
    """Forget every stored credential. The accounts themselves are untouched."""
    from dandelion import credentials

    forgotten: list[str] = []
    for credential in credentials.CREDENTIALS:
        for field_ in credential.fields:
            if credentials.load(field_.env) is not None:
                credentials.forget(field_.env)
                forgotten.append(field_.env)
    return forgotten


def execute(install: Install, choices: dict[str, bool], manifest=None,
            forget_credentials: bool = False) -> UninstallReport:
    """Carry out the chosen removals. Junctions go first, always."""
    report = UninstallReport()
    items = {item.key: item for item in plan(install, manifest)}

    dropped = remove_junctions(install, manifest)
    report.removed.extend(f"link {link}" for link in dropped)

    if forget_credentials:
        forgotten = remove_credentials()
        if forgotten:
            report.removed.append(f"{len(forgotten)} stored credential(s)")

    # Data and runs first: the data root often sits inside the app root, so tidying the app
    # root before honouring those answers would delete them regardless of what was chosen.
    for key in ("data", "runs", "mastr"):
        item = items.get(key)
        if item is None:
            continue
        if not choices.get(key, item.remove_by_default):
            report.kept.append(item.label)
            continue
        _remove(item.path, item, report)

    software = items.get("software")
    if software is not None:
        if choices.get("software", software.remove_by_default):
            for path in software_paths(install):
                _remove(path, software, report, quiet=True)
            report.removed.append(software.label)
            report.freed_bytes += software.bytes
            unregister()
            # Only if nothing the user kept is still inside it.
            try:
                if install.app_root.is_dir() and not any(install.app_root.iterdir()):
                    install.app_root.rmdir()
            except OSError:
                pass
        else:
            report.kept.append(software.label)
    return report


def _remove(path: Path, item: Removal, report: UninstallReport, quiet: bool = False) -> None:
    if not path.exists():
        return
    try:
        if path.is_dir():
            shutil.rmtree(path, onexc=_force_writable)
        else:
            path.unlink()
    except OSError as exc:
        report.failed.append(f"{item.label}: {exc}")
        return
    if not quiet:
        report.removed.append(item.label)
        report.freed_bytes += item.bytes


def _force_writable(func, path, _exc):
    """Read-only files (git objects, some caches) refuse to delete on Windows."""
    import stat

    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# Windows "Apps & features" entry
# --------------------------------------------------------------------------------------

def register(install: Install, *, app_version: str, executable: Path,
             product_name: str) -> bool:
    """List the product in Add/Remove Programs, under HKCU so no elevation is needed."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            values = {
                "DisplayName": product_name,
                "DisplayVersion": app_version,
                "InstallLocation": str(install.app_root),
                "UninstallString": f'"{executable}" --uninstall',
                "QuietUninstallString": f'"{executable}" --uninstall --quiet',
                "NoModify": 1,
                "NoRepair": 1,
                "EstimatedSize": max(1, directory_size(install.app_root) // 1024),
            }
            for name, value in values.items():
                kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, kind, value)
        return True
    except OSError:
        return False


def unregister() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        return True
    except OSError:
        return False


def is_registered() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY):
            return True
    except OSError:
        return False


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
