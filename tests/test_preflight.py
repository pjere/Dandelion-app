"""The CMIP6 and markup guards, and the registry consistency they depend on.

These tests encode a specific upstream behaviour: with the climate trend enabled and the
deltas npz missing, `weathergen simulate` emits one line and produces an UNTRENDED cube
while its own provenance claims otherwise. The guard's job is to make that unreachable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers import preflight  # noqa: E402
from drivers.inventory import IMPLEMENTED_PREFLIGHTS, JOBS, job, validate_registry  # noqa: E402

TREND_ON = {
    "run": {"models_dir": "models"},
    "trend": {"enabled": True, "ssp": "ssp245", "target_year": 2050, "cmip6_deltas_path": None},
}
TREND_OFF = {
    "run": {"models_dir": "models"},
    "trend": {"enabled": False, "ssp": "ssp245", "target_year": 2050, "cmip6_deltas_path": None},
}


def _config(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return p


def _deltas(tmp_path: Path, ssp="ssp245", year=2050, model=preflight.CMIP6_DEFAULT_MODEL) -> Path:
    d = tmp_path / "models"
    d.mkdir(exist_ok=True)
    f = d / preflight.deltas_filename(ssp, year, model)
    f.write_bytes(b"not really an npz")
    return f


# --------------------------------------------------------------------------------- naming

def test_filename_matches_what_upstream_looks_for():
    """cli.py:101 builds f'cmip6_deltas_{ssp}_{target_year}_{DEFAULT_MODEL}.npz'."""
    assert preflight.deltas_filename("ssp245", 2050) == "cmip6_deltas_ssp245_2050_mpi_esm1_2_lr.npz"


def test_default_model_is_mpi_not_the_stale_cli_help():
    """The --model help still says ec_earth3; the filename embeds the real default."""
    assert preflight.CMIP6_DEFAULT_MODEL == "mpi_esm1_2_lr"
    assert "ec_earth3" not in preflight.deltas_filename("ssp585", 2040)


# --------------------------------------------------------------------------------- checks

def test_trend_off_needs_no_deltas(tmp_path):
    r = preflight.check_cmip6_deltas(_config(tmp_path, TREND_OFF))
    assert r.ok and "disabled" in r.reason


def test_trend_on_without_deltas_is_refused(tmp_path):
    r = preflight.check_cmip6_deltas(_config(tmp_path, TREND_ON))
    assert not r.ok
    assert r.remedy_job == "weathergen-cmip6"
    assert r.remedy_args["ssp"] == "ssp245" and r.remedy_args["target_year"] == 2050
    # the message must say what would silently happen, not just "file missing"
    assert "NO climate adjustment" in r.reason


def test_trend_on_with_deltas_passes(tmp_path):
    cfg = _config(tmp_path, TREND_ON)
    _deltas(tmp_path)
    r = preflight.check_cmip6_deltas(cfg)
    assert r.ok and r.detail["path"].endswith("cmip6_deltas_ssp245_2050_mpi_esm1_2_lr.npz")


def test_simulate_time_overrides_change_which_file_is_required(tmp_path):
    """--ssp / --target-year are simulate-time inputs: one fitted model, many scenarios."""
    cfg = _config(tmp_path, TREND_ON)
    _deltas(tmp_path, "ssp245", 2050)          # the config default is present...
    r = preflight.check_cmip6_deltas(cfg, ssp="ssp585", target_year=2040)
    assert not r.ok                             # ...but the requested scenario is not
    assert r.remedy_args == {"ssp": "ssp585", "target_year": 2040,
                             "model": preflight.CMIP6_DEFAULT_MODEL}
    _deltas(tmp_path, "ssp585", 2040)
    assert preflight.check_cmip6_deltas(cfg, ssp="ssp585", target_year=2040).ok


def test_an_explicit_deltas_path_is_cwd_anchored_not_config_anchored(tmp_path):
    """The odd one out, and this test used to assert the wrong thing.

    Every other path in a weathergen config is resolved relative to the config FILE —
    `Config.resolve` (config.py:35) and `models_dir` (config.py:43) both do. An explicit
    `trend.cmip6_deltas_path` passes through NEITHER: cli.py hands it straight to trend.fit,
    which tests it with a bare `Path(path).exists()` (trend.py:94) — relative to the
    process's working directory. Anchoring it to the config here would pass on a file
    upstream cannot find."""
    payload = {**TREND_ON, "trend": {**TREND_ON["trend"], "cmip6_deltas_path": "custom/d.npz"}}
    cfg = _config(tmp_path, payload)
    elsewhere = tmp_path / "run"
    elsewhere.mkdir()

    # Sitting next to the CONFIG is not enough — upstream would not look there.
    beside_config = tmp_path / "custom" / "d.npz"
    beside_config.parent.mkdir()
    beside_config.write_bytes(b"x")
    assert not preflight.check_cmip6_deltas(cfg, cwd=elsewhere).ok

    # Sitting under the working directory is what counts.
    (elsewhere / "custom").mkdir()
    (elsewhere / "custom" / "d.npz").write_bytes(b"x")
    assert preflight.check_cmip6_deltas(cfg, cwd=elsewhere).ok


def test_a_run_overlay_moves_the_dispatch_reports_directory(tmp_path):
    """`Config.reports_dir` is `(config.parent / run.reports_dir)` (dispatch_model
    config.py:74), so a run bundle's overlay moves it. Hardcoding <code>/dispatch_model
    /reports is right only for the shipped config, and `dispatch-run` takes -c precisely
    so it can be given another."""
    import yaml

    overlay = tmp_path / "runs" / "r1" / "config.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(yaml.safe_dump({"run": {"reports_dir": "reports"}}), encoding="utf-8")
    assert preflight.dispatch_reports_dir(overlay) == (overlay.parent / "reports").resolve()

    absolute = tmp_path / "shared"
    overlay.write_text(yaml.safe_dump({"run": {"reports_dir": str(absolute)}}),
                       encoding="utf-8")
    assert preflight.dispatch_reports_dir(overlay) == absolute


def test_models_dir_is_anchored_to_the_config_file_not_the_cwd(tmp_path):
    sub = tmp_path / "weathergen"
    sub.mkdir()
    cfg = _config(sub, TREND_ON)
    assert preflight.models_dir_of(cfg) == (sub / "models").resolve()


def test_absolute_models_dir_passes_through(tmp_path):
    """Upstream: `p if p.is_absolute() else (config.parent / p)` — this is what lets the
    Phase 5 run overlay redirect outputs without a junction."""
    elsewhere = tmp_path / "elsewhere"
    cfg = _config(tmp_path, {"run": {"models_dir": str(elsewhere)}, "trend": {"enabled": False}})
    assert preflight.models_dir_of(cfg) == elsewhere


def test_missing_config_is_refused_not_ignored(tmp_path):
    r = preflight.check_cmip6_deltas(tmp_path / "nope.yaml")
    assert not r.ok


def test_markup_guard(tmp_path):
    assert not preflight.check_markup_model(tmp_path).ok
    (tmp_path / "markup_model.json").write_text("{}", encoding="utf-8")
    r = preflight.check_markup_model(tmp_path)
    assert r.ok


def test_markup_message_explains_the_silent_consequence(tmp_path):
    r = preflight.check_markup_model(tmp_path)
    assert "clipped SMC" in r.reason and "silently" in r.reason


# ------------------------------------------------------------------------------- registry

def test_registry_is_internally_consistent():
    assert validate_registry() == []


def test_the_deltas_guard_is_attached_where_deltas_are_consumed():
    """_build_trend runs in cmd_simulate, never in cmd_fit — the guard must follow it."""
    assert "cmip6-deltas-present" in job("weathergen-simulate").preflight
    assert "cmip6-deltas-present" not in job("weathergen-fit").preflight
    assert job("weathergen-simulate").requires == ("weathergen-fit",)


def test_monte_carlo_inherits_the_guard():
    """Every draw regenerates a cube through weathergen, so the same trap applies."""
    assert "cmip6-deltas-present" in job("montecarlo").preflight


def test_missing_deltas_is_a_failure_not_a_warning_on_simulate():
    j = job("weathergen-simulate")
    assert any("deltas not found" in m for m in j.failure_markers)
    assert not any("deltas not found" in m for m in j.warning_markers)


def test_projection_jobs_guard_the_markup_wedge():
    for jid in ("dispatch-run", "projection-20y", "montecarlo"):
        assert "markup-model-present" in job(jid).preflight, jid


@pytest.mark.parametrize("j", JOBS, ids=lambda j: j.id)
def test_every_declared_preflight_is_implemented(j):
    for check in j.preflight:
        assert check in IMPLEMENTED_PREFLIGHTS


def test_unknown_job_lookup_is_loud():
    with pytest.raises(KeyError):
        job("no-such-job")


def test_the_cluster_job_covers_every_zone_the_eight_zone_backfill_misses():
    """`backfill-entsoe` passes no zones argument, so it ingests series.ZONES — eight. The
    dispatch model reads thirteen more through its four virtual clusters, and nothing
    shipped fetches them. Measured cost on the 2019 backtest: DE_LU 41% low with 466
    negative hours against 210 observed."""
    from drivers.inventory import ENTSOE_CLUSTERS_RUNNER

    assert "ALL_ZONES" in ENTSOE_CLUSTERS_RUNNER
    for call in ("ingest_load", "ingest_generation", "ingest_installed_capacity"):
        assert f"S.{call}(eng,cl,s,e,zones=Z)" in ENTSOE_CLUSTERS_RUNNER, call
    assert job("backfill-entsoe-clusters").needs_credentials == ("ENTSOE_TOKEN",)
    assert job("backfill-entsoe-clusters").resumable


def test_the_cluster_job_fetches_italian_capacity_at_control_area_level():
    """Verified against the live API for 2019: `IT` returns 18 technologies and 94,373 MW;
    IT_NORD, IT_CNOR, IT_CSUD, IT_SUD, IT_SICI, IT_SARD and IT_CALA all raise
    NoMatchingDataError. ALL_ZONES therefore cannot reach it, and every Italian technology
    falls through to the generation proxy."""
    from drivers.inventory import ENTSOE_CLUSTERS_RUNNER

    assert "CONTROL={'IT':'IT'}" in ENTSOE_CLUSTERS_RUNNER
    assert "zones=CONTROL" in ENTSOE_CLUSTERS_RUNNER


def test_we_do_not_enable_the_allocation_upstream_switched_off():
    """DECISIONS.md:985 measured DISPATCH_AREA_CAPACITY and left it off: IT_NORTH 2019 goes
    -2.1 to -13.7, 2022 -3.0 to -22.4. Fetching the data is ours to do; flipping a modelling
    switch the author tested and rejected is not."""
    from pathlib import Path

    from drivers import inventory

    source = Path(inventory.__file__).read_text(encoding="utf-8")
    assert "DISPATCH_AREA_CAPACITY" in source, "the decision must be recorded"
    assert '"DISPATCH_AREA_CAPACITY"' not in source, "and never set as an env var"


# --------------------------------------------------------------- dependency edges

def test_every_projection_requires_its_own_calibration():
    """Each edge is a real `.load()` of a file the other job writes, checked in the release
    tree rather than assumed from the naming:

        demand   projection/engine.py:125-126  calibrated.json, residual.json
        res      projection/engine.py:64-65    calibrated_res.json, residual_res.json
        avail    projection/engine.py:143      calibrated_availability.json
    """
    for projection, calibration in (("demand-project", "demand-calibrate"),
                                    ("res-project", "res-calibrate"),
                                    ("avail-project", "avail-calibrate")):
        assert calibration in job(projection).requires, projection


def test_the_weather_dependent_projections_need_a_simulated_cube():
    """demand: 'from weathergen weather'; res: 'from the weather draws ... coherent with
    demand'. Availability draws outages from its fitted process and needs no cube."""
    for weather_dependent in ("demand-project", "res-project"):
        assert "weathergen-simulate" in job(weather_dependent).requires
    assert "weathergen-simulate" not in job("avail-project").requires


def test_no_dependency_cycles():
    """A cycle would make both jobs permanently unrunnable, and the refusal message would
    point each at the other."""
    seen: dict[str, int] = {}

    def visit(job_id: str, stack: tuple[str, ...]) -> None:
        assert job_id not in stack, f"cycle: {' -> '.join(stack + (job_id,))}"
        if seen.get(job_id):
            return
        for dep in job(job_id).requires:
            visit(dep, stack + (job_id,))
        seen[job_id] = 1

    for j in JOBS:
        visit(j.id, ())
