"""Build, measure and stamp the shipped executable.

The app release is the exe plus `latest.json`, which is what an installed copy polls to
discover an update. Everything the updater needs to decide and verify lives in that file:
version, size, sha256, and the minimum OS.

The build is MEASURED, not just produced. Two numbers decide the Phase 2 packaging question
and neither can be reasoned about from first principles:

  * cold start - a onefile exe unpacks itself into %TEMP% on every launch, which costs
    seconds and looks exactly like malware behaviour to a real-time scanner;
  * size - what a user downloads.

So this builds, runs the frozen binary's own --self-check, times a cold start, and records
all of it. If onefile turns out slow or AV-provoking, --onedir is one flag away and the
numbers are directly comparable.

    python release_tools/release_app.py --version 0.1.0
    python release_tools/release_app.py --version 0.1.0 --onedir
    python release_tools/release_app.py --version 0.1.0 --no-build     # re-stamp only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "src" / "dandelion" / "__main__.py"
NAME = "Dandelion"

#: Windows 10 1803 is the floor for the APIs the installer relies on (junctions without
#: elevation are older, but WebView2 and modern TLS effectively set this).
MIN_OS = "10.0.17134"

COLD_START_BUDGET_S = 5.0


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def find_pyinstaller() -> list[str] | None:
    exe = shutil.which("pyinstaller")
    if exe:
        return [exe]
    p = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                       capture_output=True, text=True)
    return [sys.executable, "-m", "PyInstaller"] if p.returncode == 0 else None


def build(version: str, out_dir: Path, onedir: bool, work: Path) -> Path:
    pyinstaller = find_pyinstaller()
    if pyinstaller is None:
        raise SystemExit(
            "PyInstaller not found. `pip install pyinstaller` in the tooling environment.\n"
            "It is a build-time tool only - it never reaches a user machine."
        )
    if not ENTRY.is_file():
        raise SystemExit(f"no entry point at {ENTRY}")

    argv = [
        *pyinstaller,
        "--noconfirm", "--clean",
        "--name", NAME,
        "--onedir" if onedir else "--onefile",
        "--distpath", str(out_dir),
        "--workpath", str(work / "build"),
        "--specpath", str(work),
        # Console for now: the wizard has diagnostics to print and Phase 2 decides the
        # windowed/native question on clean-VM evidence, not here.
        "--console",
        str(ENTRY),
    ]
    print("   " + " ".join(argv[len(pyinstaller):]))
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=3600)
    if p.returncode != 0:
        raise SystemExit(f"PyInstaller failed:\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}")

    exe = (out_dir / NAME / f"{NAME}.exe") if onedir else (out_dir / f"{NAME}.exe")
    if not exe.is_file():
        raise SystemExit(f"build reported success but {exe} is missing")
    return exe


def measure(exe: Path) -> dict:
    """Run the frozen binary and time it. A build that cannot run is not a release."""
    result: dict = {}

    t0 = time.monotonic()
    p = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    result["cold_start_s"] = round(time.monotonic() - t0, 2)
    result["version_output"] = p.stdout.strip()
    result["runs"] = p.returncode == 0

    t0 = time.monotonic()
    p = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    result["warm_start_s"] = round(time.monotonic() - t0, 2)

    p = subprocess.run([str(exe), "--self-check"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    result["self_check"] = p.stdout.strip().splitlines()
    result["self_check_ok"] = p.returncode == 0
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", type=Path, default=REPO / "dist" / "app")
    ap.add_argument("--onedir", action="store_true",
                    help="ship a directory instead of a single self-extracting file")
    ap.add_argument("--notes", default="", help="one-line release note for latest.json")
    ap.add_argument("--url", default="", help="download URL to advertise in latest.json")
    ap.add_argument("--no-build", action="store_true", help="re-stamp an existing build")
    ap.add_argument("--work", type=Path,
                    default=Path.home() / "AppData" / "Local" / "Temp" / "dandelion-app-build")
    args = ap.parse_args(argv)

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)

    print(f"== app {args.version} ==")
    print(f"   mode  {'onedir' if args.onedir else 'onefile'}")
    print(f"   out   {out_dir}\n")

    exe = (out_dir / NAME / f"{NAME}.exe") if args.onedir else (out_dir / f"{NAME}.exe")
    if args.no_build:
        if not exe.is_file():
            raise SystemExit(f"--no-build but nothing at {exe}")
        print("[1/3] using the existing build")
    else:
        print("[1/3] building")
        exe = build(args.version, out_dir, args.onedir, args.work)

    payload = exe.parent if args.onedir else exe
    total_bytes = (sum(f.stat().st_size for f in payload.rglob("*") if f.is_file())
                   if args.onedir else exe.stat().st_size)
    print(f"      {exe.name}  {human(exe.stat().st_size)}"
          + (f"  (payload {human(total_bytes)})" if args.onedir else ""))

    print("[2/3] measuring")
    m = measure(exe)
    print(f"      runs            {m['runs']}  ({m['version_output']})")
    print(f"      cold start      {m['cold_start_s']}s")
    print(f"      warm start      {m['warm_start_s']}s")
    for line in m["self_check"]:
        print(f"      | {line}")

    warnings = []
    if not args.onedir and m["cold_start_s"] > COLD_START_BUDGET_S:
        warnings.append(
            f"cold start {m['cold_start_s']}s exceeds the {COLD_START_BUDGET_S}s budget - "
            f"a onefile exe self-extracts to %TEMP% on every launch. Try --onedir and compare."
        )
    if not m["runs"] or not m["self_check_ok"]:
        warnings.append("the frozen binary did not run cleanly - do not publish")

    print("[3/3] stamping latest.json")
    latest = {
        "version": args.version,
        "name": NAME,
        "file": exe.name,
        "packaging": "onedir" if args.onedir else "onefile",
        "sha256": sha256_file(exe),
        "bytes": exe.stat().st_size,
        "payload_bytes": total_bytes,
        "min_os": MIN_OS,
        "url": args.url,
        "notes": args.notes,
        "signed": False,
        "measurements": m,
        "warnings": warnings,
    }
    (out_dir / "latest.json").write_text(json.dumps(latest, indent=2), encoding="utf-8")

    print()
    for w in warnings:
        print(f"WARNING: {w}")
    if not latest["url"]:
        print("NOTE: latest.json has no url - fill it in when hosting is decided (D1a/D3).")
    if not latest["signed"]:
        print("NOTE: this binary is unsigned. SmartScreen will warn users until D2 "
              "(code signing) is settled.")
    print(f"\nwrote {out_dir / 'latest.json'}")
    return 1 if not m["runs"] or not m["self_check_ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
