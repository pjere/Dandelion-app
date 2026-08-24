"""The uninstaller's interface: show exactly what will go, then ask.

Windows launches this from Apps & features, so it may be the only thing the user sees all
session. It therefore states sizes before asking, defaults the irreplaceable things to
*keep*, and requires a typed confirmation rather than a single click for the databases —
26 GB rebuilt overnight deserves more friction than a stray Enter key.

`--quiet` removes only the software, never data, never runs. That is what Windows passes for
an unattended removal, and unattended is precisely when destroying someone's work is least
excusable.
"""
from __future__ import annotations

import sys
from pathlib import Path

from dandelion import branding
from dandelion.manifest import InstallManifest
from dandelion.paths import Install, default_install
from dandelion.uninstall import execute, human, plan


def _load_install() -> tuple[Install, InstallManifest | None]:
    install = default_install()
    manifest = InstallManifest.load(install.manifest_file)
    if manifest and manifest.app_root:
        install = Install(app_root=Path(manifest.app_root),
                          data_root=Path(manifest.data_root or install.data_root))
    return install, manifest


def run(quiet: bool = False, stream=None) -> int:
    out = stream or sys.stdout
    install, manifest = _load_install()

    def say(text: str = "") -> None:
        print(text, file=out)

    if not install.app_root.exists():
        say(f"{branding.PRODUCT_NAME} does not appear to be installed at "
            f"{install.app_root}.")
        return 0

    items = plan(install, manifest)
    say(f"Uninstalling {branding.PRODUCT_NAME}")
    say(f"  {install.app_root}")
    say()

    if quiet:
        # Unattended: software only. Nothing irreplaceable goes without a person present.
        choices = {item.key: item.key == "software" for item in items}
        report = execute(install, choices, manifest)
        say(f"Removed the program ({human(report.freed_bytes)}).")
        for kept in report.kept:
            say(f"Kept: {kept}")
        return 0

    say("Choose what to remove. Anything you keep can be used by a future installation.")
    say()
    choices: dict[str, bool] = {}
    for item in items:
        if not item.path.exists() or (item.bytes == 0 and item.key != "software"):
            continue
        say(f"  {item.label}")
        say(f"    {item.path}")
        say(f"    {human(item.bytes)} - {item.why}")
        default = "Y/n" if item.remove_by_default else "y/N"
        answer = input(f"    Remove? [{default}] ").strip().lower()
        if not answer:
            choices[item.key] = item.remove_by_default
        else:
            choices[item.key] = answer.startswith("y")

        if choices[item.key] and item.irreplaceable:
            typed = input("    This cannot be undone. Type REMOVE to confirm: ").strip()
            if typed != "REMOVE":
                choices[item.key] = False
                say("    Kept.")
        say()

    forget = input("Forget the stored API credentials? Your accounts are untouched. [Y/n] ")
    forget_credentials = not forget.strip().lower().startswith("n")

    report = execute(install, choices, manifest, forget_credentials=forget_credentials)

    say()
    for entry in report.removed:
        say(f"Removed  {entry}")
    for entry in report.kept:
        say(f"Kept     {entry}")
    for entry in report.failed:
        say(f"FAILED   {entry}")
    say()
    say(f"Freed {human(report.freed_bytes)}.")
    if report.failed:
        say("Some items could not be removed - close any program using them and run this "
            "again.")
        return 1
    return 0
