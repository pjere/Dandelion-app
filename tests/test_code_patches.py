"""The one sanctioned edit to the release tree must stay surgical, scoped and declared.

The fixture mirrors the shape of the real `weathergen/config.yaml` at 09a2459, including the
decoy `enabled:` under `data.era5` and the French inline comments, because both are what a
careless patcher gets wrong.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.code_patches import (  # noqa: E402
    DEFAULT_PATCHES,
    PatchSummary,
    apply_patches,
    find_block,
    read_scalar_in_block,
    set_scalar_in_block,
    trend_patch,
    verify_patches,
)

CONFIG = """\
# Configuration weathergen.

data:
  source: "pricemodeling_sqlite"
  sqlite_path: "../data/pricemodeling.db"
  era5:
    enabled: true                # DECOY: a patcher that searches the whole file flips this
    source: "arco"

run:
  seed: 20260629
  models_dir: "models"

# --- Phase 6 : tendance climatique (externe, jamais estimee) -----------------
trend:
  enabled: false                 # off par defaut -> sortie stationnaire au climat observe
  method: "qdm"                  # quantile delta mapping
  ssp: "ssp245"                  # input simulation
  cmip6_deltas_path: null        # npz
  baseline_year: 2020            # climat de reference
  target_year: 2050              # horizon
  trend_variability: true        # deltas par quantile

simulate:
  horizon_years: 20
"""


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "weathergen").mkdir()
    (tmp_path / "weathergen" / "config.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


def _cfg(tree) -> Path:
    return tree / "weathergen" / "config.yaml"


# ------------------------------------------------------------------------------- scoping

def test_block_scoping_finds_only_the_named_block():
    lines = CONFIG.split("\n")
    start, end = find_block(lines, "trend")
    body = "\n".join(lines[start:end])
    assert "enabled: false" in body
    assert "source: \"arco\"" not in body


def test_reads_the_right_enabled_of_two():
    assert read_scalar_in_block(CONFIG, "trend", "enabled") == "false"


def test_missing_block_or_key_is_loud():
    with pytest.raises(KeyError):
        read_scalar_in_block(CONFIG, "nosuchblock", "enabled")
    with pytest.raises(KeyError):
        read_scalar_in_block(CONFIG, "trend", "nosuchkey")


# -------------------------------------------------------------------------------- surgery

def test_exactly_one_line_changes(tree):
    before = _cfg(tree).read_text(encoding="utf-8").split("\n")
    apply_patches(tree)
    after = _cfg(tree).read_text(encoding="utf-8").split("\n")
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(differing) == 1
    assert "enabled" in after[differing[0]]


def test_the_decoy_is_untouched(tree):
    apply_patches(tree)
    parsed = yaml.safe_load(_cfg(tree).read_text(encoding="utf-8"))
    assert parsed["data"]["era5"]["enabled"] is True
    assert parsed["trend"]["enabled"] is True


def test_nothing_else_in_the_document_moves(tree):
    before = yaml.safe_load(_cfg(tree).read_text(encoding="utf-8"))
    apply_patches(tree)
    after = yaml.safe_load(_cfg(tree).read_text(encoding="utf-8"))
    before["trend"]["enabled"] = True
    assert before == after


def _trend_line(tree) -> str:
    """The `enabled:` line inside the trend block — NOT the identical-looking decoy above it."""
    lines = _cfg(tree).read_text(encoding="utf-8").split("\n")
    start, end = find_block(lines, "trend")
    return next(ln for ln in lines[start:end] if ln.strip().startswith("enabled:"))


def test_the_new_line_explains_itself(tree):
    apply_patches(tree)
    line = _trend_line(tree)
    assert "true" in line
    assert "[dandelion]" in line and "upstream default: false" in line


def test_comments_elsewhere_survive(tree):
    apply_patches(tree)
    text = _cfg(tree).read_text(encoding="utf-8")
    assert "# quantile delta mapping" in text
    assert "tendance climatique" in text


def test_crlf_files_stay_crlf(tmp_path):
    (tmp_path / "weathergen").mkdir()
    p = tmp_path / "weathergen" / "config.yaml"
    p.write_bytes(CONFIG.replace("\n", "\r\n").encode("utf-8"))
    apply_patches(tmp_path)
    raw = p.read_bytes()
    assert b"\r\n" in raw and b"\n\n" not in raw.replace(b"\r\n", b"")


# --------------------------------------------------------------------------------- safety

def test_idempotent(tree):
    apply_patches(tree)
    text_once = _cfg(tree).read_text(encoding="utf-8")
    records = apply_patches(tree)
    assert records[0].applied is False
    assert "already at the target value" in records[0].note
    assert _cfg(tree).read_text(encoding="utf-8") == text_once


def test_refuses_when_upstream_default_changed(tree):
    """If upstream ships something other than what the patch assumes, do not overwrite it."""
    text = _cfg(tree).read_text(encoding="utf-8")
    text, _ = set_scalar_in_block(text, "trend", "enabled", "maybe")
    _cfg(tree).write_text(text, encoding="utf-8")
    records = apply_patches(tree)
    assert records[0].applied is False
    assert "REFUSED" in records[0].note
    assert read_scalar_in_block(_cfg(tree).read_text(encoding="utf-8"), "trend", "enabled") == "maybe"


def test_missing_target_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        apply_patches(tmp_path)


def test_records_carry_both_hashes(tree):
    r = apply_patches(tree)[0]
    assert r.sha256_before != r.sha256_after
    assert len(r.sha256_before) == 64 and len(r.sha256_after) == 64
    assert r.old_value == "false" and r.new_value == "true"
    assert "Monte-Carlo" in r.reason


def test_verify_accepts_the_tree_it_patched(tree):
    assert verify_patches(tree, apply_patches(tree)) == []


def test_verify_catches_post_install_tampering(tree):
    records = apply_patches(tree)
    text = _cfg(tree).read_text(encoding="utf-8")
    _cfg(tree).write_text(text.replace("enabled: true", "enabled: false"), encoding="utf-8")
    problems = verify_patches(tree, records)
    assert problems and "changed after install" in problems[0]


# --------------------------------------------------------------------------------- toggle

def test_the_gui_toggle_uses_the_same_machinery(tree):
    apply_patches(tree)
    off = apply_patches(tree, (trend_patch(False),))
    # turning it off is itself a declared patch, not a drift
    assert off[0].applied is True and off[0].new_value == "false"
    line = _trend_line(tree)
    assert "false" in line and "[dandelion]" in line and "user choice" in line


def test_the_toggle_is_not_a_one_way_door(tree):
    """Regression: the 'is it still the upstream default?' guard once blocked every toggle
    after the first install, so the trend could be turned on but never off."""
    for enabled in (True, False, True, False):
        apply_patches(tree, (trend_patch(enabled),))
        parsed = yaml.safe_load(_cfg(tree).read_text(encoding="utf-8"))
        assert parsed["trend"]["enabled"] is enabled
        assert parsed["data"]["era5"]["enabled"] is True


def test_shipped_default_is_trend_on():
    assert len(DEFAULT_PATCHES) == 1
    p = DEFAULT_PATCHES[0]
    assert (p.file, p.block, p.key, p.value) == ("weathergen/config.yaml", "trend", "enabled", "true")


def test_summary_label_states_the_scenario():
    on = PatchSummary(True, "ssp245", 2050).label()
    assert "SSP245" in on and "2020" in on and "2050" in on
    assert "OFF" in PatchSummary(False, "ssp245", 2050).label()


# --------------------------------------------------------------- inherited-but-chosen values

def test_expectations_pass_on_the_shipped_config(tree):
    from drivers.code_patches import check_expectations
    assert check_expectations(tree) == []


def test_expectations_catch_an_upstream_scenario_change(tree):
    """SSP2-4.5 is a choice we inherit, not one we set. If upstream moves it, say so."""
    from drivers.code_patches import check_expectations
    text = _cfg(tree).read_text(encoding="utf-8")
    text, _ = set_scalar_in_block(text, "trend", "ssp", '"ssp585"')
    _cfg(tree).write_text(text, encoding="utf-8")
    problems = check_expectations(tree)
    assert len(problems) == 1
    assert "ssp585" in problems[0] and "ssp245" in problems[0]
    assert "owner decision" in problems[0]


def test_expectations_catch_a_horizon_change(tree):
    from drivers.code_patches import check_expectations
    text = _cfg(tree).read_text(encoding="utf-8")
    text, _ = set_scalar_in_block(text, "trend", "target_year", "2040")
    _cfg(tree).write_text(text, encoding="utf-8")
    assert any("target_year" in p for p in check_expectations(tree))


def test_expectations_are_reported_not_overwritten(tree):
    """These are a review trigger, never a silent correction."""
    from drivers.code_patches import check_expectations
    text = _cfg(tree).read_text(encoding="utf-8")
    text, _ = set_scalar_in_block(text, "trend", "ssp", '"ssp585"')
    _cfg(tree).write_text(text, encoding="utf-8")
    check_expectations(tree)
    assert read_scalar_in_block(_cfg(tree).read_text(encoding="utf-8"), "trend", "ssp") == '"ssp585"'
