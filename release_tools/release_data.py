"""Package the owner's built databases into a downloadable snapshot.

This is the ONE tool in the product that reads the owner's working copy, and it exists
because those databases live nowhere else: 26 GB of ingested data, git-ignored by design,
representing hours of API pulls against the owner's own quotas. A user who cannot get a
snapshot must rebuild it themselves, which is legal and supported but takes a night.

Because it is the one exception, it is the one tool with hard guardrails:

  * it never packages a TRACKED file - code and config come from the code release, and a
    file that `git ls-files` knows about is by definition not data (verified per file, not
    by extension guessing);
  * it never packages source-like files, as a second, independent check;
  * it packages a store only when `data_stores.yaml` says `ship: yes` AND the worksheet is
    signed off, so a licensing question can never be answered by accident;
  * it takes the source root as an argument. There is no default path to anyone's machine.

    python release_tools/release_data.py --source <upstream working copy> --dry-run
    python release_tools/release_data.py --source <...> --out dist/snapshot-2026-08
    python release_tools/release_data.py --verify dist/snapshot-2026-08
"""
from __future__ import annotations

import argparse
import fnmatch
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

import yaml  # noqa: E402

WORKSHEET = Path(__file__).resolve().parent / "data_stores.yaml"

#: GitHub caps a single release asset at 2 GB; stay under it with room for the container.
CHUNK_BYTES = 1_500_000_000

#: Independent of the git check: nothing that looks like source may ever enter a data
#: snapshot, even if somebody forgot to commit it.
SOURCE_SUFFIXES = {".py", ".pyi", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".md", ".rst",
                   ".txt", ".ipynb", ".sh", ".ps1", ".bat", ".cmd"}

SAMPLE_BYTES = 8 << 20        # bytes read per sampled file
MAX_SAMPLE_FILES = 24         # files sampled beyond the three largest
MAX_SAMPLE_BYTES = 192 << 20  # ceiling on total bytes read for one estimate


@dataclass
class StorePlan:
    key: str
    path: str
    ship: str
    files: int = 0
    bytes: int = 0
    excluded_tracked: int = 0
    excluded_source: int = 0
    sample_ratio: float | None = None
    sample_coverage: float = 0.0
    estimated_compressed_bytes: int | None = None
    chunks: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tracked_files(source: Path) -> set[str]:
    """Every path git knows about, as posix relative paths.

    This is the primary guard. Extension heuristics are a backstop; git is the authority on
    what is code and what is data, and upstream's .gitignore already draws that line.
    """
    try:
        p = subprocess.run(["git", "ls-files"], cwd=str(source), capture_output=True,
                           text=True, timeout=300, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if p.returncode != 0:
        return set()
    return {line.strip() for line in p.stdout.splitlines() if line.strip()}


def is_excluded(rel: str, globs: list[str]) -> bool:
    """Whether a path is inside an excluded subtree.

    `fnmatch` alone is not enough: the worksheet names DIRECTORIES ("dispatch_model/
    scratchpad", "**/era5_cache"), and fnmatch("a/scratchpad/cube.nc", "a/scratchpad") is
    False. Without the trailing-glob forms the exclusions silently do nothing, which is the
    worst kind of wrong for a tool whose job is deciding what gets published.
    """
    for raw in globs:
        g = raw.rstrip("/")
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, f"{g}/*"):
            return True
    return False


def load_worksheet(path: Path = WORKSHEET) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def resolve_store_paths(source: Path, store: dict) -> list[Path]:
    if store.get("home"):
        return [Path.home() / store["path"]]
    if store.get("glob"):
        return sorted(p for p in source.glob(store["path"]) if p.is_dir())
    return [source / store["path"]]


def plan_store(source: Path, store: dict, tracked: set[str],
               excluded_globs: list[str]) -> StorePlan:
    plan = StorePlan(key=store["key"], path=store["path"], ship=str(store.get("ship", "pending")))

    for root in resolve_store_paths(source, store):
        if not root.exists():
            plan.notes.append(f"absent on this machine: {root}")
            continue
        candidates = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for f in candidates:
            try:
                rel = f.relative_to(source).as_posix()
            except ValueError:
                rel = f.name                       # outside the source root (home stores)
            if rel in tracked:
                plan.excluded_tracked += 1
                continue
            if f.suffix.lower() in SOURCE_SUFFIXES:
                plan.excluded_source += 1
                continue
            if is_excluded(rel, excluded_globs):
                continue
            try:
                plan.bytes += f.stat().st_size
            except OSError:
                continue
            plan.files += 1
    return plan


def estimate_ratio(source: Path, store: dict,
                   level: int = 10) -> tuple[float | None, float]:
    """Compress a spread of files to predict the packed size. Returns (ratio, coverage).

    Compressing 37 GB to answer "how big is the download" would take an hour, so this
    samples. Sampling only the biggest files would be misleading for a store like
    `data/raw`, which is 1,644 heterogeneous payloads rather than one big one - so the
    sample takes the largest few AND a stride across the rest, and reports what fraction of
    the store's bytes it actually read. Trust the ratio in proportion to that coverage.
    """
    try:
        import zstandard
    except ImportError:
        return None, 0.0

    files: list[Path] = []
    for root in resolve_store_paths(source, store):
        if not root.exists():
            continue
        files.extend([root] if root.is_file() else
                     [p for p in root.rglob("*") if p.is_file()])
    if not files:
        return None, 0.0

    sized = sorted(((f, f.stat().st_size) for f in files if f.exists()),
                   key=lambda t: t[1], reverse=True)
    total_bytes = sum(n for _, n in sized) or 1

    chosen = sized[:3]
    rest = sized[3:]
    if rest:
        stride = max(1, len(rest) // MAX_SAMPLE_FILES)
        chosen += rest[::stride][:MAX_SAMPLE_FILES]

    cctx = zstandard.ZstdCompressor(level=level)
    raw = comp = 0
    for f, _ in chosen:
        if raw >= MAX_SAMPLE_BYTES:
            break
        try:
            with f.open("rb") as fh:
                sample = fh.read(SAMPLE_BYTES)
        except OSError:
            continue
        if not sample:
            continue
        raw += len(sample)
        comp += len(cctx.compress(sample))
    if not raw:
        return None, 0.0
    return comp / raw, min(1.0, raw / total_bytes)


def write_chunks(source: Path, store: dict, plan: StorePlan, out_dir: Path,
                 tracked: set[str], excluded_globs: list[str], level: int = 10) -> None:
    """Stream the store into zstd-compressed tar chunks of at most CHUNK_BYTES."""
    import tarfile

    import zstandard

    out_dir.mkdir(parents=True, exist_ok=True)
    cctx = zstandard.ZstdCompressor(level=level)
    index = 0
    written = 0
    chunk_path = out_dir / f"{plan.key}.{index:03d}.tar.zst"
    fh = chunk_path.open("wb")
    writer = cctx.stream_writer(fh)
    tar = tarfile.open(fileobj=writer, mode="w|")

    def rotate():
        nonlocal index, written, chunk_path, fh, writer, tar
        tar.close()
        writer.close()
        fh.close()
        plan.chunks.append({"name": chunk_path.name, "bytes": chunk_path.stat().st_size,
                            "sha256": sha256_file(chunk_path)})
        index += 1
        written = 0
        chunk_path = out_dir / f"{plan.key}.{index:03d}.tar.zst"
        fh = chunk_path.open("wb")
        writer = cctx.stream_writer(fh)
        tar = tarfile.open(fileobj=writer, mode="w|")

    for root in resolve_store_paths(source, store):
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for f in candidates:
            try:
                rel = f.relative_to(source).as_posix()
            except ValueError:
                rel = f"{store['key']}/{f.name}"
            if rel in tracked or f.suffix.lower() in SOURCE_SUFFIXES:
                continue
            if is_excluded(rel, excluded_globs):
                continue
            size = f.stat().st_size
            if written and written + size > CHUNK_BYTES:
                rotate()
            tar.add(f, arcname=rel)
            written += size

    tar.close()
    writer.close()
    fh.close()
    if chunk_path.stat().st_size > 0:
        plan.chunks.append({"name": chunk_path.name, "bytes": chunk_path.stat().st_size,
                            "sha256": sha256_file(chunk_path)})
    elif chunk_path.exists():
        chunk_path.unlink()


def verify(snapshot_dir: Path) -> int:
    manifest_path = snapshot_dir / "data_manifest.json"
    if not manifest_path.is_file():
        print(f"no data_manifest.json in {snapshot_dir}")
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = 0
    total = 0
    for store in manifest.get("stores", []):
        for chunk in store.get("chunks", []):
            total += 1
            path = snapshot_dir / chunk["name"]
            if not path.is_file():
                print(f"  MISSING  {chunk['name']}")
                bad += 1
                continue
            digest = sha256_file(path)
            if digest != chunk["sha256"]:
                print(f"  CORRUPT  {chunk['name']}  {digest[:12]} != {chunk['sha256'][:12]}")
                bad += 1
            else:
                print(f"  ok       {chunk['name']}  {human(chunk['bytes'])}")
    print(f"\n{total - bad}/{total} chunks verified")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path,
                    help="the upstream working copy holding the built data")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--snapshot-id", default=None, help="defaults to the source's HEAD date")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure and estimate; write nothing")
    ap.add_argument("--verify", type=Path, default=None, help="re-check a built snapshot")
    ap.add_argument("--level", type=int, default=10, help="zstd level (default 10)")
    ap.add_argument("--worksheet", type=Path, default=WORKSHEET)
    args = ap.parse_args(argv)

    if args.verify:
        return verify(args.verify.resolve())

    if not args.source:
        ap.error("--source is required (there is deliberately no default path)")
    source = args.source.resolve()
    if not source.is_dir():
        return print(f"not a directory: {source}") or 2

    sheet = load_worksheet(args.worksheet)
    signed = bool(sheet.get("signed_off"))
    excluded_globs = [e["path"] for e in sheet.get("excluded", [])]
    tracked = tracked_files(source)

    print("== data snapshot ==")
    print(f"   source     {source}")
    print(f"   worksheet  {args.worksheet.name}  "
          f"({'SIGNED OFF' if signed else 'NOT signed off (D1b pending)'})")
    print(f"   tracked files seen by git: {len(tracked):,} (never packaged)\n")

    plans: list[StorePlan] = []
    for store in sheet.get("stores", []):
        plan = plan_store(source, store, tracked, excluded_globs)
        ratio, coverage = estimate_ratio(source, store, args.level) if plan.bytes else (None, 0.0)
        plan.sample_ratio = round(ratio, 4) if ratio else None
        plan.sample_coverage = round(coverage, 4)
        if ratio:
            plan.estimated_compressed_bytes = int(plan.bytes * ratio)
        plans.append(plan)

    width = max(len(p.key) for p in plans)
    total_raw = total_est = 0
    shippable_raw = shippable_est = 0
    print(f"   {'store'.ljust(width)}  {'ship':>8}  {'files':>8}  {'raw':>12}  {'~packed':>12}"
          f"  ratio  sampled")
    for p in plans:
        est = p.estimated_compressed_bytes or 0
        total_raw += p.bytes
        total_est += est
        if p.ship == "yes":
            shippable_raw += p.bytes
            shippable_est += est
        ratio = f"{p.sample_ratio:.2f}" if p.sample_ratio else "  -"
        cover = f"{p.sample_coverage * 100:5.1f}%" if p.sample_ratio else "     -"
        print(f"   {p.key.ljust(width)}  {p.ship:>8}  {p.files:>8,}  {human(p.bytes):>12}  "
              f"{human(est):>12}  {ratio:>5}  {cover}")
        if p.excluded_tracked or p.excluded_source:
            print(f"   {' ' * width}  excluded: {p.excluded_tracked} tracked, "
                  f"{p.excluded_source} source-like")
        for note in p.notes:
            print(f"   {' ' * width}  {note}")

    print(f"\n   TOTAL (all stores)        {human(total_raw):>12}  ~{human(total_est)} compressed")
    print(f"   TOTAL (ship: yes)         {human(shippable_raw):>12}  ~{human(shippable_est)} compressed")
    n_chunks = -(-shippable_est // CHUNK_BYTES) if shippable_est else 0
    print(f"   -> {n_chunks} chunk(s) of at most {human(CHUNK_BYTES)}")

    if args.dry_run:
        print("\ndry run - nothing written.")
        if not signed:
            print("D1b is not signed off, so every store reads `pending` and the shippable "
                  "total is zero.\nFill in release_tools/data_stores.yaml, set signed_off: "
                  "true, then re-run.")
        print("\nCompressed sizes are ESTIMATES, not measurements: each store is sampled (its "
              "largest files plus\na stride across the rest) and the ratio extrapolated. The "
              "`sampled` column is the fraction of\nthe store's bytes actually compressed - "
              "read each estimate with that in mind. Precise enough\nto choose hosting (D1a); "
              "real sizes land in data_manifest.json when a snapshot is built.")
        return 0

    if not signed:
        print("\nREFUSING to package: release_tools/data_stores.yaml is not signed off (D1b).")
        print("Redistributing these stores is a per-source licensing decision. Fill in the "
              "worksheet,\nset signed_off: true, and re-run. Until then use --dry-run.")
        return 1

    shippable = [p for p in plans if p.ship == "yes"]
    if not shippable:
        print("\nnothing marked `ship: yes` - nothing to package.")
        return 1

    snapshot_id = args.snapshot_id or subprocess.run(
        ["git", "log", "-1", "--format=%cs"], cwd=str(source), capture_output=True, text=True
    ).stdout.strip() or "undated"
    out_dir = (args.out or Path("dist") / f"snapshot-{snapshot_id}").resolve()
    print(f"\npackaging {len(shippable)} store(s) -> {out_dir}")

    for plan in shippable:
        store = next(s for s in sheet["stores"] if s["key"] == plan.key)
        print(f"   {plan.key} ...", flush=True)
        write_chunks(source, store, plan, out_dir, tracked, excluded_globs, args.level)
        print(f"   {plan.key}: {len(plan.chunks)} chunk(s), "
              f"{human(sum(c['bytes'] for c in plan.chunks))}")

    provenance = source / "data" / "RAW_EXTRACTS_MANIFEST.json"
    manifest = {
        "snapshot_id": snapshot_id,
        "chunk_limit_bytes": CHUNK_BYTES,
        "zstd_level": args.level,
        "worksheet_signed_off_by": sheet.get("signed_off_by"),
        "worksheet_signed_off_date": str(sheet.get("signed_off_date")),
        "raw_extracts_manifest": (
            json.loads(provenance.read_text(encoding="utf-8"))
            if provenance.is_file() else None),
        "stores": [asdict(p) for p in shippable],
    }
    (out_dir / "data_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nwrote data_manifest.json - verify with:\n"
          f"    python release_tools/release_data.py --verify {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
