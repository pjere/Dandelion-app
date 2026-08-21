"""Package the owner's fitted model parameters for users to download.

The product ships no data — users rebuild the databases themselves with their own credentials.
But the *fits* are a different thing: they are the owner's own work, derived from public data
but not a redistribution of it, and they are what makes a user's projections match the
reference model rather than merely resemble it. Shipping them also removes hours of
calibration from every first run.

WHAT A COMPLETE FIT SET IS

This tool does not sweep `*/models` and hope. Each package declares exactly which artifacts
constitute its fit, because upstream's serializer (`powersim_core.serialize.save_params`)
writes a JSON tree plus a `.npz` sidecar *only when the payload contains arrays*, and deletes
a stale sidecar when it does not. So "is the sidecar required?" is answered by reading the
JSON for `__ndarray__` placeholders — and a fit whose JSON references arrays with no sidecar
beside it is **broken**, not merely small. That is not hypothetical: it is the state the
owner's weathergen fit was in when this tool was written.

    python release_tools/release_fits.py --source <checkout> --tag v0.1.0 --dry-run
    python release_tools/release_fits.py --source <checkout> --tag v0.1.0 --python <venv>
    python release_tools/release_fits.py --verify dist/fits-v0.1.0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

#: The placeholder `save_params` leaves in the JSON tree for every numpy array it moved out.
ARRAY_TAG = "__ndarray__"

#: Hard limit on one published file. GitHub refuses a release asset above 2 GB.
ASSET_LIMIT = 2_000_000_000

#: Raw bytes packed into one chunk. Compression only shrinks, so this normally keeps the
#: output well under ASSET_LIMIT - but a SINGLE artifact larger than this cannot be split,
#: and then the output size depends entirely on how well that one file compresses. The
#: weathergen sidecar is already 2.8 GB and doubled during a single afternoon, so the output
#: is checked explicitly rather than assumed.
CHUNK_BYTES = 1_500_000_000

#: Per package: the fitted artifacts the model code actually loads, by the name it loads them
#: under. Sidecars are derived, not listed. Verified against the load sites in each package.
FIT_SETS: dict[str, tuple[str, ...]] = {
    "weathergen": ("fitted.json", "wind100.json"),
    "demand_model": ("calibrated.json", "residual.json"),
    "res_model": ("calibrated_res.json", "residual_res.json", "wind_transfers.json"),
    "availability_model": ("calibrated_availability.json",),
}

#: Extra artifacts matched by pattern rather than exact name.
FIT_GLOBS: dict[str, tuple[str, ...]] = {
    "weathergen": ("cmip6_deltas_*.npz",),
}

#: Never shipped, whatever it weighs.
EXCLUDED_NAMES = {
    "_gauss_cache.npz": "referenced nowhere in the upstream tree - a stale cache",
}
EXCLUDED_SUFFIXES = {".bak", ".old", ".tmp"}
EXCLUDED_DIR_PREFIXES = ("backup",)

#: dispatch_model has no models/ directory: its one fitted artifact, markup_model.json, is
#: tracked and ships inside the code release. Shipping it here too would create two copies
#: with no rule for which wins.
NOT_PACKAGED = {
    "dispatch_model": "markup_model.json is tracked and ships with the code release",
}


@dataclass
class Artifact:
    package: str
    name: str
    bytes: int
    sha256: str = ""
    needs_sidecar: bool = False
    sidecar: str | None = None


@dataclass
class FitSet:
    package: str
    artifacts: list[Artifact] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(a.bytes for a in self.artifacts)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def references_arrays(json_path: Path) -> bool:
    """Whether this JSON tree points at arrays that must live in a .npz sidecar."""
    try:
        return ARRAY_TAG in json_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def is_excluded(path: Path) -> str | None:
    if path.name in EXCLUDED_NAMES:
        return EXCLUDED_NAMES[path.name]
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return f"{path.suffix} backup"
    for part in path.parts:
        if part.lower().startswith(EXCLUDED_DIR_PREFIXES):
            return f"inside {part}/"
    return None


def collect(source: Path, hash_files: bool = True) -> list[FitSet]:
    """Resolve every declared fit set against the owner's checkout."""
    out: list[FitSet] = []
    for package, names in FIT_SETS.items():
        fs = FitSet(package=package)
        models = source / package / "models"
        if not models.is_dir():
            fs.problems.append(f"no models directory at {package}/models")
            out.append(fs)
            continue

        wanted: list[Path] = [models / n for n in names]
        for pattern in FIT_GLOBS.get(package, ()):
            wanted += sorted(p for p in models.glob(pattern) if not is_excluded(p))

        for path in wanted:
            if not path.is_file():
                fs.problems.append(f"missing: {package}/models/{path.name}")
                continue
            why = is_excluded(path)
            if why:
                continue
            art = Artifact(package=package, name=path.name, bytes=path.stat().st_size)
            if path.suffix == ".json" and references_arrays(path):
                art.needs_sidecar = True
                sidecar = path.with_suffix(path.suffix + ".npz")
                if sidecar.is_file():
                    art.sidecar = sidecar.name
                else:
                    fs.problems.append(
                        f"BROKEN: {package}/models/{path.name} references arrays but "
                        f"{sidecar.name} is missing - this fit cannot be loaded"
                    )
                    continue
            if hash_files:
                art.sha256 = sha256_file(path)
            fs.artifacts.append(art)

            if art.sidecar:
                sc = models / art.sidecar
                out_art = Artifact(package=package, name=sc.name, bytes=sc.stat().st_size)
                if hash_files:
                    out_art.sha256 = sha256_file(sc)
                fs.artifacts.append(out_art)
        out.append(fs)
    return out


def verify_loadable(python: Path, source: Path, sets: list[FitSet]) -> dict[str, str]:
    """Load every fit through upstream's own deserializer. A fit that will not load is not
    a release: shipping one would fail on the user's machine, hours into their first run."""
    results: dict[str, str] = {}
    for fs in sets:
        for art in fs.artifacts:
            if not art.name.endswith(".json"):
                continue
            path = source / fs.package / "models" / art.name
            code = (
                "import sys;"
                "from powersim_core.serialize import load_dataclass, load_params;"
                "p = sys.argv[1];"
                "obj = None;"
                "\ntry:\n"
                "    obj = load_dataclass(p)\n"
                "except Exception:\n"
                "    obj = load_params(p)\n"
                "print(type(obj).__name__)"
            )
            p = subprocess.run([str(python), "-c", code, str(path)], capture_output=True,
                               text=True, timeout=1800, encoding="utf-8", errors="replace")
            key = f"{fs.package}/{art.name}"
            if p.returncode == 0:
                results[key] = f"ok ({p.stdout.strip().splitlines()[-1] if p.stdout.strip() else 'loaded'})"
            else:
                tail = (p.stderr or p.stdout or "").strip().splitlines()
                results[key] = f"FAILED: {tail[-1] if tail else 'unknown error'}"
    return results


def oversized_artifacts(sets: list[FitSet]) -> list[Artifact]:
    """Artifacts too big to share a chunk, whose packed size therefore rides on their own
    compressibility alone."""
    return [a for fs in sets for a in fs.artifacts if a.bytes > CHUNK_BYTES]


def write_chunks(source: Path, sets: list[FitSet], out_dir: Path, level: int = 10) -> list[dict]:
    import tarfile

    import zstandard

    out_dir.mkdir(parents=True, exist_ok=True)
    cctx = zstandard.ZstdCompressor(level=level)
    chunks: list[dict] = []
    index = written = 0

    def open_chunk(i: int):
        path = out_dir / f"fits.{i:03d}.tar.zst"
        fh = path.open("wb")
        writer = cctx.stream_writer(fh)
        return path, fh, writer, tarfile.open(fileobj=writer, mode="w|")

    path, fh, writer, tar = open_chunk(index)
    for fs in sets:
        for art in fs.artifacts:
            src = source / fs.package / "models" / art.name
            if written and written + art.bytes > CHUNK_BYTES:
                tar.close()
                writer.close()
                fh.close()
                chunks.append({"name": path.name, "bytes": path.stat().st_size,
                               "sha256": sha256_file(path)})
                index += 1
                written = 0
                path, fh, writer, tar = open_chunk(index)
            tar.add(src, arcname=f"{fs.package}/models/{art.name}")
            written += art.bytes
    tar.close()
    writer.close()
    fh.close()
    if path.stat().st_size:
        chunks.append({"name": path.name, "bytes": path.stat().st_size,
                       "sha256": sha256_file(path)})
    elif path.exists():
        path.unlink()

    over = [c for c in chunks if c["bytes"] > ASSET_LIMIT]
    if over:
        listed = ", ".join(f"{c['name']} ({human(c['bytes'])})" for c in over)
        raise SystemExit(
            f"chunk over the {human(ASSET_LIMIT)} publishing limit: {listed}.\n"
            f"A chunk holding one artifact bigger than {human(CHUNK_BYTES)} cannot be split "
            f"further, so its size depends on that file's compressibility. Either lower "
            f"--level (worse), host this release off GitHub, or split the artifact upstream."
        )
    return chunks


def verify(snapshot_dir: Path) -> int:
    manifest = snapshot_dir / "fits_manifest.json"
    if not manifest.is_file():
        print(f"no fits_manifest.json in {snapshot_dir}")
        return 2
    data = json.loads(manifest.read_text(encoding="utf-8"))
    bad = total = 0
    for chunk in data.get("chunks", []):
        total += 1
        path = snapshot_dir / chunk["name"]
        if not path.is_file():
            print(f"  MISSING  {chunk['name']}")
            bad += 1
            continue
        digest = sha256_file(path)
        if digest != chunk["sha256"]:
            print(f"  CORRUPT  {chunk['name']}")
            bad += 1
        else:
            print(f"  ok       {chunk['name']}  {human(chunk['bytes'])}")
    print(f"\n{total - bad}/{total} chunks verified")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, help="the checkout holding the fitted models")
    ap.add_argument("--tag", help="the code tag these fits belong to (they are not portable)")
    ap.add_argument("--python", type=Path, default=None,
                    help="a release venv's python, to prove every fit actually loads")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", type=Path, default=None)
    ap.add_argument("--level", type=int, default=10)
    args = ap.parse_args(argv)

    if args.verify:
        return verify(args.verify.resolve())
    if not args.source or not args.tag:
        ap.error("--source and --tag are required (a fit is only valid for one code version)")

    source = args.source.resolve()
    sets = collect(source, hash_files=not args.dry_run)

    print("== model fits ==")
    print(f"   source  {source}")
    print(f"   tag     {args.tag}\n")

    total = 0
    for fs in sets:
        print(f"   {fs.package}")
        for art in fs.artifacts:
            total += art.bytes
            note = "  (array sidecar)" if art.name.endswith(".npz") else ""
            print(f"      {art.name:<44} {human(art.bytes):>12}{note}")
        for problem in fs.problems:
            print(f"      !! {problem}")
    for package, why in NOT_PACKAGED.items():
        print(f"   {package}: not packaged - {why}")
    print(f"\n   TOTAL {human(total)}")

    problems = [p for fs in sets for p in fs.problems]

    loadable: dict[str, str] = {}
    if args.python:
        print("\n   verifying every fit loads through upstream's deserializer")
        loadable = verify_loadable(args.python, source, sets)
        for key, result in loadable.items():
            print(f"      [{'OK  ' if result.startswith('ok') else 'FAIL'}] {key}  {result}")
        problems += [f"{k}: {v}" for k, v in loadable.items() if not v.startswith("ok")]
    else:
        print("\n   NOTE: --python not given, so the fits were NOT load-tested. A fit that "
              "does not\n         load fails on the user's machine, not here. Pass a release "
              "venv before publishing.")

    if problems:
        print(f"\nNOT PACKAGED - {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1

    if args.dry_run:
        print("\ndry run - nothing written.")
        return 0

    for art in oversized_artifacts(sets):
        print(f"\n   NOTE: {art.package}/{art.name} is {human(art.bytes)}, larger than the "
              f"{human(CHUNK_BYTES)} chunk size. It gets a chunk to\n         itself, so the "
              f"published size depends on how well that one file compresses.")

    out_dir = (args.out or Path("dist") / f"fits-{args.tag}").resolve()
    print(f"\npackaging -> {out_dir}")
    chunks = write_chunks(source, sets, out_dir, args.level)
    manifest = {
        "code_tag": args.tag,
        "note": ("Fitted parameters produced by the owner. Valid only for this code tag: the "
                 "serialized dataclasses name their own classes and will not load across a "
                 "structural change."),
        "chunk_limit_bytes": CHUNK_BYTES,
        "raw_bytes": total,
        "artifacts": [asdict(a) for fs in sets for a in fs.artifacts],
        "load_verified": loadable or None,
        "chunks": chunks,
    }
    (out_dir / "fits_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    packed = sum(c["bytes"] for c in chunks)
    print(f"   {len(chunks)} chunk(s), {human(packed)} packed from {human(total)}")
    print(f"\nverify with:\n    python release_tools/release_fits.py --verify {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
