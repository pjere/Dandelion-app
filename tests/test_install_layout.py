"""The install layout and the guards that run before 26 GB lands somewhere unsuitable.

The layout is not a matter of taste: it is the shape that lets an unmodified research
codebase, which resolves paths from its own file locations, find a data directory that is not
inside it. The tests below pin the decisions that make that work.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion import paths  # noqa: E402
from dandelion.paths import Install, junctions_for  # noqa: E402
from dandelion.provision import count_pinned  # noqa: E402


@pytest.fixture
def install(tmp_path: Path) -> Install:
    return Install(app_root=tmp_path / "app", data_root=tmp_path / "data")


# ---------------------------------------------------------------------------- layout

def test_each_release_gets_its_own_environment_code_and_store(install):
    """Phase 7 installs releases side by side; nothing may be shared between tags."""
    a, b = install.env_dir("v0.1.0"), install.env_dir("v0.2.0")
    assert a != b
    assert install.code_dir("v0.1.0") != install.code_dir("v0.2.0")
    assert install.store_dir("v0.1.0") != install.store_dir("v0.2.0")


def test_data_root_is_separable_from_the_app_root(tmp_path):
    """The data is the biggest thing on the machine; users put it on another drive."""
    inst = Install(app_root=tmp_path / "app", data_root=Path("D:/dandelion"))
    assert not str(inst.data_dir).startswith(str(inst.app_root))


def test_uv_directories_live_inside_the_install(install):
    """Otherwise uv writes to global per-user folders that survive an uninstall."""
    for directory in (install.runtime_dir, install.cache_dir):
        assert directory.is_relative_to(install.app_root)


def test_mastr_is_outside_the_install_and_that_is_recorded(install):
    """registries/mastr.py uses os.path.expanduser, so this one cannot be relocated."""
    assert not install.mastr_dir.is_relative_to(install.app_root)
    assert install.mastr_dir.name == ".open-MaStR"


# ---------------------------------------------------------------------------- junctions

def test_junctions_cover_every_store_that_must_leave_the_code_tree(install):
    links = {j.link.name for j in junctions_for(install, "v0.1.0")}
    assert links == {"data", "output", "scratchpad", "era5_cache"}


def test_junctions_point_from_inside_the_code_tree_to_the_data_root(install):
    for junction in junctions_for(install, "v0.1.0"):
        assert junction.link.is_relative_to(install.code_dir("v0.1.0")), junction.link
        assert junction.target.is_relative_to(install.data_root), junction.target
        assert junction.why, "a junction without a reason is a junction nobody can review"


def test_reports_and_models_are_never_junctioned(install):
    """They contain files that ship with the release; a junction would hide them.

    markup_model.json is the case that matters — dispatch loads it for every projected year,
    and its absence degrades silently to clipped SMC.
    """
    links = [str(j.link) for j in junctions_for(install, "v0.1.0")]
    assert not any(link.endswith("reports") for link in links)
    assert not any(link.endswith("models") for link in links)


def test_the_seeded_files_are_the_tracked_ones_inside_relocated_directories():
    assert "dispatch_model/reports/markup_model.json" in paths.SEEDED_FROM_RELEASE
    assert "availability_model/reports/methodology.md" in paths.SEEDED_FROM_RELEASE


def test_the_monte_carlo_scratchpad_is_linked(install):
    """run_montecarlo.py resolves cube paths against its cwd; 472 MB per retained draw."""
    scratch = next(j for j in junctions_for(install, "v0.1.0") if j.link.name == "scratchpad")
    assert scratch.link.parent.name == "dispatch_model"


# ---------------------------------------------------------------------------- location guards

def test_a_network_path_is_refused(monkeypatch):
    monkeypatch.setattr(paths, "is_network_path", lambda p: True)
    monkeypatch.setattr(paths, "is_sync_root", lambda p: None)
    monkeypatch.setattr(paths, "free_gb", lambda p: 500.0)
    verdict = paths.check_data_location(Path(r"\\server\share\dandelion"))
    assert not verdict.ok
    assert any("network" in b.lower() for b in verdict.blocking)


def test_a_sync_root_is_refused_and_the_provider_is_named(monkeypatch):
    monkeypatch.setattr(paths, "is_network_path", lambda p: False)
    monkeypatch.setattr(paths, "is_sync_root", lambda p: "OneDrive")
    monkeypatch.setattr(paths, "free_gb", lambda p: 500.0)
    # path-guard: allow - a synthetic fixture, not a path on anyone's machine
    verdict = paths.check_data_location(Path("C:/Users/x/OneDrive/dandelion"))  # path-guard: allow
    assert not verdict.ok
    assert any("OneDrive" in b for b in verdict.blocking)


def test_too_little_space_blocks_and_a_bit_little_only_warns(monkeypatch):
    monkeypatch.setattr(paths, "is_network_path", lambda p: False)
    monkeypatch.setattr(paths, "is_sync_root", lambda p: None)

    monkeypatch.setattr(paths, "free_gb", lambda p: 5.0)
    assert not paths.check_data_location(Path("C:/d")).ok

    monkeypatch.setattr(paths, "free_gb", lambda p: 38.0)
    verdict = paths.check_data_location(Path("C:/d"))
    assert verdict.ok and verdict.warnings


def test_a_good_location_passes_clean(monkeypatch):
    monkeypatch.setattr(paths, "is_network_path", lambda p: False)
    monkeypatch.setattr(paths, "is_sync_root", lambda p: None)
    monkeypatch.setattr(paths, "free_gb", lambda p: 500.0)
    verdict = paths.check_data_location(Path("D:/dandelion"))
    assert verdict.ok and not verdict.warnings and not verdict.blocking


def test_sync_root_detection_reads_the_onedrive_environment(monkeypatch, tmp_path):
    onedrive = tmp_path / "OneDrive - Contoso"
    (onedrive / "docs").mkdir(parents=True)
    monkeypatch.setenv("OneDrive", str(onedrive))
    assert paths.is_sync_root(onedrive / "docs") == "OneDrive"
    assert paths.is_sync_root(tmp_path / "elsewhere") is None


def test_sync_root_detection_falls_back_to_the_folder_name(monkeypatch, tmp_path):
    monkeypatch.delenv("OneDrive", raising=False)
    monkeypatch.delenv("OneDriveCommercial", raising=False)
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    assert paths.is_sync_root(Path("C:/Users/x/Dropbox/models")) == "Dropbox"  # path-guard: allow
    assert paths.is_sync_root(Path("C:/Users/x/Google Drive/models")) == "Google Drive"  # path-guard: allow


def test_a_non_ascii_path_warns_but_does_not_block(monkeypatch):
    monkeypatch.setattr(paths, "is_network_path", lambda p: False)
    monkeypatch.setattr(paths, "is_sync_root", lambda p: None)
    monkeypatch.setattr(paths, "free_gb", lambda p: 500.0)
    verdict = paths.check_data_location(Path("C:/Données/dandelion"))
    assert verdict.ok
    assert any("non-ASCII" in w for w in verdict.warnings)


# ---------------------------------------------------------------------------- lock counting

def test_pinned_count_ignores_uv_provenance_comments(tmp_path):
    """`uv pip compile` indents `# via ...` under each pin; counting raw lines triples it."""
    lock = tmp_path / "constraints.lock"
    lock.write_text(
        "# generated\n"
        "numpy==2.1.0\n"
        "    # via pandas\n"
        "    # via xarray\n"
        "pandas==2.2.0\n"
        "    # via -r requirements.in\n"
        "-e ./local\n",
        encoding="utf-8")
    assert count_pinned(lock) == 2
