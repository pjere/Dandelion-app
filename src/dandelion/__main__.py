"""Entry point for the shipped binary.

One executable, two modes: with no install manifest under %LOCALAPPDATA%\\Dandelion\\ this is
the Setup Wizard; with one, it is Studio. Phase 2 builds both; today this is the shell that
proves the packaging works and pins the two things that must happen before anything else.

DO NOT reorder the top of main(). `freeze_support()` must run before any other import-time
work that could spawn a process: NiceGUI's native mode starts its window through
multiprocessing, and in a frozen executable each spawned child re-runs this module. Without
freeze_support the child re-enters main(), spawns another child, and the exe forks until the
machine gives up.
"""
from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path

from dandelion.branding import DISCLAIMER_VERSION, PRODUCT_NAME, disclaimer_sha256

__version__ = "0.0.1"

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Dandelion"
INSTALL_MANIFEST = APP_DIR / "install_manifest.json"


def webview2_present() -> bool:
    """Whether the Edge WebView2 runtime is installed.

    NiceGUI's native window is a pywebview host, which on Windows needs WebView2. It ships
    with Windows 11 but is not guaranteed on Windows 10, so the app checks rather than
    crashing with a blank window. Registry only - never installs anything.
    """
    if sys.platform != "win32":
        return False
    import winreg

    keys = (
        r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
        r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
        r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    )
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in keys:
            try:
                with winreg.OpenKey(root, key) as handle:
                    version, _ = winreg.QueryValueEx(handle, "pv")
                    if version and version != "0.0.0.0":
                        return True
            except OSError:
                continue
    return False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--version" in argv:
        print(f"{PRODUCT_NAME} {__version__}")
        return 0

    if "--self-check" in argv:
        # What the release build asserts about a frozen binary before publishing it.
        print(f"{PRODUCT_NAME} {__version__}")
        print(f"frozen           : {getattr(sys, 'frozen', False)}")
        print(f"python           : {sys.version.split()[0]}")
        print(f"app dir          : {APP_DIR}")
        print(f"install manifest : "
              f"{'present' if INSTALL_MANIFEST.is_file() else 'absent -> Setup Wizard'}")
        print(f"webview2         : "
              f"{'present' if webview2_present() else 'ABSENT -> browser fallback'}")
        print(f"disclaimer       : v{DISCLAIMER_VERSION}  sha256 {disclaimer_sha256()[:16]}")
        return 0

    if "--uninstall" in argv:
        from dandelion.uninstall_cli import run as run_uninstall

        return run_uninstall(quiet="--quiet" in argv)

    if "--headless-check" in argv:
        # Builds the interface and exits. This is what proves a frozen binary carries
        # NiceGUI's static assets: they are loaded when the page is constructed, so a
        # missing bundle fails here rather than as a blank window on a user's machine.
        from dandelion.studio import run as run_studio
        from dandelion.wizard import run as run_wizard

        run_wizard(headless_check=True)
        if INSTALL_MANIFEST.is_file():
            run_studio(headless_check=True)
        print("interface built OK")
        return 0

    force = None
    if "--browser" in argv:
        force = "browser"
    elif "--native" in argv:
        force = "native"

    if INSTALL_MANIFEST.is_file():
        from dandelion.studio import run as run_studio

        return run_studio(force_mode=force)

    from dandelion.wizard import run as run_wizard

    return run_wizard(force_mode=force)


if __name__ == "__main__":
    # Must be the first thing that happens in a frozen process. See the module docstring.
    multiprocessing.freeze_support()
    raise SystemExit(main())
