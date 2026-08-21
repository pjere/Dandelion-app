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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_tools import release_code  # noqa: E402
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


# ------------------------------------------------------------------ the wheel audit

def test_parse_no_wheel_reads_the_resolver_diagnostic():
    out = (
        "  x No solution found when resolving dependencies:\n"
        "  |-> Because pymeeus==0.5.12 has no usable wheels and you require\n"
        "      pymeeus==0.5.12, we can conclude that your requirements are\n"
        "      unsatisfiable.\n"
    )
    assert release_code.parse_no_wheel(out) == {"pymeeus": "0.5.12"}


def test_parse_no_wheel_is_empty_on_success():
    assert release_code.parse_no_wheel("Would install 118 packages\n") == {}


def test_audit_exempts_exactly_the_allowlist(monkeypatch, tmp_path):
    """The audit must ask for wheels everywhere and exempt only reviewed packages."""
    seen: dict = {}

    def fake_run(argv, cwd=None, timeout=1800, env=None):
        seen["argv"] = argv
        return 0, ""

    monkeypatch.setattr(release_code, "run", fake_run)
    ok, detail = release_code.audit_wheels(["uv"], tmp_path / "python.exe", tmp_path / "lock")
    assert ok
    argv = seen["argv"]
    assert "--only-binary" in argv and ":all:" in argv
    exempted = [argv[i + 1] for i, a in enumerate(argv) if a == "--no-binary"]
    assert exempted == list(release_code.SDIST_ALLOWLIST)
    assert "pymeeus" in detail


def test_audit_names_the_offender_and_says_what_to_do(monkeypatch, tmp_path):
    def fake_run(argv, cwd=None, timeout=1800, env=None):
        return 1, "Because somepkg==2.0 has no usable wheels and you require somepkg==2.0"

    monkeypatch.setattr(release_code, "run", fake_run)
    ok, detail = release_code.audit_wheels(["uv"], tmp_path / "python.exe", tmp_path / "lock")
    assert not ok
    assert "somepkg==2.0" in detail
    assert "no compiler" in detail and "SDIST_ALLOWLIST" in detail


def test_audit_falls_back_to_raw_output_when_it_cannot_parse(monkeypatch, tmp_path):
    def fake_run(argv, cwd=None, timeout=1800, env=None):
        return 1, "network unreachable"

    monkeypatch.setattr(release_code, "run", fake_run)
    ok, detail = release_code.audit_wheels(["uv"], tmp_path / "python.exe", tmp_path / "lock")
    assert not ok and "network unreachable" in detail


# ------------------------------------------------------------------ the fits packager

from release_tools import release_fits  # noqa: E402


def _models(tmp_path: Path, package: str) -> Path:
    d = tmp_path / package / "models"
    d.mkdir(parents=True)
    return d


def test_array_placeholder_detection():
    assert release_fits.ARRAY_TAG == "__ndarray__"


def test_json_referencing_arrays_needs_a_sidecar(tmp_path):
    p = tmp_path / "f.json"
    p.write_text('{"a": {"__ndarray__": "a0"}}', encoding="utf-8")
    assert release_fits.references_arrays(p)


def test_json_without_arrays_needs_no_sidecar(tmp_path):
    p = tmp_path / "f.json"
    p.write_text('{"a": [1, 2, 3]}', encoding="utf-8")
    assert not release_fits.references_arrays(p)


def test_a_fit_missing_its_sidecar_is_reported_as_broken(tmp_path):
    """The exact state the owner's weathergen fit was in: 563 array refs, no npz."""
    d = _models(tmp_path, "availability_model")
    (d / "calibrated_availability.json").write_text('{"x": {"__ndarray__": "a0"}}',
                                                    encoding="utf-8")
    sets = release_fits.collect(tmp_path, hash_files=False)
    avail = next(fs for fs in sets if fs.package == "availability_model")
    assert avail.artifacts == []
    assert any("BROKEN" in p and "cannot be loaded" in p for p in avail.problems)


def test_a_complete_fit_pairs_the_json_with_its_sidecar(tmp_path):
    d = _models(tmp_path, "availability_model")
    (d / "calibrated_availability.json").write_text('{"x": {"__ndarray__": "a0"}}',
                                                    encoding="utf-8")
    (d / "calibrated_availability.json.npz").write_bytes(b"\x00" * 32)
    sets = release_fits.collect(tmp_path, hash_files=False)
    avail = next(fs for fs in sets if fs.package == "availability_model")
    assert [a.name for a in avail.artifacts] == [
        "calibrated_availability.json", "calibrated_availability.json.npz"]
    assert avail.problems == []


def test_sidecarless_json_is_fine_when_it_holds_no_arrays(tmp_path):
    d = _models(tmp_path, "availability_model")
    (d / "calibrated_availability.json").write_text('{"x": 1}', encoding="utf-8")
    sets = release_fits.collect(tmp_path, hash_files=False)
    avail = next(fs for fs in sets if fs.package == "availability_model")
    assert [a.name for a in avail.artifacts] == ["calibrated_availability.json"]
    assert avail.problems == []


def test_backups_and_stale_caches_are_never_shipped(tmp_path):
    for path, why in [
        (Path("x/models/fitted_pre_seasonal.pkl.bak"), ".bak"),
        (Path("x/models/backup_fr42/fitted.json"), "backup dir"),
        (Path("x/models/_gauss_cache.npz"), "unreferenced cache"),
    ]:
        assert release_fits.is_excluded(path) is not None, why


def test_a_real_artifact_is_not_excluded():
    assert release_fits.is_excluded(Path("weathergen/models/fitted.json")) is None


def test_missing_declared_artifact_is_reported(tmp_path):
    _models(tmp_path, "res_model")
    sets = release_fits.collect(tmp_path, hash_files=False)
    res = next(fs for fs in sets if fs.package == "res_model")
    assert any("missing: res_model/models/calibrated_res.json" in p for p in res.problems)


def test_cmip6_deltas_are_collected_by_pattern(tmp_path):
    d = _models(tmp_path, "weathergen")
    (d / "fitted.json").write_text('{"x": 1}', encoding="utf-8")
    (d / "wind100.json").write_text('{"x": 1}', encoding="utf-8")
    (d / "cmip6_deltas_ssp245_2050_mpi_esm1_2_lr.npz").write_bytes(b"\x00" * 8)
    sets = release_fits.collect(tmp_path, hash_files=False)
    wg = next(fs for fs in sets if fs.package == "weathergen")
    assert "cmip6_deltas_ssp245_2050_mpi_esm1_2_lr.npz" in [a.name for a in wg.artifacts]


def test_dispatch_markup_is_deliberately_not_packaged():
    """It is tracked and ships with the code; two copies would have no tie-break rule."""
    assert "dispatch_model" in release_fits.NOT_PACKAGED
    assert "dispatch_model" not in release_fits.FIT_SETS


def test_fits_require_a_code_tag(capsys):
    with pytest.raises(SystemExit):
        release_fits.main(["--source", "."])


def test_an_artifact_larger_than_a_chunk_is_flagged():
    """It cannot be split, so its published size rides on its own compressibility."""
    big = release_fits.Artifact(package="weathergen", name="fitted.json.npz",
                                bytes=release_fits.CHUNK_BYTES + 1)
    small = release_fits.Artifact(package="res_model", name="calibrated_res.json", bytes=8000)
    fs = release_fits.FitSet(package="x", artifacts=[big, small])
    assert release_fits.oversized_artifacts([fs]) == [big]


def test_the_publishing_limit_is_the_github_asset_cap():
    assert release_fits.ASSET_LIMIT == 2_000_000_000
    assert release_fits.CHUNK_BYTES < release_fits.ASSET_LIMIT
