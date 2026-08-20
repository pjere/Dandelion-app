"""The declared, auditable exceptions to code-tree immutability.

The release tree is immutable by discipline, enforced by a manifest hash-check. This module
holds the *only* sanctioned deviations: a short, explicit list of configuration values the
installer sets, each with a reason, the upstream default it replaces, and a record written
into the install manifest so the deviation is always visible.

WHY THIS EXISTS AT ALL

`trend.enabled` is the one setting that cannot be passed in. `weathergen simulate` accepts
`--trend`, but the Monte-Carlo path does not go through the CLI: `mc_weather.py:34` loads
`<code_root>/weathergen/config.yaml` by hardcoded path and calls `_build_trend` with
`Namespace(ssp=None, target_year=None, trend=None)` — so all three overrides are None and
the config file's value is final. There is no flag, no environment variable and no `-c`.

Leaving it alone would mean a trended single simulation and an untrended Monte-Carlo from
the same install: exactly the silent divergence this product exists to prevent. So the
installer sets the value in the file, declares it, and hashes both states.

Patches are line-surgical: one value on one line inside one block. Comments, key order and
every other byte are preserved, so `git diff` against the pristine release shows one line.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ConfigPatch:
    """One scalar value the installer sets in one YAML block."""

    file: str            # path relative to the code root
    block: str           # top-level YAML key
    key: str             # key inside that block
    value: str           # the value to set, as it should appear in YAML
    upstream_default: str
    reason: str
    comment: str = ""    # replaces the inline comment, so the file stays self-explaining
    #: Values this key is allowed to hold before we write. Anything else means upstream
    #: changed its shipped default or somebody hand-edited the tree, and we stop rather than
    #: overwrite. It must include every value this patch family can itself write, otherwise
    #: the GUI toggle would be a one-way door.
    known_values: tuple[str, ...] = ()

    def accepts(self, current: str) -> bool:
        return current in (self.known_values or (self.upstream_default, self.value))


@dataclass
class PatchRecord:
    """What actually happened, for the install manifest."""

    file: str
    block: str
    key: str
    old_value: str
    new_value: str
    sha256_before: str
    sha256_after: str
    reason: str
    applied: bool = True
    note: str = ""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------------------
# Line-surgical YAML editing
# --------------------------------------------------------------------------------------

_BLOCK_RE = "^(?P<indent>[ ]*)%s:[ ]*(?P<comment>#.*)?$"
_KEY_RE = r"^(?P<indent>[ ]+)%s:(?P<gap>[ ]*)(?P<value>[^#\n]*?)(?P<pad>[ ]*)(?P<comment>#.*)?$"


def find_block(lines: list[str], block: str) -> tuple[int, int]:
    """Return the [start, end) line span of a top-level YAML block's body.

    Scoping matters: `weathergen/config.yaml` has an `enabled:` under `data.era5` as well as
    the one under `trend`. A file-wide search would flip the wrong one.
    """
    pattern = re.compile(_BLOCK_RE % re.escape(block))
    start = None
    base_indent = 0
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m and len(m.group("indent")) == 0:
            start = i + 1
            base_indent = len(m.group("indent"))
            break
    if start is None:
        raise KeyError(f"no top-level block {block!r}")

    end = len(lines)
    for i in range(start, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue                       # blank lines and comments stay inside the block
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            end = i
            break
    return start, end


def set_scalar_in_block(text: str, block: str, key: str, value: str,
                        comment: str | None = None) -> tuple[str, str]:
    """Set `block.key` to `value`, preserving everything else byte-for-byte.

    Returns `(new_text, old_value)`. Raises KeyError if the block or key is absent — a
    silent no-op here would mean shipping an install that thinks it enabled something it
    did not.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    start, end = find_block(lines, block)
    pattern = re.compile(_KEY_RE % re.escape(key))

    for i in range(start, end):
        m = pattern.match(lines[i])
        if not m:
            continue
        old_value = m.group("value").strip()
        # keep the inline comment aligned where it was, if it still fits
        tail = ""
        if comment is not None:
            tail = f"  {comment}" if comment else ""
        elif m.group("comment"):
            tail = f"{m.group('pad')}{m.group('comment')}"
        if comment is not None and m.group("comment"):
            column = len(m.group("indent")) + len(key) + 1 + len(m.group("gap")) + len(old_value) \
                + len(m.group("pad"))
            written = len(m.group("indent")) + len(key) + 1 + len(m.group("gap")) + len(value)
            tail = " " * max(1, column - written) + comment
        lines[i] = f"{m.group('indent')}{key}:{m.group('gap')}{value}{tail}"
        return newline.join(lines), old_value

    raise KeyError(f"no key {key!r} inside block {block!r}")


def read_scalar_in_block(text: str, block: str, key: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    start, end = find_block(lines, block)
    pattern = re.compile(_KEY_RE % re.escape(key))
    for i in range(start, end):
        m = pattern.match(lines[i])
        if m:
            return m.group("value").strip()
    raise KeyError(f"no key {key!r} inside block {block!r}")


# --------------------------------------------------------------------------------------
# The declared patch set
# --------------------------------------------------------------------------------------

def trend_patch(enabled: bool) -> ConfigPatch:
    """The climate-trend switch, as an install-time (and GUI-toggleable) patch.

    Upstream ships `false` — a stationary generator at observed climate. The product ships
    `true`, because its headline output is a projection to 2050: a twenty-year price path on
    present-day climate would understate cooling demand, wind and hydro shifts throughout.
    `Trend.apply` ramps the deltas from `baseline_year` (2020) to `target_year` (2050), so
    each simulated year gets its own fraction rather than a step change.

    Turning it off in the GUI writes the same patch with the other value, so the state is
    always declared rather than drifting.
    """
    return ConfigPatch(
        file="weathergen/config.yaml",
        block="trend",
        key="enabled",
        value="true" if enabled else "false",
        upstream_default="false",
        reason=(
            "climate trend ON by default for 2050 projections (owner decision, 2026-08-20); "
            "the Monte-Carlo path reads this file directly and accepts no override"
            if enabled else
            "climate trend explicitly disabled by the user"
        ),
        comment=(
            "# [dandelion] trend ON for 2050 projections; upstream default: false"
            if enabled else
            "# [dandelion] trend OFF by user choice; upstream default: false"
        ),
        known_values=("true", "false"),
    )


#: Applied by the installer, in order, right after extraction and before anything runs.
DEFAULT_PATCHES: tuple[ConfigPatch, ...] = (trend_patch(True),)


def apply_patches(code_root: Path,
                  patches: tuple[ConfigPatch, ...] = DEFAULT_PATCHES) -> list[PatchRecord]:
    """Apply the declared patches to an extracted release tree."""
    records: list[PatchRecord] = []
    for patch in patches:
        path = Path(code_root) / patch.file
        if not path.is_file():
            raise FileNotFoundError(f"patch target missing from the release: {patch.file}")

        before = _sha256(path)
        text = path.read_text(encoding="utf-8")
        current = read_scalar_in_block(text, patch.block, patch.key)

        if current == patch.value:
            records.append(PatchRecord(
                patch.file, patch.block, patch.key, current, patch.value,
                before, before, patch.reason, applied=False,
                note="already at the target value",
            ))
            continue

        if not patch.accepts(current):
            # Upstream changed the shipped default, or someone edited the tree. Either way the
            # assumption behind this patch no longer holds - say so instead of overwriting.
            records.append(PatchRecord(
                patch.file, patch.block, patch.key, current, patch.value,
                before, before, patch.reason, applied=False,
                note=(f"REFUSED: {patch.block}.{patch.key} holds {current!r}, which is neither "
                      f"the upstream default {patch.upstream_default!r} nor a value this "
                      f"product sets - review before overriding"),
            ))
            continue

        new_text, old_value = set_scalar_in_block(
            text, patch.block, patch.key, patch.value, patch.comment or None)
        path.write_text(new_text, encoding="utf-8")
        records.append(PatchRecord(
            patch.file, patch.block, patch.key, old_value, patch.value,
            before, _sha256(path), patch.reason,
        ))
    return records


def verify_patches(code_root: Path, records: list[PatchRecord]) -> list[str]:
    """Confirm the tree still matches what the install manifest says it did."""
    problems: list[str] = []
    for r in records:
        path = Path(code_root) / r.file
        if not path.is_file():
            problems.append(f"{r.file}: missing")
            continue
        digest = _sha256(path)
        expected = r.sha256_after if r.applied else r.sha256_before
        if digest != expected:
            problems.append(
                f"{r.file}: hash {digest[:12]} does not match the manifest ({expected[:12]}) - "
                f"the file changed after install"
            )
            continue
        try:
            actual = read_scalar_in_block(path.read_text(encoding="utf-8"), r.block, r.key)
        except KeyError as exc:
            problems.append(f"{r.file}: {exc}")
            continue
        if actual != r.new_value and r.applied:
            problems.append(f"{r.file}: {r.block}.{r.key} is {actual!r}, expected {r.new_value!r}")
    return problems


@dataclass
class PatchSummary:
    """What the GUI shows on the projection page, because this changes numbers."""

    trend_enabled: bool
    ssp: str
    target_year: int
    baseline_year: int = 2020
    deltas_file: str | None = None
    records: list[PatchRecord] = field(default_factory=list)

    def label(self) -> str:
        if not self.trend_enabled:
            return "Climate trend: OFF (observed climate, stationary)"
        return (f"Climate trend: ON - {self.ssp.upper()}, ramped {self.baseline_year} to "
                f"{self.target_year}")
