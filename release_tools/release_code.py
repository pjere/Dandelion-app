"""Turn an upstream git tag into a pinned, qualified code release.

A code release is three files:

    dandelion-code-<tag>.zip    the source tree, from `git archive <tag>` and nothing else
    constraints.lock            every third-party version, resolved for windows / cpython 3.12
    code_manifest.json          hashes, sizes, provenance and the qualification results

The owner's working copy is never an input. The archive comes from the tag, so uncommitted
work cannot leak into a release, and two people building the same tag get the same bytes.

QUALIFICATION is the point of this tool. It does not just package: it builds a scratch
environment using the *exact procedure the installer will use*, and refuses to publish if
that procedure does not produce a working install. The checks that matter:

  * nothing is built from source except packages we have reviewed as pure Python, because
    a user machine has no compiler (see SDIST_ALLOWLIST);
  * the seven packages install EDITABLE in ADR-8 order (a regular install silently breaks
    pricemodeling's PROJECT_ROOT anchoring - see docs/phase-reports/phase-0.md);
  * the three lazily-imported, undeclared dependencies are importable;
  * no NEW undeclared third-party import has appeared since the last release;
  * the config values the product deliberately inherits are still what it expects;
  * the upstream test suites pass, with the dev layer installed in the scratch venv ONLY -
    pytest is a dev dependency upstream and must never reach a user environment. The suites
    are not fully offline: one dispatch_model test needs the built database and is named in
    DATA_DEPENDENT_TESTS. Any OTHER failure fails the release.

    python release_tools/release_code.py --tag v0.1.0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The Windows console is usually cp1252 and uv's diagnostics are full of box-drawing and
# arrows. Without this, the tool crashes while REPORTING a failure - the worst moment to lose
# output. Same guard upstream uses in pricemodeling/pipeline.py.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.code_patches import check_expectations  # noqa: E402
from drivers.inventory import (  # noqa: E402
    CONSOLE_SCRIPTS,
    INSTALL_ORDER,
    REQUIRED_EXTRAS,
    UNDECLARED_IMPORTS,
    selftest,
)
from release_tools.import_scan import _dist_name  # noqa: E402
from release_tools.import_scan import main as import_scan_main  # noqa: E402

DEFAULT_REPO = "https://github.com/pjere/Dandelion.git"

#: Pinned by us, not by upstream: imported lazily inside functions and declared in no
#: pyproject.toml and no requirements file. Without these, offline tests pass and real
#: ingestion dies at first use. See UPSTREAM_WISHLIST.md section 1.
DRIVERS_SUPPLEMENT = (
    "cdsapi>=0.7.7",
    "entsoe-py>=0.6",
    "open-mastr>=0.14",
)

#: Needed by the app itself inside the user's environment (process-tree cancellation).
APP_RUNTIME_SUPPLEMENT = (
    "psutil>=5.9",
)

#: Packages PyPI ships as an sdist only, which we have reviewed and accepted.
#:
#: The requirement the installer actually has to meet is "no compiler on the user's machine",
#: which is NOT the same as "every dependency is a wheel": a pure-Python sdist builds fine
#: with setuptools alone. So the gate is an allowlist, not a blanket ban - and anything that
#: appears here without review fails the release.
#:
#: Every entry must state why building it is safe.
SDIST_ALLOWLIST = {
    "pymeeus": (
        "pure-Python implementation of Meeus' astronomical algorithms; no extension modules, "
        "no compiler. Reaches us through demand_model -> workalendar -> convertdate, which "
        "needs it for lunar-calendar holidays. PyPI has never published a wheel for it."
    ),
}

#: Upstream tests that fail in a clean release environment because they need the BUILT
#: DATABASE, not just the code. A freshly extracted release has no pricemodeling.db, so this
#: says nothing about the release; what matters is that no OTHER test fails.
DATA_DEPENDENT_TESTS = {
    "dispatch_model:tests/test_structural.py::test_neighbour_thermal_blocks_carry_must_run_floor":
        "builds neighbour thermal blocks from the built database",
}

#: These SKIP cleanly when there is no database at all, but FAIL with "no such table" when an
#: empty pricemodeling.db exists. An empty database is what you get by re-using a work tree
#: that a previous run touched - so seeing these fail means the tree is stale, not that the
#: code regressed. They are deliberately NOT allowlisted: silently accepting eleven failures
#: would hide a real regression in any of them. Instead they drive a specific hint.
STALE_DB_SENSITIVE_TESTS = frozenset({
    "tests/test_fr_stack.py::test_efficiency_dispersion_gives_slope",
    "tests/test_fr_stack.py::test_merit_order_and_fuel_switch",
    "tests/test_fr_stack.py::test_stack_composition",
    "tests/test_neighbours.py::test_german_fuel_switch_2022",
    "tests/test_neighbours.py::test_german_stack_composition",
    "tests/test_neighbours.py::test_netload_range",
    "tests/test_sensitivity.py::test_co2_shock_raises_prices_more_in_coal_heavy_DE",
    "tests/test_sensitivity.py::test_gas_shock_raises_prices",
    "tests/test_sensitivity.py::test_nuclear_shock_creates_FR_premium",
    "tests/test_hydro.py::test_reservoir_climatology_from_db",
    "tests/test_smoke.py::test_entsoe_loaders",
})

_PYTEST_FAIL_RE = re.compile(r"^(?:FAILED|ERROR)\s+(?P<nodeid>\S+)", re.MULTILINE)


def parse_pytest_failures(output: str) -> set[str]:
    """Node ids pytest reported as failed or errored, from its -rfE summary."""
    return {m.group("nodeid").split(" - ")[0] for m in _PYTEST_FAIL_RE.finditer(output)}


_BUILDING_RE = re.compile(r"^\s*(?:Building|Built)\s+(?P<name>[A-Za-z0-9._-]+)==(?P<version>\S+)",
                          re.MULTILINE)


def sdists_built(install_output: str) -> dict[str, str]:
    """Which distributions uv had to build from source, from its own progress output."""
    return {m.group("name").lower().replace("_", "-"): m.group("version")
            for m in _BUILDING_RE.finditer(install_output)}


# --------------------------------------------------------------------------------------

@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    duration_s: float = 0.0


@dataclass
class Manifest:
    tag: str
    commit: str
    built_at: str
    built_on: str
    #: Where the tag came from. A local path means this was a REHEARSAL, not a publishable
    #: release: the tag was not fetched from the canonical remote.
    source_repo: str = ""
    rehearsal: bool = False
    archive: str = ""
    archive_sha256: str = ""
    archive_bytes: int = 0
    lock_sha256: str = ""
    locked_packages: int = 0
    built_from_sdist: dict = field(default_factory=dict)
    python: str = ""
    uv_version: str = ""
    packages: list[str] = field(default_factory=list)
    qualification: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def rmtree(path: Path) -> None:
    """Delete a tree that may contain a git object store.

    Git marks objects and packfiles read-only, and on Windows that makes `os.unlink` raise
    PermissionError. `shutil.rmtree(..., ignore_errors=True)` then leaves a partial tree
    behind, and the next clone fails with "destination path already exists" - which reads
    like a missing tag, sending you off to debug the wrong thing.
    """
    def _clear_readonly(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if path.exists():
        shutil.rmtree(path, onexc=_clear_readonly)
    if path.exists():
        raise SystemExit(f"could not remove {path} - close anything holding a file inside it")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(argv: list[str], cwd: Path | None = None, timeout: int = 1800,
        env: dict | None = None) -> tuple[int, str]:
    p = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace", env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def find_uv() -> list[str]:
    """uv, however it is available. Recorded in the manifest so a release is reproducible."""
    exe = shutil.which("uv")
    if exe:
        return [exe]
    rc, _ = run([sys.executable, "-m", "uv", "--version"], timeout=60)
    if rc == 0:
        return [sys.executable, "-m", "uv"]
    raise SystemExit(
        "uv not found. Install it with `pip install uv`, or from https://astral.sh/uv, then "
        "re-run. The version used is recorded in code_manifest.json."
    )


# --------------------------------------------------------------------------------------
# 1. source
# --------------------------------------------------------------------------------------

def fetch_tag(repo: str, tag: str, dest: Path) -> str:
    """Shallow-clone exactly one tag. Returns the commit sha it points at."""
    rmtree(dest)
    rc, out = run(["git", "clone", "--depth", "1", "--branch", tag, repo, str(dest)], timeout=900)
    if rc != 0:
        raise SystemExit(
            f"could not clone tag {tag!r} from {repo}\n{out.strip()}\n\n"
            f"If the tag does not exist yet, create and push it:\n"
            f"    git tag -a {tag} -m \"release {tag}\" && git push origin {tag}"
        )
    rc, sha = run(["git", "rev-parse", "HEAD"], cwd=dest, timeout=60)
    return sha.strip()


def make_archive(clone: Path, tag: str, out_dir: Path) -> Path:
    """`git archive` and nothing else: the tag is the only input."""
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"dandelion-code-{tag}.zip"
    rc, out = run(["git", "archive", "--format=zip", f"--prefix=dandelion-{tag}/",
                   "-o", str(archive), tag], cwd=clone, timeout=600)
    if rc != 0:
        raise SystemExit(f"git archive failed:\n{out}")
    return archive


def extract(archive: Path, work: Path, tag: str) -> Path:
    rmtree(work)
    work.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(work)
    root = work / f"dandelion-{tag}"
    if not root.is_dir():
        raise SystemExit(f"archive did not contain the expected prefix dandelion-{tag}/")
    return root


# --------------------------------------------------------------------------------------
# 2. the lock
# --------------------------------------------------------------------------------------

def collect_requirements(root: Path) -> tuple[list[str], list[str]]:
    """Every runtime requirement of the seven packages, plus what upstream forgot.

    Extras are NOT optional in practice: `weathergen[stats]` carries the statistical stack
    that `fit` needs and `demand_model[calib]` the one `calibrate` needs, so a lock compiled
    from `dependencies` alone produces an install that dies at the first model fit.
    """
    specs: list[str] = []
    provenance: list[str] = []

    for pkg in INSTALL_ORDER:
        pyproject = root / pkg / "pyproject.toml"
        if not pyproject.is_file():
            raise SystemExit(f"release tree is missing {pkg}/pyproject.toml")
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        proj = data.get("project", {})
        for spec in proj.get("dependencies", []):
            specs.append(spec)
        extras = REQUIRED_EXTRAS.get(pkg, ())
        available = proj.get("optional-dependencies") or {}
        for extra in extras:
            if extra not in available:
                provenance.append(f"# NOTE: {pkg} no longer declares the [{extra}] extra")
                continue
            for spec in available[extra]:
                specs.append(spec)
        if extras:
            provenance.append(f"# {pkg}: dependencies + extras {list(extras)}")
        else:
            provenance.append(f"# {pkg}: dependencies")

    req_txt = root / "requirements.txt"
    if req_txt.is_file():
        for line in req_txt.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                specs.append(line)
        provenance.append("# requirements.txt (upstream's own pin list)")

    specs.extend(DRIVERS_SUPPLEMENT)
    provenance.append(f"# drivers supplement (undeclared upstream imports): {list(DRIVERS_SUPPLEMENT)}")
    specs.extend(APP_RUNTIME_SUPPLEMENT)
    provenance.append(f"# app runtime supplement: {list(APP_RUNTIME_SUPPLEMENT)}")

    seen, deduped = set(), []
    for spec in specs:
        key = spec.strip()
        if key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped, provenance


def compile_lock(uv: list[str], specs: list[str], provenance: list[str],
                 out_dir: Path, tag: str) -> Path:
    req_in = out_dir / "requirements.in"
    header = [
        f"# Generated by release_tools/release_code.py for {tag}.",
        "# Do not edit: regenerate by re-running the release.",
        *provenance,
        "",
    ]
    req_in.write_text("\n".join(header + specs) + "\n", encoding="utf-8")

    lock = out_dir / "constraints.lock"
    rc, out = run([*uv, "pip", "compile", str(req_in),
                   "--python-version", "3.12",
                   "--output-file", str(lock),
                   "--no-header"], timeout=1800)
    if rc != 0:
        raise SystemExit(f"uv pip compile failed:\n{out}")
    return lock


def count_locked(lock: Path) -> int:
    return sum(1 for line in lock.read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.strip().startswith(("#", "-")))


# --------------------------------------------------------------------------------------
# 3. the scratch environment - the installer's procedure, exactly
# --------------------------------------------------------------------------------------

def build_env(uv: list[str], root: Path, lock: Path,
              venv: Path) -> tuple[Path, list[Step], dict[str, str]]:
    steps: list[Step] = []
    built: dict[str, str] = {}
    rmtree(venv)

    rc, out = run([*uv, "venv", "--python", "3.12", str(venv)], timeout=900)
    steps.append(Step("create venv (cpython 3.12)", rc == 0, out.strip()[-400:]))
    if rc != 0:
        return venv, steps, built
    python = venv / "Scripts" / "python.exe"

    rc, out = run([*uv, "pip", "install", "--python", str(python), "-r", str(lock)],
                  timeout=2400)
    steps.append(Step("install locked dependencies", rc == 0,
                      out.strip()[-1500:] if rc else f"{count_locked(lock)} packages"))
    if rc != 0:
        return venv, steps, built

    # The gate: nothing may be built from source unless we have reviewed it and concluded it
    # needs no compiler. A user machine has no build tools, so an unreviewed source build is
    # a failed install waiting to happen.
    built.update(sdists_built(out))
    unreviewed = {n: v for n, v in built.items() if n not in SDIST_ALLOWLIST}
    if built:
        reviewed = ", ".join(f"{n}=={v}" for n, v in sorted(built.items())
                             if n in SDIST_ALLOWLIST)
        detail = (f"unreviewed source build(s): "
                  f"{', '.join(f'{n}=={v}' for n, v in sorted(unreviewed.items()))}. "
                  f"Confirm the package is pure Python and add it to SDIST_ALLOWLIST with a "
                  f"reason, or pin around it."
                  if unreviewed else f"allowlisted, pure-Python: {reviewed}")
    else:
        detail = "every locked dependency installed from a wheel"
    steps.append(Step("no unreviewed source builds", not unreviewed, detail))
    if unreviewed:
        return venv, steps, built

    # ADR-8: editable, and in this order. --no-deps because the lock already decided versions.
    for pkg in INSTALL_ORDER:
        rc, out = run([*uv, "pip", "install", "--python", str(python),
                       "--no-deps", "-e", str(root / pkg)], timeout=900)
        steps.append(Step(f"editable install {pkg}", rc == 0, out.strip()[-400:]))
        if rc != 0:
            return venv, steps, built

    return venv, steps, built


def qualify(root: Path, venv: Path, uv: list[str], run_tests: bool) -> list[Step]:
    steps: list[Step] = []
    python = venv / "Scripts" / "python.exe"

    checks = selftest(root, python)
    failed = [c for c in checks if not c.ok and c.fatal]
    warned = [c for c in checks if not c.ok and not c.fatal]
    steps.append(Step(
        f"inventory selftest ({len(checks)} checks)", not failed,
        "; ".join(f"{c.name}: {c.detail}" for c in failed) or
        (f"{len(warned)} warnings" if warned else "all passed"),
    ))

    for mod, dist in UNDECLARED_IMPORTS.items():
        rc, _ = run([str(python), "-c", f"import {mod}"], timeout=180)
        steps.append(Step(f"undeclared import '{mod}' resolves (pip: {dist})", rc == 0))

    for name in CONSOLE_SCRIPTS:
        exe = venv / "Scripts" / f"{name}.exe"
        steps.append(Step(f"console script '{name}'", exe.is_file()))

    pinned = ",".join(sorted({_dist_name(s) for s in DRIVERS_SUPPLEMENT + APP_RUNTIME_SUPPLEMENT}))
    rc = import_scan_main([str(root), "--pinned", pinned])
    steps.append(Step("no NEW undeclared third-party import", rc == 0,
                      "see the scan output above - pin it in DRIVERS_SUPPLEMENT and re-release"))

    problems = check_expectations(root)
    steps.append(Step("inherited config values unchanged", not problems, "; ".join(problems)))

    if run_tests:
        dev = root / "requirements-dev.txt"
        if dev.is_file():
            # dev layer in the SCRATCH venv only - the user environment never gets pytest
            rc, out = run([*uv, "pip", "install", "--python", str(python), "-r", str(dev)],
                          timeout=1800)
            steps.append(Step("install dev layer (scratch venv only)", rc == 0, out.strip()[-400:]))
        suites = [("root", root)] + [(p, root / p) for p in INSTALL_ORDER
                                     if (root / p / "tests").is_dir()]
        for label, cwd in suites:
            if not (cwd / "tests").is_dir():
                continue
            # No -x: stopping at the first failure hides how many others there are, and the
            # first one is usually a data-dependent test rather than the interesting one.
            rc, out = run([str(python), "-m", "pytest", "-q", "--no-header", "-rfE", "tests"],
                          cwd=cwd, timeout=3600)
            summary = ""
            for line in reversed(out.strip().splitlines()):
                if re.search(r"\d+ (passed|failed|error)", line):
                    summary = line.strip()
                    break
            failures = parse_pytest_failures(out)
            expected = {f for f in failures if f"{label}:{f}" in DATA_DEPENDENT_TESTS}
            unexpected = failures - expected
            detail = summary or out.strip()[-500:]
            if expected:
                detail += f"  [{len(expected)} known data-dependent]"
            if unexpected:
                detail += "  UNEXPECTED: " + ", ".join(sorted(unexpected)[:6])
                if unexpected <= STALE_DB_SENSITIVE_TESTS:
                    detail += ("  -- these all skip on a clean tree and fail only against a "
                               "stale EMPTY pricemodeling.db. The work tree was re-used: "
                               "re-run with a fresh --work directory.")
            steps.append(Step(f"pytest {label}", not unexpected, detail))
    return steps


# --------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help="git URL, or a local path/clone (for rehearsals)")
    ap.add_argument("--out", type=Path, default=Path("dist"))
    ap.add_argument("--work", type=Path, default=None,
                    help="scratch directory; keep it SHORT (MAX_PATH bites the wheel build)")
    ap.add_argument("--skip-tests", action="store_true",
                    help="skip the upstream pytest suites (faster rehearsal, not a release)")
    ap.add_argument("--allow-failures", action="store_true",
                    help="write the manifest even when qualification fails - never for a real release")
    args = ap.parse_args(argv)

    uv = find_uv()
    rc, uv_ver = run([*uv, "--version"], timeout=60)
    out_dir = (args.out / args.tag).resolve()
    work = (args.work or Path.home() / "AppData" / "Local" / "Temp" / "dandelion-release").resolve()
    work.mkdir(parents=True, exist_ok=True)

    print(f"== release {args.tag} ==")
    print(f"   repo   {args.repo}")
    print(f"   out    {out_dir}")
    print(f"   work   {work}")
    print(f"   uv     {uv_ver.strip()}\n")

    clone = work / "src"
    print("[1/6] fetching the tag")
    commit = fetch_tag(args.repo, args.tag, clone)
    print(f"      {args.tag} -> {commit}")

    print("[2/6] building the archive")
    archive = make_archive(clone, args.tag, out_dir)
    root = extract(archive, work / "tree", args.tag)
    print(f"      {archive.name}  {archive.stat().st_size / 1e6:.1f} MB")

    print("[3/6] resolving the lock")
    specs, provenance = collect_requirements(root)
    lock = compile_lock(uv, specs, provenance, out_dir, args.tag)
    n_locked = count_locked(lock)
    print(f"      {len(specs)} requirements -> {n_locked} pinned packages")

    print("[4/6] building a scratch environment with the installer's procedure")
    venv, build_steps, sdists = build_env(uv, root, lock, work / "venv")
    for s in build_steps:
        print(f"      [{'OK  ' if s.ok else 'FAIL'}] {s.name}")

    steps = list(build_steps)
    if all(s.ok for s in build_steps):
        print("[5/6] qualifying")
        qsteps = qualify(root, venv, uv, run_tests=not args.skip_tests)
        for s in qsteps:
            print(f"      [{'OK  ' if s.ok else 'FAIL'}] {s.name}"
                  + (f"  {s.detail}" if s.detail and not s.ok else ""))
        steps += qsteps
    else:
        print("[5/6] qualification skipped - the environment did not build")

    failed = [s for s in steps if not s.ok]

    print("[6/6] writing the manifest")
    rc, py_ver = run([str(venv / "Scripts" / "python.exe"), "--version"], timeout=60)
    manifest = Manifest(
        tag=args.tag, commit=commit,
        built_at=subprocess.run(["git", "log", "-1", "--format=%cI", commit], cwd=clone,
                                capture_output=True, text=True).stdout.strip(),
        built_on=f"{platform.system()} {platform.release()} / {platform.machine()}",
        source_repo=str(args.repo),
        rehearsal=not str(args.repo).startswith(("http://", "https://", "git@", "ssh://")),
        archive=archive.name, archive_sha256=sha256_file(archive),
        archive_bytes=archive.stat().st_size,
        lock_sha256=sha256_file(lock), locked_packages=n_locked,
        built_from_sdist=sdists,
        python=py_ver.strip(), uv_version=uv_ver.strip(),
        packages=list(INSTALL_ORDER),
        qualification=[asdict(s) for s in steps],
    )
    if manifest.rehearsal:
        manifest.notes.append(
            f"REHEARSAL: the tag was read from {args.repo}, not from the canonical remote. "
            f"These artifacts must not be published."
        )
    if args.skip_tests:
        manifest.notes.append("upstream test suites were skipped (--skip-tests)")
    if failed:
        manifest.notes.append(f"QUALIFICATION FAILED: {len(failed)} step(s)")

    (out_dir / "code_manifest.json").write_text(
        json.dumps(asdict(manifest), indent=2), encoding="utf-8")

    print()
    if failed:
        print(f"RELEASE NOT QUALIFIED - {len(failed)} failing step(s):")
        for s in failed:
            print(f"  - {s.name}: {s.detail}")
        if args.allow_failures:
            print(f"\n--allow-failures: artifacts were written to {out_dir} anyway. "
                  f"They are NOT publishable.")
            return 0
        print(f"\nartifacts left in {out_dir} for inspection; do not publish them")
        return 1

    print(f"release {args.tag} qualified - {len(steps)} checks passed")
    print(f"  {archive.name}          sha256 {manifest.archive_sha256[:16]}...")
    print(f"  constraints.lock        sha256 {manifest.lock_sha256[:16]}...  "
          f"{n_locked} packages")
    print("  code_manifest.json")
    print(f"\nin {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
