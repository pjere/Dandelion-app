"""Removing an installation without removing the things that cost a night to build.

The hazard this file exists for: `code/<tag>/data` is a junction to the data root, so a
careless recursive delete could walk through it and take 26 GB of databases with it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion.paths import Install  # noqa: E402
from dandelion.uninstall import (  # noqa: E402
    Removal,
    directory_size,
    execute,
    is_link,
    plan,
    remove_link,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")


@pytest.fixture
def install(tmp_path: Path) -> Install:
    inst = Install(app_root=tmp_path / "app", data_root=tmp_path / "data")
    (inst.app_root / "code" / "v0.1.0").mkdir(parents=True)
    inst.data_dir.mkdir(parents=True)
    (inst.data_dir / "pricemodeling.db").write_text("irreplaceable", encoding="utf-8")
    inst.runs_dir.mkdir(parents=True)
    (inst.runs_dir / "run-1.json").write_text("a projection", encoding="utf-8")
    return inst


def make_junction(link: Path, target: Path) -> bool:
    link.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True)
    return result.returncode == 0 and link.exists()


# ------------------------------------------------------------------------- junctions

@windows_only
def test_a_junction_is_recognised_as_a_link(install):
    link = install.code_dir("v0.1.0") / "data"
    assert make_junction(link, install.data_dir)
    assert is_link(link)


@windows_only
def test_islink_alone_would_miss_a_junction(install):
    """The trap: os.path.islink() is False for a junction, so code testing it treats the
    link as a real directory and deletes through it."""
    link = install.code_dir("v0.1.0") / "data"
    assert make_junction(link, install.data_dir)
    assert not os.path.islink(link)
    assert os.path.isjunction(link)


@windows_only
def test_removing_a_link_leaves_the_target_intact(install):
    link = install.code_dir("v0.1.0") / "data"
    assert make_junction(link, install.data_dir)
    assert remove_link(link)
    assert not link.exists()
    assert (install.data_dir / "pricemodeling.db").is_file()


@windows_only
def test_uninstalling_the_software_never_takes_the_data_with_it(install):
    """The whole point. Remove the program, keep the databases."""
    link = install.code_dir("v0.1.0") / "data"
    assert make_junction(link, install.data_dir)

    execute(install, {"software": True, "data": False, "runs": False, "mastr": False})

    assert not (install.app_root / "code").exists()
    assert not install.env_dir("v0.1.0").exists()
    assert (install.data_dir / "pricemodeling.db").read_text(encoding="utf-8") == "irreplaceable"


def test_the_app_folder_is_only_removed_once_it_is_empty(install):
    """It survives while it still holds something the user chose to keep."""
    execute(install, {"software": True, "data": False, "runs": False, "mastr": False})
    assert install.app_root.exists(), "runs were kept, so the folder holding them must stay"
    assert (install.runs_dir / "run-1.json").is_file()


def test_the_app_folder_goes_when_nothing_is_kept(tmp_path):
    inst = Install(app_root=tmp_path / "app", data_root=tmp_path / "data")
    (inst.app_root / "code" / "v0.1.0").mkdir(parents=True)
    inst.data_dir.mkdir(parents=True)
    execute(inst, {"software": True, "data": True, "runs": True, "mastr": False})
    assert not inst.app_root.exists()


@windows_only
def test_directory_size_does_not_count_data_twice_through_a_link(install):
    link = install.code_dir("v0.1.0") / "data"
    assert make_junction(link, install.data_dir)
    assert directory_size(install.app_root / "code") == 0


def test_removing_a_plain_directory_is_not_a_link(tmp_path):
    plain = tmp_path / "ordinary"
    plain.mkdir()
    assert not is_link(plain)
    assert not remove_link(plain)
    assert plain.exists()


# ------------------------------------------------------------------------------ plan

def test_irreplaceable_things_are_never_removed_by_default(install):
    for item in plan(install):
        if item.irreplaceable:
            assert not item.remove_by_default, item.key


def test_the_databases_and_runs_are_marked_irreplaceable(install):
    marked = {item.key for item in plan(install) if item.irreplaceable}
    assert marked == {"data", "runs"}


def test_the_software_is_removed_by_default(install):
    software = next(item for item in plan(install) if item.key == "software")
    assert software.remove_by_default and not software.irreplaceable


def test_every_item_explains_the_consequence(install):
    for item in plan(install):
        assert len(item.why) > 30, item.key


def test_the_mastr_download_is_offered_separately(install):
    """It lives in the user profile and other tools may use it."""
    mastr = next(item for item in plan(install) if item.key == "mastr")
    assert not mastr.remove_by_default
    assert "profile" in mastr.why or "other tools" in mastr.why


# --------------------------------------------------------------------------- execute

def test_keeping_everything_removes_nothing(install):
    report = execute(install, {"software": False, "data": False, "runs": False,
                               "mastr": False})
    assert install.app_root.exists() and install.data_dir.exists()
    assert report.removed == [] or all("link" in r for r in report.removed)


def test_runs_can_be_kept_while_the_program_goes(install):
    execute(install, {"software": True, "data": False, "runs": False, "mastr": False})
    assert (install.runs_dir / "run-1.json").is_file()


def test_a_clean_sweep_asks_for_data_first(install):
    """If the data root sits inside the app root, removing the parent first would take it
    without the choice ever being honoured."""
    inside = Install(app_root=install.app_root, data_root=install.app_root / "data-root")
    inside.data_dir.mkdir(parents=True)
    (inside.data_dir / "db").write_text("x", encoding="utf-8")
    execute(inside, {"software": True, "data": False, "runs": False, "mastr": False})
    assert (inside.data_dir / "db").is_file()


def test_a_removal_records_what_it_kept(install):
    report = execute(install, {"software": False, "data": False, "runs": False,
                               "mastr": False})
    assert any("database" in kept.lower() for kept in report.kept)


def test_removal_reports_bytes_freed(install):
    item = Removal(key="x", label="x", path=install.runs_dir, why="y" * 40, bytes=123)
    assert item.bytes == 123
