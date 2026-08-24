"""Download the owner's fitted models, or decline and fit them yourself.

These are parameters, not data: the owner's own work, derived from public sources but not a
redistribution of them. Downloading them does two things beyond saving time — it makes a
user's projections match the reference model rather than merely resemble it, and it removes
the only reason most users need a Copernicus account at all, because nothing then pulls ERA5.

They are pinned to a code tag. The serialized objects name their own classes, so a fit from
one release will not load in another; there is no "latest fits".
"""
from __future__ import annotations

import json
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dandelion.download import DownloadError, download, sha256_file

#: The one thing a user gives up by not downloading them.
FIT_YOURSELF_COST = (
    "Fitting the weather generator yourself needs a Copernicus account, downloads about "
    "4 GB of ERA5, and takes several hours. Your results will also differ slightly from the "
    "reference model, because they are calibrated on the data you downloaded rather than on "
    "the owner's."
)


class FitsError(RuntimeError):
    """Carries a message written for the person running the installer."""


@dataclass
class FitsPlan:
    tag: str
    chunks: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    raw_bytes: int = 0

    @property
    def download_bytes(self) -> int:
        return sum(int(c.get("bytes", 0)) for c in self.chunks)


def fetch_plan(base_url: str, tag: str, into: Path) -> FitsPlan:
    """Read the published manifest so the user is told the real size before committing."""
    manifest_path = into / f"fits_manifest-{tag}.json"
    try:
        download(f"{base_url}/{tag}/fits_manifest.json", manifest_path)
    except DownloadError as exc:
        raise FitsError(
            f"Could not read the fitted-model listing for {tag}. {exc}"
        ) from exc
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FitsError("The fitted-model listing could not be read.") from exc

    if data.get("code_tag") != tag:
        raise FitsError(
            f"The published fitted models are for {data.get('code_tag')!r}, not {tag!r}. "
            f"A fit only loads in the release it was produced by."
        )
    return FitsPlan(tag=tag, chunks=list(data.get("chunks", [])),
                    artifacts=list(data.get("artifacts", [])),
                    raw_bytes=int(data.get("raw_bytes", 0)))


def download_fits(plan: FitsPlan, base_url: str, into: Path,
                  on_progress: Callable[[str, float | None, int, int], None] | None = None,
                  ) -> list[Path]:
    """Fetch every chunk, resuming and verifying. Returns the local chunk paths."""
    into.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    total = len(plan.chunks)

    for index, chunk in enumerate(plan.chunks, 1):
        name = chunk["name"]
        target = into / name

        def report(progress, _name=name, _i=index) -> None:
            if on_progress:
                on_progress(_name, progress.fraction, _i, total)

        try:
            download(f"{base_url}/{plan.tag}/{name}", target,
                     sha256=chunk.get("sha256"), on_progress=report)
        except DownloadError as exc:
            raise FitsError(str(exc)) from exc
        paths.append(target)
    return paths


def extract_fits(chunks: list[Path], code_dir: Path) -> list[str]:
    """Unpack the chunks into the release tree.

    Members are named `<package>/models/<file>`, and `models_dir` resolves relative to each
    package's config file — so extracting into the code tree puts every fit exactly where the
    model looks for it, with no configuration change at all.
    """
    import zstandard

    extracted: list[str] = []
    decompressor = zstandard.ZstdDecompressor()

    for chunk in chunks:
        with chunk.open("rb") as raw, decompressor.stream_reader(raw) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for member in tar:
                    if not _safe_member(member.name):
                        raise FitsError(
                            f"{chunk.name} contains an unexpected path ({member.name!r}) and "
                            f"was not unpacked."
                        )
                    tar.extract(member, code_dir, filter="data")
                    if member.isfile():
                        extracted.append(member.name)
    return extracted


def _safe_member(name: str) -> bool:
    """Only `<package>/models/...`, and nothing that escapes the tree."""
    parts = Path(name).parts
    if ".." in parts or Path(name).is_absolute():
        return False
    return len(parts) >= 3 and parts[1] == "models"


def verify_installed(plan: FitsPlan, code_dir: Path) -> list[str]:
    """Re-hash what landed on disk against the manifest. Returns the problems found."""
    problems: list[str] = []
    for artifact in plan.artifacts:
        package, name = artifact.get("package"), artifact.get("name")
        expected = artifact.get("sha256")
        if not (package and name):
            continue
        path = code_dir / package / "models" / name
        if not path.is_file():
            problems.append(f"{package}/models/{name} is missing")
        elif expected and sha256_file(path) != expected:
            problems.append(f"{package}/models/{name} does not match its published checksum")
    return problems


def already_installed(plan: FitsPlan, code_dir: Path) -> bool:
    return not verify_installed(plan, code_dir) and bool(plan.artifacts)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
