"""Find third-party imports an upstream release declares nowhere.

Upstream imports several packages lazily, inside the function that needs them, and declares
them in no `pyproject.toml` and no requirements file. The offline test suite passes without
them; real ingestion dies at first use. They are invisible to `uv pip compile`, so the
release lock would omit them and every user would hit the same wall.

This scan is a permanent step of `release_code.py`. Its job is not to list the known ones -
those are pinned in `DRIVERS_SUPPLEMENT` and handled - but to catch a *new* one appearing in
a future release, quietly, the same way the current three did.

    python release_tools/import_scan.py <path-to-extracted-release> [--pinned cdsapi,entsoe]

Exit 1 if an undeclared import is found that is not already pinned.
"""
from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from pathlib import Path

PACKAGES = [
    "powersim_core", "pricemodeling", "weathergen",
    "demand_model", "res_model", "availability_model", "dispatch_model",
]

#: Import name -> distribution name, where they differ.
IMPORT_TO_DIST = {
    "yaml": "pyyaml", "dotenv": "python-dotenv", "sklearn": "scikit-learn",
    "dateutil": "python-dateutil", "PIL": "pillow", "entsoe": "entsoe-py",
    "open_mastr": "open-mastr", "bs4": "beautifulsoup4", "OpenSSL": "pyopenssl",
    "win32api": "pywin32", "win32job": "pywin32", "win32con": "pywin32",
    "attr": "attrs", "pkg_resources": "setuptools", "cv2": "opencv-python",
}

#: Requirement files that count as a declaration. requirements-dev.txt counts because a tool
#: importing `pdoc` in scripts/ is dev-only by construction - it never runs in a user venv.
REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt")


def _normalise(name: str) -> str:
    return name.lower().replace("_", "-")


def _dist_name(spec: str) -> str:
    for sep in (">=", "==", "<=", "~=", ">", "<", "[", ";", "!="):
        spec = spec.split(sep)[0]
    return _normalise(spec.strip())


def declared_distributions(root: Path) -> set[str]:
    """Everything the release declares anywhere: pyproject deps, every extra, requirement files."""
    dists: set[str] = set()
    for pkg in PACKAGES:
        pyproject = root / pkg / "pyproject.toml"
        if not pyproject.is_file():
            continue
        proj = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
        for spec in proj.get("dependencies", []):
            dists.add(_dist_name(spec))
        for specs in (proj.get("optional-dependencies") or {}).values():
            for spec in specs:
                dists.add(_dist_name(spec))
    for filename in REQUIREMENT_FILES:
        path = root / filename
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line and not line.startswith("-"):
                dists.add(_dist_name(line))
    return dists


def local_module_names(root: Path) -> set[str]:
    """Names importable as top-level modules without being installed.

    The packages themselves, plus every module sitting in a `scripts/` directory: those
    scripts put their own folder on `sys.path` before importing a sibling. `run_montecarlo.py`
    does exactly that to reach `mc_weather`, which is a file, not a distribution.
    """
    names = set(PACKAGES) | {"tools", "scripts", "conftest", "setup"}
    for scripts_dir in root.rglob("scripts"):
        if scripts_dir.is_dir():
            names.update(p.stem for p in scripts_dir.glob("*.py"))
    return names


def scan_imports(root: Path) -> dict[str, set[str]]:
    """{top-level import name: {relative source paths}} across the tree, excluding tests."""
    found: dict[str, set[str]] = {}
    for py in root.rglob("*.py"):
        rel = py.relative_to(root).as_posix()
        if "/tests/" in f"/{rel}" or rel.startswith("tests/"):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.setdefault(alias.name.split(".")[0], set()).add(rel)
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue                       # relative import - internal
                found.setdefault(node.module.split(".")[0], set()).add(rel)
    return found


def find_undeclared(root: Path) -> dict[str, set[str]]:
    """Third-party imports with no declaration anywhere in the release."""
    std = set(sys.stdlib_module_names)
    declared = declared_distributions(root)
    local = local_module_names(root)

    undeclared: dict[str, set[str]] = {}
    for name, sources in scan_imports(root).items():
        if name in std or name in local or name.startswith("_"):
            continue
        dist = _normalise(IMPORT_TO_DIST.get(name, name))
        if dist not in declared:
            undeclared[name] = sources
    return undeclared


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="extracted release tree")
    ap.add_argument("--pinned", default="",
                    help="comma-separated distributions already pinned by the release lock; "
                         "these are reported as handled rather than as failures")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    root = args.root.resolve()
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 2

    pinned = {_normalise(p) for p in args.pinned.split(",") if p.strip()}
    undeclared = find_undeclared(root)

    handled, new = {}, {}
    for name, sources in undeclared.items():
        dist = _normalise(IMPORT_TO_DIST.get(name, name))
        (handled if dist in pinned else new)[name] = sources

    print(f"release tree : {root}")
    print(f"undeclared   : {len(undeclared)}  ({len(handled)} pinned by us, {len(new)} new)")

    if handled:
        print("\nknown, and pinned by the release lock:")
        for name in sorted(handled):
            print(f"  {name} -> {IMPORT_TO_DIST.get(name, name)}")

    if not new:
        print("\nOK - no undeclared third-party import that the lock does not already cover.")
        return 0

    print(f"\nNEW UNDECLARED IMPORT{'S' if len(new) > 1 else ''} - the lock would not install "
          f"{'them' if len(new) > 1 else 'it'}:")
    for name, sources in sorted(new.items()):
        print(f"  {name}  (distribution: {IMPORT_TO_DIST.get(name, name)})")
        for src in sorted(sources)[:4]:
            print(f"      {src}")
        if len(sources) > 4:
            print(f"      ... +{len(sources) - 4} more")
    print("\nAdd it to DRIVERS_SUPPLEMENT in release_tools/release_code.py, with a pin, and "
          "record it in UPSTREAM_WISHLIST.md.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
