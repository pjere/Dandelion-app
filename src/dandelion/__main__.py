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
        print(f"Dandelion {__version__}")
        return 0

    if "--self-check" in argv:
        # What the release build asserts about a frozen binary before publishing it.
        print(f"Dandelion {__version__}")
        print(f"frozen           : {getattr(sys, 'frozen', False)}")
        print(f"python           : {sys.version.split()[0]}")
        print(f"app dir          : {APP_DIR}")
        print(f"install manifest : "
              f"{'present' if INSTALL_MANIFEST.is_file() else 'absent -> Setup Wizard'}")
        print(f"webview2         : "
              f"{'present' if webview2_present() else 'ABSENT -> browser fallback'}")
        return 0

    mode = "Studio" if INSTALL_MANIFEST.is_file() else "Setup Wizard"
    print(f"Dandelion {__version__} would start: {mode}")
    print("(the interface is built in Phase 2)")
    return 0


if __name__ == "__main__":
    # Must be the first thing that happens in a frozen process. See the module docstring.
    multiprocessing.freeze_support()
    raise SystemExit(main())
