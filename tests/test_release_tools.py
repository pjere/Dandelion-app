"""Unit tests for the release tooling's decision logic.

The parts worth pinning are the ones that decide whether a release is publishable: what
counts as a source build, what counts as a test failure, what goes into the lock, and what
the data packager is allowed to touch. Each of these silently waves something through if it
is wrong, which is the failure mode a release gate must not have.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_tools import release_code, release_data  # noqa: E402
from release_tools.import_scan import find_undeclared, local_module_names  # noqa: E402

# ------------------------------------------------------------------ source builds

def test_sdists_built_reads_uv_output():
    out = (
        "Resolved 118 packages in 1.2s\n"
        "   Building pymeeus==0.5.12\n"
        "      Built pymeeus==0.5.12 in 3.4s\n"
        "Installed 118 packages\n"
    )
    assert release_code.sdists_built(out) == {"pymeeus": "0.5.12"}


def test_sdists_built_is_empty_for_an_all_wheel_install():
    assert release_code.sdists_built("Installed 118 packages in 4s\n") == {}


def test_sdists_built_normalises_names_to_match_the_allowlist():
    got = release_code.sdists_built("   Building some_pkg==1.0\n")
    assert "some-pkg" in got


def test_the_one_allowlisted_sdist_states_why_it_is_safe():
    """An entry without a justification is how an unreviewed compiler dependency creeps in."""
    assert set(release_code.SDIST_ALLOWLIST) == {"pymeeus"}
    reason = release_code.SDIST_ALLOWLIST["pymeeus"]
    assert "pure-Python" in reason and "no compiler" in reason


# ------------------------------------------------------------------ pytest verdicts

def test_parse_pytest_failures_reads_failures_and_errors():
    out = (
        "FAILED tests/test_hydro.py::test_reservoir_climatology_from_db - pandas.errors...\n"
        "ERROR tests/test_fr_stack.py::test_stack_composition\n"
        "3 failed, 284 passed\n"
    )
    assert release_code.parse_pytest_failures(out) == {
        "tests/test_hydro.py::test_reservoir_climatology_from_db",
        "tests/test_fr_stack.py::test_stack_composition",
    }


def test_parse_pytest_failures_ignores_ordinary_output():
    assert release_code.parse_pytest_failures("284 passed, 12 skipped in 9.03s\n") == set()


def test_only_the_genuinely_failing_test_is_allowlisted():
    """The other eleven skip on a clean tree; allowlisting them would hide regressions."""
    assert len(release_code.DATA_DEPENDENT_TESTS) == 1
    key = next(iter(release_code.DATA_DEPENDENT_TESTS))
    assert key.startswith("dispatch_model:")
    assert "test_neighbour_thermal_blocks_carry_must_run_floor" in key


def test_stale_db_set_and_allowlist_do_not_overlap():
    allowlisted = {k.split(":", 1)[1] for k in release_code.DATA_DEPENDENT_TESTS}
    assert not (allowlisted & release_code.STALE_DB_SENSITIVE_TESTS)


# ------------------------------------------------------------------ the lock inputs

def _release_tree(tmp_path: Path) -> Path:
    """A minimal stand-in for an extracted release: two packages and a requirements file."""
    root = tmp_path / "tree"
    for pkg, body in {
        "powersim_core": '[project]\nname="powersim-core"\ndependencies=["numpy>=1.26"]\n',
        "pricemodeling": '[project]\nname="pricemodeling"\ndependencies=["typer>=0.9"]\n',
        "weathergen": (
            '[project]\nname="weathergen"\ndependencies=["scipy>=1.11"]\n'
            '[project.optional-dependencies]\n'
            'stats=["statsmodels>=0.14","xclim>=0.50"]\nviz=["matplotlib>=3.8"]\n'
        ),
        "demand_model": (
            '[project]\nname="demand_model"\ndependencies=["pvlib>=0.10"]\n'
            '[project.optional-dependencies]\ncalib=["pygam>=0.9"]\nviz=["jinja2>=3.1"]\n'
        ),
        "res_model": '[project]\nname="res_model"\ndependencies=["pyarrow>=14"]\n',
        "availability_model": '[project]\nname="availability_model"\ndependencies=["pandera>=0.18"]\n',
        "dispatch_model": '[project]\nname="dispatch_model"\ndependencies=["linopy>=0.3"]\n',
    }.items():
        (root / pkg).mkdir(parents=True)
        (root / pkg / "pyproject.toml").write_text(body, encoding="utf-8")
    (root / "requirements.txt").write_text("tqdm>=4.66\n# a comment\nduckdb>=1.0\n",
                                           encoding="utf-8")
    return root


def test_lock_inputs_include_the_extras_the_fits_need(tmp_path):
    """A lock built from `dependencies` alone dies at the first model fit."""
    specs, _ = release_code.collect_requirements(_release_tree(tmp_path))
    joined = " ".join(specs)
    for needed in ("statsmodels", "xclim", "pygam", "matplotlib", "jinja2"):
        assert needed in joined, needed


def test_lock_inputs_include_the_undeclared_three(tmp_path):
    specs, _ = release_code.collect_requirements(_release_tree(tmp_path))
    joined = " ".join(specs)
    for needed in ("cdsapi", "entsoe-py", "open-mastr"):
        assert needed in joined, needed


def test_lock_inputs_include_upstreams_own_requirements(tmp_path):
    specs, _ = release_code.collect_requirements(_release_tree(tmp_path))
    joined = " ".join(specs)
    assert "tqdm>=4.66" in joined and "duckdb>=1.0" in joined
    assert "# a comment" not in joined


def test_lock_inputs_are_deduplicated(tmp_path):
    specs, _ = release_code.collect_requirements(_release_tree(tmp_path))
    assert len(specs) == len(set(specs))


def test_a_missing_package_fails_loudly(tmp_path):
    root = _release_tree(tmp_path)
    (root / "dispatch_model" / "pyproject.toml").unlink()
    with pytest.raises(SystemExit, match="dispatch_model"):
        release_code.collect_requirements(root)


def test_a_vanished_extra_is_recorded_rather_than_ignored(tmp_path):
    root = _release_tree(tmp_path)
    (root / "weathergen" / "pyproject.toml").write_text(
        '[project]\nname="weathergen"\ndependencies=["scipy>=1.11"]\n', encoding="utf-8")
    _, provenance = release_code.collect_requirements(root)
    assert any("no longer declares the [stats] extra" in line for line in provenance)


# ------------------------------------------------------------------ the import scan

def test_scripts_siblings_are_not_mistaken_for_dependencies(tmp_path):
    """run_montecarlo.py imports mc_weather, a FILE beside it, not a distribution."""
    root = _release_tree(tmp_path)
    scripts = root / "dispatch_model" / "scripts"
    scripts.mkdir()
    (scripts / "mc_weather.py").write_text("X = 1\n", encoding="utf-8")
    (scripts / "run_montecarlo.py").write_text("from mc_weather import X\n", encoding="utf-8")
    assert "mc_weather" in local_module_names(root)
    assert "mc_weather" not in find_undeclared(root)


def test_a_dev_only_import_declared_in_requirements_dev_is_not_flagged(tmp_path):
    root = _release_tree(tmp_path)
    (root / "requirements-dev.txt").write_text("pdoc>=14.0\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "build_docs.py").write_text("import pdoc\n", encoding="utf-8")
    assert "pdoc" not in find_undeclared(root)


def test_a_genuinely_undeclared_import_is_flagged(tmp_path):
    root = _release_tree(tmp_path)
    (root / "pricemodeling" / "series.py").write_text("import entsoe\n", encoding="utf-8")
    assert "entsoe" in find_undeclared(root)


def test_stdlib_and_relative_imports_are_never_flagged(tmp_path):
    root = _release_tree(tmp_path)
    (root / "pricemodeling" / "x.py").write_text(
        "import json, pathlib\nfrom .config import load\n", encoding="utf-8")
    found = find_undeclared(root)
    assert "json" not in found and "pathlib" not in found and "config" not in found


def test_test_files_are_not_scanned(tmp_path):
    """A test importing pytest must not make the release think pytest is a runtime dep."""
    root = _release_tree(tmp_path)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("import some_test_only_package\n", encoding="utf-8")
    assert "some_test_only_package" not in find_undeclared(root)


# ------------------------------------------------------------------ the data packager

def test_worksheet_parses_and_starts_unsigned():
    sheet = release_data.load_worksheet()
    assert sheet["signed_off"] is False
    assert sheet["stores"] and all("ship" in s for s in sheet["stores"])
    assert all(s["ship"] == "pending" for s in sheet["stores"])


def test_every_store_names_its_sources_and_a_licence_to_check():
    for store in release_data.load_worksheet()["stores"]:
        assert store.get("sources"), store["key"]
        for src in store["sources"]:
            assert src.get("name") and src.get("licence"), store["key"]


def test_tracked_files_are_excluded_from_a_store(tmp_path):
    """markup_model.json ships with the code release; it must never enter a data snapshot."""
    source = tmp_path
    (source / "dispatch_model" / "reports").mkdir(parents=True)
    (source / "dispatch_model" / "reports" / "markup_model.json").write_text("{}", encoding="utf-8")
    (source / "dispatch_model" / "reports" / "projection.parquet").write_bytes(b"\x00" * 100)
    store = {"key": "dispatch_reports", "path": "dispatch_model/reports", "ship": "yes"}
    tracked = {"dispatch_model/reports/markup_model.json"}

    plan = release_data.plan_store(source, store, tracked, [])
    assert plan.files == 1 and plan.excluded_tracked == 1
    assert plan.bytes == 100


def test_source_files_are_excluded_even_when_untracked(tmp_path):
    source = tmp_path
    (source / "data").mkdir()
    (source / "data" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    (source / "data" / "notes.md").write_text("hi\n", encoding="utf-8")
    (source / "data" / "series.parquet").write_bytes(b"\x00" * 50)
    plan = release_data.plan_store(source, {"key": "d", "path": "data", "ship": "yes"}, set(), [])
    assert plan.files == 1 and plan.excluded_source == 2


def test_excluded_globs_are_honoured(tmp_path):
    source = tmp_path
    (source / "dispatch_model" / "scratchpad").mkdir(parents=True)
    (source / "dispatch_model" / "scratchpad" / "cube.nc").write_bytes(b"\x00" * 10)
    store = {"key": "s", "path": "dispatch_model", "ship": "yes"}
    plan = release_data.plan_store(source, store, set(), ["dispatch_model/scratchpad"])
    assert plan.files == 0


def test_an_absent_store_is_reported_not_silently_zero(tmp_path):
    plan = release_data.plan_store(tmp_path, {"key": "x", "path": "nope", "ship": "yes"},
                                   set(), [])
    assert plan.files == 0 and any("absent" in n for n in plan.notes)


def test_packaging_refuses_while_the_worksheet_is_unsigned(tmp_path, capsys):
    sheet = release_data.load_worksheet()
    sheet["signed_off"] = False
    path = tmp_path / "sheet.yaml"
    path.write_text(yaml.safe_dump(sheet), encoding="utf-8")
    rc = release_data.main(["--source", str(tmp_path), "--worksheet", str(path)])
    assert rc == 1
    assert "REFUSING to package" in capsys.readouterr().out


def test_dry_run_writes_nothing(tmp_path, capsys):
    rc = release_data.main(["--source", str(tmp_path), "--dry-run"])
    assert rc == 0
    assert "dry run - nothing written" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_chunk_limit_stays_under_the_github_asset_cap():
    assert release_data.CHUNK_BYTES < 2_000_000_000
