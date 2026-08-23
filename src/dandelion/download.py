"""Resumable, verified downloads.

The installer fetches a 1.2 MB code archive and, if the user wants them, 1.3 GB of fitted
models. On a domestic connection the second one is long enough that closing the laptop is a
realistic event, so downloads resume from where they stopped rather than starting again.

Every download is checked against a sha256 published in the release manifest. A truncated or
corrupted file that merely *looks* complete is the failure mode worth engineering against:
it surfaces hours later as an unreadable model rather than as a download error.
"""
from __future__ import annotations

import hashlib
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "Dandelion-Studio-Installer"

#: A partial file gets this suffix until it is complete and verified, so an interrupted
#: download can never be mistaken for a finished one.
PARTIAL_SUFFIX = ".partial"

CHUNK = 1 << 20


@dataclass
class DownloadProgress:
    name: str
    downloaded: int
    total: int | None
    resumed_from: int
    bytes_per_second: float

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return min(1.0, self.downloaded / self.total)


class DownloadError(RuntimeError):
    """Raised with a message meant for a user, not a developer."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _open(url: str, offset: int, timeout: float):
    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - https URLs we build


def download(url: str, dest: Path, *, sha256: str | None = None,
             on_progress: Callable[[DownloadProgress], None] | None = None,
             timeout: float = 60.0, attempts: int = 5) -> Path:
    """Fetch `url` to `dest`, resuming and retrying. Returns `dest`.

    If `dest` already exists and matches `sha256`, nothing is downloaded — which is what makes
    re-running the wizard after an interruption cheap.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and sha256:
        if sha256_file(dest) == sha256:
            return dest
        dest.unlink()                                   # present but wrong: start over

    partial = dest.with_name(dest.name + PARTIAL_SUFFIX)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        offset = partial.stat().st_size if partial.exists() else 0
        try:
            with _open(url, offset, timeout) as response:
                # A server that ignores Range replies 200 and sends the whole file; starting
                # from zero is then the only correct action, and appending would corrupt.
                if offset and response.status != 206:
                    offset = 0
                    partial.unlink(missing_ok=True)

                declared = response.headers.get("Content-Length")
                total = (int(declared) + offset) if declared and declared.isdigit() else None

                mode = "ab" if offset else "wb"
                started = time.monotonic()
                downloaded = offset
                with partial.open(mode) as fh:
                    while True:
                        block = response.read(CHUNK)
                        if not block:
                            break
                        fh.write(block)
                        downloaded += len(block)
                        if on_progress:
                            elapsed = max(time.monotonic() - started, 1e-6)
                            on_progress(DownloadProgress(
                                name=dest.name, downloaded=downloaded, total=total,
                                resumed_from=offset,
                                bytes_per_second=(downloaded - offset) / elapsed))
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
            if attempt == attempts:
                raise DownloadError(_explain(url, exc, partial)) from exc
            time.sleep(min(2 ** attempt, 30))
    else:                                                # pragma: no cover - loop always breaks
        raise DownloadError(_explain(url, last_error, partial))

    if sha256:
        actual = sha256_file(partial)
        if actual != sha256:
            partial.unlink(missing_ok=True)
            raise DownloadError(
                f"{dest.name} downloaded but its checksum does not match what the release "
                f"published, so the file is corrupt or was tampered with. Expected "
                f"{sha256[:16]}…, got {actual[:16]}…. The partial file has been discarded; "
                f"trying again is safe."
            )

    partial.replace(dest)
    return dest


def _explain(url: str, exc: Exception | None, partial: Path) -> str:
    got = partial.stat().st_size if partial.exists() else 0
    where = f" {got / 1e6:.1f} MB were saved and will be reused." if got else ""
    reason = str(exc) if exc else "unknown error"
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
        return (f"Not found at {url}. The release may not have been published yet, or the "
                f"file name differs from what this version of the installer expects.")
    if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
        return (f"Access denied for {url}. If the releases repository is private, the "
                f"installer cannot read it - it carries no credentials by design.")
    return (f"Could not download {url}: {reason}.{where} Check the network connection, or a "
            f"proxy or firewall that might be blocking github.com.")


def free_space_for(path: Path) -> int | None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None
