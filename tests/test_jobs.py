"""The job engine: progress it can trust, failures it must not miss.

The defining test here is `test_a_failure_that_exits_zero_is_reported_as_a_failure`. Upstream's
`extract-rte` and `extract-entsoe` catch their own exceptions, print `[ERREUR] …` and return 0.
An engine trusting the exit code would tell the user their ingest succeeded, and they would
discover otherwise days later when a projection came back short.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion.jobs import (  # noqa: E402
    JobResult,
    parse_progress,
    render_argv,
    summarise,
)
from dandelion.paths import Install  # noqa: E402
from drivers.inventory import job as find_job  # noqa: E402


@pytest.fixture
def install(tmp_path: Path) -> Install:
    return Install(app_root=tmp_path / "app", data_root=tmp_path / "data")


# ------------------------------------------------------------------------- progress

def test_a_progress_line_becomes_a_fraction_and_an_eta():
    p = parse_progress("[2031 windows] 12/52  23%  elapsed 0:04:10  eta 0:14:35  20.8 s/it")
    assert p is not None
    assert p.label == "2031 windows" and p.done == 12 and p.total == 52
    assert p.eta == "0:14:35"
    assert abs(p.fraction - 12 / 52) < 1e-9


def test_a_counter_line_has_no_fraction():
    """`total <= 0` upstream means the work length is unknown - so no percentage is invented."""
    p = parse_progress("[ingest] 42 done  elapsed 0:01:00  1.4 s/it")
    assert p is not None and p.fraction is None and p.done == 42


def test_ordinary_output_is_not_progress():
    for line in ("Base : C:/data/pricemodeling.db",
                 "[ERREUR] ENTSO-E: HTTP 401",
                 "  master_hourly    105,192 lignes"):
        assert parse_progress(line) is None


def test_the_note_survives_parsing():
    p = parse_progress("[deliverables] 1/20   5%  elapsed 0:00:01  eta 0:00:19  ?  2027 done")
    assert p.note == "2027 done"


# --------------------------------------------------------------------------- argv

def test_python_is_the_release_environment_not_whatever_is_on_path(install):
    argv = render_argv(find_job("status"), install, "v0.1.0", {})
    assert argv[0] == str(install.python("v0.1.0"))


def test_a_console_script_resolves_inside_the_environment(install):
    """PATH on a user's machine may hold an entirely different Python."""
    argv = render_argv(find_job("dispatch-backtest"), install, "v0.1.0", {"year": 2019})
    assert argv[0] == str(install.env_dir("v0.1.0") / "Scripts" / "dispatch-model.exe")


def test_parameters_are_substituted(install):
    argv = render_argv(find_job("dispatch-backtest"), install, "v0.1.0", {"year": 2019})
    assert "2019" in argv


def test_a_missing_parameter_is_refused_rather_than_passed_through(install):
    """Passing a literal '{year}' to the model would be a confusing failure much later."""
    with pytest.raises(ValueError, match="year"):
        render_argv(find_job("dispatch-backtest"), install, "v0.1.0", {})


# ------------------------------------------------------------------------- outcomes

def make(**kwargs) -> JobResult:
    base = {"job_id": "x", "argv": [], "cwd": ".", "log_path": "x.log",
            "started_at": "2026-08-24T09:00:00+00:00"}
    return JobResult(**{**base, **kwargs})


def test_a_clean_run_is_a_success():
    assert make(exit_code=0, outcome="succeeded").ok


def test_a_failure_that_exits_zero_is_reported_as_a_failure():
    """The whole reason failure markers exist. Verified against real upstream output:
    `extract-entsoe` with no token exits 0 and prints `[ERREUR] ENTSO-E: …`."""
    result = make(exit_code=0, outcome="failed",
                  failure_lines=["[ERREUR] ENTSO-E: Token ENTSO-E manquant."])
    assert not result.ok
    assert result.outcome == "failed"


def test_every_swallowing_command_declares_a_failure_marker():
    """These are the commands measured to exit 0 on error."""
    for job_id in ("extract-rte", "extract-entsoe", "ingest-remit"):
        assert find_job(job_id).failure_markers, job_id


def test_the_untrended_cube_line_is_a_failure_not_a_warning():
    """It produces a present-day cube labelled as the target year - a wrong answer, not a note."""
    job = find_job("weathergen-simulate")
    assert any("deltas not found" in m for m in job.failure_markers)
    assert not any("deltas not found" in m for m in job.warning_markers)


# ------------------------------------------------------------------------ summaries

def test_summaries_read_as_plain_english():
    assert summarise(make(outcome="succeeded", duration_s=45)) == "finished in 45s"
    assert summarise(make(outcome="succeeded", duration_s=125)) == "finished in 2m 05s"
    assert summarise(make(outcome="failed", duration_s=5)) == "failed after 5s"
    assert summarise(make(outcome="cancelled", duration_s=90)) == "cancelled after 1m 30s"
    assert summarise(make(outcome="refused")) == "did not start"


def test_a_run_with_warnings_says_so():
    result = make(outcome="succeeded", duration_s=10, warning_lines=["[trend] something"])
    assert "warnings" in summarise(result)


# ------------------------------------------------------------------- optional argv

def test_optional_arguments_are_omitted_when_not_supplied(install):
    """`extract-rte` with no dates must run over each resource's whole declared history.
    An empty or literal `--start` would be a silently different ingest."""
    argv = render_argv(find_job("extract-rte"), install, "v0.1.0", {})
    assert "--start" not in argv and "--end" not in argv
    assert argv[-1] == "extract-rte"


def test_optional_arguments_appear_when_supplied(install):
    argv = render_argv(find_job("extract-rte"), install, "v0.1.0",
                       {"start": "2019-01-01", "end": "2020-01-01"})
    assert argv[-4:] == ["--start", "2019-01-01", "--end", "2020-01-01"]


def test_each_optional_flag_is_independent(install):
    """Upstream treats --start and --end separately: `--start` alone means "from here to
    the resource's natural end", which is a real query. Each flag is its own group so that
    supplying one never drags in a half-rendered other."""
    argv = render_argv(find_job("extract-rte"), install, "v0.1.0", {"start": "2019-01-01"})
    assert argv[-2:] == ["--start", "2019-01-01"]
    assert "--end" not in argv


def test_an_unrelated_parameter_does_not_trigger_a_group(install):
    argv = render_argv(find_job("extract-rte"), install, "v0.1.0", {"force": "yes"})
    assert "--start" not in argv and "--only" not in argv


def test_optional_groups_do_not_disturb_required_placeholders(install):
    argv = render_argv(find_job("dispatch-backtest"), install, "v0.1.0", {"year": 2019})
    assert "2019" in argv


def test_parameters_reach_a_runner_through_the_environment(install, monkeypatch):
    """`backfill-entsoe-extras` invokes module-level upstream functions with `python -c`.
    The runner is a constant, so its year arrives as DANDELION_YEAR rather than argv."""
    import dandelion.jobs as jobs

    captured = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured.update(kwargs.get("env") or {})
            self.stdout, self.returncode, self.pid = iter(()), 0, 0

        def wait(self, *a, **k):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(jobs.subprocess, "Popen", FakePopen)
    engine = jobs.JobEngine(install, "v0.1.0")
    engine.run("backfill-entsoe-extras", {"year": 2019}, preflight=lambda _: None)
    assert captured.get("DANDELION_YEAR") == "2019"


# ------------------------------------------------------------------- credentials

def test_a_job_without_its_credentials_is_refused(install, monkeypatch):
    """`needs_credentials` was declared on nine jobs and read in exactly two places, both
    of which only PRINT it. The job started anyway — and upstream's answer to a missing
    token is not reliably an error: `_do` records an empty ENTSO-E payload as status='ok',
    so the run could report success having written nothing."""
    import dandelion.jobs as jobs

    monkeypatch.setattr(jobs.credentials, "status_by_field", lambda: {})
    engine = jobs.JobEngine(install, "v0.1.0")
    result = engine.run("backfill-entsoe", {"years": 2019})
    assert result.outcome == "refused"
    assert "ENTSO-E" in result.reason
    assert "no data has changed" in result.reason


def test_a_job_with_its_credentials_is_not_blocked_by_that_check(install, monkeypatch):
    import dandelion.jobs as jobs

    monkeypatch.setattr(jobs.credentials, "status_by_field",
                        lambda: {"ENTSOE_TOKEN": True})
    check = jobs.declared_preflight(install, "v0.1.0", {"years": 2019})
    assert check("backfill-entsoe") is None


def test_partial_credentials_name_only_what_is_missing(install, monkeypatch):
    """RTE needs two fields. Having the id and not the secret must say so."""
    import dandelion.jobs as jobs

    monkeypatch.setattr(jobs.credentials, "status_by_field",
                        lambda: {"RTE_CLIENT_ID": True})
    check = jobs.declared_preflight(install, "v0.1.0", {})
    reason = check("extract-rte")
    assert reason and "secret" in reason.lower()
    assert "client id" not in reason.lower()


# ------------------------------------------------------------- graceful degradation

def test_a_cluster_with_no_data_is_dropped_and_reported(install, monkeypatch, tmp_path):
    """IT_SOUTH cannot be modelled for 2019 — IT_CALA did not exist yet — but the other
    twelve zones can. The run proceeds against a generated overlay, and the dropped zone
    becomes a warning on the result rather than a silent 181 EUR/MWh."""
    import dandelion.jobs as jobs

    source = install.code_dir("v0.1.0") / "dispatch_model" / "config.yaml"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "run:\n  reports_dir: reports\nzones:\n  FR: {}\n  IT_SOUTH: {}\n"
        "borders:\n  - [FR, IT_SOUTH]\n", encoding="utf-8")

    monkeypatch.setattr(
        "drivers.preflight.check_cluster_zones",
        lambda db, year: type("P", (), {"ok": True, "detail": {"incomplete": {"IT_SOUTH": ["IT_CALA"]}}})())

    params = {"year": 2019}
    dropped = jobs.degrade_for_missing_data(install, "v0.1.0", "dispatch-backtest", params)
    assert dropped == ["IT_SOUTH"]
    assert params["config"] == str(install.run_configs_dir / "dispatch-2019.yaml")

    argv = jobs.render_argv(jobs.find_job("dispatch-backtest"), install, "v0.1.0", params)
    assert argv[-2:] == ["-c", params["config"]]
    assert argv.count("-c") == 2, "argparse keeps the last -c, so the default stays intact"


def test_a_complete_year_keeps_the_shipped_config(install, monkeypatch):
    import dandelion.jobs as jobs

    monkeypatch.setattr("drivers.preflight.check_cluster_zones",
                        lambda db, year: type("P", (), {"ok": True, "detail": {}})())
    params = {"year": 2024}
    assert jobs.degrade_for_missing_data(install, "v0.1.0", "dispatch-backtest", params) == []
    assert "config" not in params
    argv = jobs.render_argv(jobs.find_job("dispatch-backtest"), install, "v0.1.0", params)
    assert argv.count("-c") == 1
