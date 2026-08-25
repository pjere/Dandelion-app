"""Running a model whose config says only what the data can support.

Upstream is read-only, so a cluster with no data behind it cannot be removed in code. It
can be removed in config: `Config.all_zones` is literally the keys of the `zones:` mapping
(dispatch_model/config.py:47).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.config_overlay import write_zone_overlay  # noqa: E402

CONFIG = {
    "run": {"seed": 1, "mode": "multi_zone", "models_dir": "models",
            "reports_dir": "reports", "output_dir": "output", "resolution": "1h"},
    "zones": {
        "FR": {"name": "France", "unit_resolved": True},
        "IT_NORTH": {"name": "Italy North", "unit_resolved": False},
        "IT_SOUTH": {"name": "Italy South", "virtual": True},
        "CH": {"name": "Switzerland"},
    },
    "borders": [["FR", "CH"], ["CH", "IT_NORTH"], ["IT_NORTH", "IT_SOUTH"], ["FR", "IT_NORTH"]],
    "data": {"sqlite_path": "../data/pricemodeling.db"},
    "assumptions": {"workbook": "../scenarios.xlsx"},
}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A config with real files beside it, since resolution is existence-gated."""
    root = tmp_path / "code" / "dispatch_model"
    root.mkdir(parents=True)
    for d in ("models", "reports", "output"):
        (root / d).mkdir()
    (tmp_path / "code" / "data").mkdir()
    (tmp_path / "code" / "data" / "pricemodeling.db").write_bytes(b"")
    (tmp_path / "code" / "scenarios.xlsx").write_bytes(b"")
    config = root / "config.yaml"
    config.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    return config


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_zone_leaves(tree, tmp_path):
    out = tmp_path / "configs" / "d.yaml"
    assert write_zone_overlay(tree, out, ["IT_SOUTH"]) == ["IT_SOUTH"]
    assert set(load(out)["zones"]) == {"FR", "IT_NORTH", "CH"}


def test_its_borders_leave_with_it(tree, tmp_path):
    """`config.borders` feeds the projection path directly, where there is no intersection
    with the active zones to save a dangling pair."""
    out = tmp_path / "configs" / "d.yaml"
    write_zone_overlay(tree, out, ["IT_SOUTH"])
    borders = load(out)["borders"]
    assert ["IT_NORTH", "IT_SOUTH"] not in borders
    assert ["CH", "IT_NORTH"] in borders and len(borders) == 3


def test_paths_survive_the_move(tree, tmp_path):
    """THE trap: every relative path resolves against the config FILE's directory, so an
    overlay written elsewhere would silently relocate reports, models, output, the database
    and the workbook."""
    out = tmp_path / "configs" / "d.yaml"
    write_zone_overlay(tree, out, ["IT_SOUTH"])
    raw = load(out)
    assert Path(raw["run"]["reports_dir"]) == (tree.parent / "reports").resolve()
    assert Path(raw["run"]["models_dir"]) == (tree.parent / "models").resolve()
    assert Path(raw["data"]["sqlite_path"]).resolve() == \
        (tree.parent / ".." / "data" / "pricemodeling.db").resolve()
    assert Path(raw["assumptions"]["workbook"]).resolve() == \
        (tree.parent / ".." / "scenarios.xlsx").resolve()


def test_ordinary_strings_are_left_alone(tree, tmp_path):
    """Absolutising is existence-gated, so settings that merely look pathish stay put."""
    out = tmp_path / "configs" / "d.yaml"
    write_zone_overlay(tree, out, ["IT_SOUTH"])
    run = load(out)["run"]
    assert run["mode"] == "multi_zone" and run["resolution"] == "1h" and run["seed"] == 1


def test_dropping_nothing_writes_nothing(tree, tmp_path):
    out = tmp_path / "configs" / "d.yaml"
    assert write_zone_overlay(tree, out, ["NOT_A_ZONE"]) == []
    assert not out.exists(), "a complete year must keep using the shipped config"


def test_the_overlay_says_why_it_exists(tree, tmp_path):
    out = tmp_path / "configs" / "d.yaml"
    write_zone_overlay(tree, out, ["IT_SOUTH"])
    head = out.read_text(encoding="utf-8")[:600]
    assert "IT_SOUTH" in head and "Do not edit" in head


def test_the_code_tree_is_not_touched(tree, tmp_path):
    before = tree.read_bytes()
    write_zone_overlay(tree, tmp_path / "configs" / "d.yaml", ["IT_SOUTH"])
    assert tree.read_bytes() == before, "the extracted release must stay as it was archived"
    assert not list(tree.parent.glob("*.degraded.yaml"))
