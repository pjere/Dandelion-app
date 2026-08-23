"""The window: a native one where Windows can host it, a browser tab where it cannot.

NiceGUI's native mode runs the page inside pywebview, which on Windows needs the Edge WebView2
runtime. That ships with Windows 11 and with recent Windows 10, but it is not guaranteed — and
a missing runtime produces a blank window rather than an error, which is the worst possible
symptom. So the runtime is checked before the window is opened, and its absence downgrades to
serving on localhost and opening the default browser.

Nothing is served beyond the loopback interface, in either mode.
"""
from __future__ import annotations

import socket
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowMode:
    native: bool
    reason: str


def webview2_present() -> bool:
    """Whether the Edge WebView2 runtime is installed, from the registry only."""
    if sys.platform != "win32":
        return False
    import winreg

    client = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    locations = (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{client}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{client}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{client}"),
    )
    for root, key in locations:
        try:
            with winreg.OpenKey(root, key) as handle:
                version, _ = winreg.QueryValueEx(handle, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def choose_window_mode(force: str | None = None) -> WindowMode:
    """Native or browser, decided on what the machine actually has.

    `force` accepts "native" or "browser" so a user whose window misbehaves has a way out
    without reinstalling.
    """
    if force == "browser":
        return WindowMode(False, "asked for a browser window")
    if force == "native":
        return WindowMode(True, "asked for a native window")
    if sys.platform != "win32":
        return WindowMode(False, "not Windows")
    if not webview2_present():
        return WindowMode(
            False,
            "the Edge WebView2 runtime is not installed, so a native window would open blank",
        )
    return WindowMode(True, "WebView2 is available")


def free_port(preferred: int = 8712) -> int:
    """A loopback port, preferring a stable one so a reopened browser tab still works."""
    for candidate in (preferred, preferred + 1, preferred + 2):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
