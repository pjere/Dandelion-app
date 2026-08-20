"""Scan an extracted upstream release tree for third-party imports and compare
against what the seven pyproject.toml files declare.

Purpose: upstream imports several packages lazily (inside functions) that are
declared nowhere. Those are invisible to `uv pip compile` and to the offline
test suite, but fatal at first real use. This scan is a permanent step of
`release-code` (Phase 1) and of Phase 0 verification.

Usage:  python release_tools/import_scan.py <path-to-extracted-release>
Exit 1 if undeclared third-party imports are found.
"""
from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

PACKAGES = [
    "powersim_core", "pricemodeling", "weathergen",
    "demand_model", "res_model", "availability_model", "dispatch_model",
]

# Import name -> distribution name, where they differ.
IMPORT_TO_DIST = {
    "yaml": "pyyaml", "dotenv": "python-dotenv", "sklearn": "scikit-learn",
    "dateutil": "python-dateutil", "PIL": "pillow", "entsoe": "entsoe-py",
    "open_mastr": "open-mastr", "netCDF4": "netCDF4", "cdsapi": "cdsapi",
    "highspy": "highspy", "bs4": "beautifulsoup4", "OpenSSL": "pyopenssl",
    "win32api": "pywin32", "win32job": "pywin32", "psutil": "psutil",
    "zoneinfo": "STDLIB", "tomllib": "STDLIB",
}


def stdlib_names() -> set[str]:
    names = set(sys.stdlib_module_names)
    names.discard("test")
    return names


def declared_dists(root: Path) -> dict[str, set[str]]:
    """Return {package: {normalised distribution names}} incl. optional extras."""
    out: dict[str, set[str]] = {}
    for pkg in PACKAGES:
        pyproject = root / pkg / "pyproject.toml"
        if not pyproject.exists():
            continue
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        proj = data.get("project", {})
        dists: set[str] = set()
        for spec in proj.get("dependencies", []):
            dists.add(_dist_name(spec))
        for extra_specs in (proj.get("optional-dependencies") or {}).values():
            for spec in extra_specs:
                dists.add(_dist_name(spec))
        out[pkg] = dists
    return out


def _dist_name(spec: str) -> str:
    for sep in (">=", "==", "<=", "~=", ">", "<", "[", ";", "!="):
        spec = spec.split(sep)[0]
    return spec.strip().lower().replace("_", "-")


def scan_imports(root: Path) -> dict[str, set[str]]:
    """Return {top-level import name: {relative source paths}} for the tree."""
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
                if node.level:          # relative import — internal
                    continue
                if node.module:
                    found.setdefault(node.module.split(".")[0], set()).add(rel)
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1]).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 2

    std = stdlib_names()
    declared = declared_dists(root)
    all_declared = set().union(*declared.values()) if declared else set()
    imports = scan_imports(root)

    local = set(PACKAGES) | {"tools", "scripts", "conftest"}
    undeclared: dict[str, set[str]] = {}
    for name, sources in sorted(imports.items()):
        if name in std or name in local or name.startswith("_"):
            continue
        dist = IMPORT_TO_DIST.get(name, name).lower().replace("_", "-")
        if dist == "stdlib":
            continue
        if dist not in all_declared:
            undeclared[name] = sources

    print(f"release tree      : {root}")
    print(f"packages declared : {len(declared)}  distributions: {len(all_declared)}")
    print(f"third-party imports found: {len(imports)}")
    print()
    if not undeclared:
        print("OK - every third-party import is declared by some package.")
        return 0

    print(f"UNDECLARED third-party imports: {len(undeclared)}")
    for name, sources in sorted(undeclared.items()):
        dist = IMPORT_TO_DIST.get(name, name)
        shown = sorted(sources)[:4]
        print(f"  {name:20s} (dist: {dist})")
        for s in shown:
            print(f"      {s}")
        if len(sources) > len(shown):
            print(f"      ... +{len(sources) - len(shown)} more")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
