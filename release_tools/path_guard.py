"""Fail the build if this repository has grown a path into somebody's machine.

The product must never depend on the owner's working copy. That rule is easy to state
and easy to violate by accident - a debug constant, a default argument, a test fixture.
This guard greps the tree for absolute paths into a user profile, for references to the
upstream checkout by name, and for the owner's identity.

`release_tools/` is exempt from the upstream-checkout rule for exactly one reason: the
`release-data` packager is an owner-side tool whose whole job is to read the git-ignored
data artifacts that exist only in the owner's working copy. It is still NOT allowed to
carry a hardcoded absolute path - it must take the source root as an argument.

    python release_tools/path_guard.py [--root .]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Directories never scanned.
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache",
             ".ruff_cache", "dist", "build", ".mypy_cache"}

#: Only text we could plausibly execute or ship.
SCAN_SUFFIXES = {".py", ".toml", ".yaml", ".yml", ".json", ".cfg", ".ini", ".txt",
                 ".ps1", ".bat", ".cmd", ".md"}


class Rule:
    def __init__(self, name: str, pattern: str, message: str,
                 exempt_dirs: tuple[str, ...] = (), ignore_case: bool = True):
        self.name = name
        self.re = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        self.message = message
        self.exempt_dirs = exempt_dirs


#: A real drive letter: one letter followed by a colon, not the tail of a scheme like
#: `sqlite:///...` (whose "e:" would otherwise read as drive E).
_DRIVE = r"(?<![A-Za-z])[A-Za-z]:"


RULES = (
    Rule(
        "absolute-user-path",
        rf"['\"](?:{_DRIVE}[\\/]+Users[\\/]+|/(?:home|Users)/)[^'\"]{{2,}}['\"]",
        "an absolute path into a user profile - take it as an argument or resolve it at runtime",
    ),
    # NB: the upstream PACKAGE is `pricemodeling` (lowercase) and citing it is entirely
    # legitimate - drivers/ is *supposed* to reference upstream files. What must never
    # appear is a path walking to the owner's CHECKOUT DIRECTORY `PriceModeling`. Hence
    # case-sensitive, and only the traversal forms.
    Rule(
        "upstream-checkout",
        rf"(?:\.\.[\\/]+|{_DRIVE}[\\/][^'\"\s]*[\\/])PriceModeling(?=[\\/'\"\s]|$)",
        "a path that walks to the owner's upstream checkout - code comes from release archives only",
        exempt_dirs=("release_tools",),
        ignore_case=False,
    ),
    Rule(
        "onedrive-path",
        r"OneDrive[^'\"\s]*[\\/]",
        "a OneDrive path - sync roots corrupt large SQLite files under write",
    ),
    Rule(
        "owner-identity",
        r"PierreJeremie|pjeremie\.org",
        "the owner's identity baked into the product",
    ),
    Rule(
        "scratch-path",
        r"AppData[\\/]+Local[\\/]+Temp[\\/]+(?:claude|dlt)\b",
        "a leftover development scratch path",
    ),
)

#: Lines carrying this marker are allowed to trip a rule - for documentation that must
#: quote a real path (phase reports, the runbook). Keep these rare and reviewed.
ALLOW = "path-guard: allow"


def scan(root: Path) -> list[tuple[Path, int, str, str]]:
    findings: list[tuple[Path, int, str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] == "docs" and "phase-reports" in rel.parts:
            continue          # phase reports record measurements from real machines
        if path.resolve() == Path(__file__).resolve():
            continue          # this file necessarily spells out the patterns it forbids
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        top = rel.parts[0] if rel.parts else ""
        for n, line in enumerate(text.splitlines(), 1):
            if ALLOW in line:
                continue
            for rule in RULES:
                if top in rule.exempt_dirs:
                    continue
                if rule.re.search(line):
                    findings.append((rel, n, rule.name, line.strip()[:140]))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path("."))
    args = ap.parse_args(argv)
    root = args.root.resolve()

    findings = scan(root)
    if not findings:
        print(f"path_guard: clean ({root})")
        return 0

    print(f"path_guard: {len(findings)} finding(s)\n")
    by_rule = {r.name: r for r in RULES}
    for rel, n, rule_name, line in findings:
        print(f"  {rel.as_posix()}:{n}  [{rule_name}]")
        print(f"      {line}")
        print(f"      -> {by_rule[rule_name].message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
