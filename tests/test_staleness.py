"""Whether a fitted model has been overtaken by its own inputs.

The defining test is `test_an_undatable_input_is_never_reported_as_current`. Saying "stale"
when nothing changed costs a refit. Saying "current" when the data moved leaves someone
projecting prices off a model that never saw the last two years of history, with nothing on
screen to say so — so uncertainty resolves toward the cheap error, not the expensive one.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion.jobs import JobResult  # noqa: E402
from dandelion.journal import Journal  # noqa: E402
from dandelion.staleness import MODEL_INPUTS, assess, assess_all, source_times  # noqa: E402

JAN = datetime(2026, 1, 1, tzinfo=UTC)


def at(days: int) -> str:
    return (JAN + timedelta(days=days)).isoformat()


def succeeded(job_id: str, day: int) -> JobResult:
    return JobResult(job_id=job_id, argv=[], cwd=".", log_path="x.log",
                     started_at=at(day), finished_at=at(day), outcome="succeeded")


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path / "journal.jsonl")


@pytest.fixture
def database(tmp_path) -> Path:
    path = tmp_path / "pricemodeling.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ingest_log (source TEXT, chunk_key TEXT, rows INTEGER,"
                " status TEXT, updated_at TEXT)")
    con.commit()
    con.close()
    return path


def ingest(database: Path, source: str, day: int, status: str = "ok") -> None:
    con = sqlite3.connect(database)
    con.execute("INSERT INTO ingest_log VALUES (?,?,?,?,?)",
                (source, "k", 1, status, at(day)))
    con.commit()
    con.close()


def full_inputs(journal, database, fit_day, source_day, job_day, job_id="demand-calibrate"):
    for source in MODEL_INPUTS[job_id].sources:
        ingest(database, source, source_day)
    for dependency in MODEL_INPUTS[job_id].jobs:
        journal.record(succeeded(dependency, job_day), tag="v0.1.0")
    journal.record(succeeded(job_id, fit_day), tag="v0.1.0")


# ------------------------------------------------------------------ the four states

def test_a_model_never_fitted_says_so(journal, database):
    verdict = assess("demand-calibrate", journal, database, "v0.1.0")
    assert verdict.state == "never" and verdict.needs_refit
    assert "never been fitted" in verdict.sentence


def test_a_fit_after_its_inputs_is_current(journal, database):
    full_inputs(journal, database, fit_day=10, source_day=5, job_day=5)
    verdict = assess("demand-calibrate", journal, database, "v0.1.0")
    assert verdict.state == "current" and not verdict.needs_refit


def test_a_fit_before_its_inputs_is_stale_and_names_what_moved(journal, database):
    full_inputs(journal, database, fit_day=5, source_day=10, job_day=5)
    verdict = assess("demand-calibrate", journal, database, "v0.1.0")
    assert verdict.state == "stale" and verdict.needs_refit
    assert "rte:consumption_short_term" in verdict.moved
    assert "fitted before" in verdict.sentence


def test_an_undatable_input_is_never_reported_as_current(journal, database):
    """THE test. build-master has no ingest_log row — it merges rather than ingests — so if
    it has never been journalled there is no date for it. That is not evidence of
    freshness."""
    for source in MODEL_INPUTS["demand-calibrate"].sources:
        ingest(database, source, 5)
    journal.record(succeeded("demand-calibrate", 10), tag="v0.1.0")
    verdict = assess("demand-calibrate", journal, database, "v0.1.0")
    assert verdict.state == "unknown"
    assert verdict.undatable == ["build-master"]
    assert "cannot be checked" in verdict.sentence


def test_something_that_moved_outranks_something_undatable(journal, database):
    """A known change is actionable; an unknown one is not. Report the actionable one."""
    ingest(database, "rte:consumption_short_term", 20)
    journal.record(succeeded("demand-calibrate", 10), tag="v0.1.0")
    verdict = assess("demand-calibrate", journal, database, "v0.1.0")
    assert verdict.state == "stale"


# ------------------------------------------------------------------ edges

def test_a_failed_ingest_does_not_count_as_a_change(journal, database):
    """A chunk that errored changed nothing, so it cannot make a model stale."""
    full_inputs(journal, database, fit_day=10, source_day=5, job_day=5)
    ingest(database, "rte:consumption_short_term", 20, status="missing")
    assert assess("demand-calibrate", journal, database, "v0.1.0").state == "current"


def test_a_fit_from_another_release_does_not_count(journal, database):
    full_inputs(journal, database, fit_day=10, source_day=5, job_day=5)
    assert assess("demand-calibrate", journal, database, "v0.0.9").state == "never"


def test_a_refit_at_the_same_moment_as_an_ingest_is_current(journal, database):
    """Strictly-after, not at-or-after: a refit triggered by an ingest can land on the same
    second, and calling that stale would loop the user forever."""
    full_inputs(journal, database, fit_day=10, source_day=10, job_day=10)
    assert assess("demand-calibrate", journal, database, "v0.1.0").state == "current"


def test_a_missing_database_leaves_every_source_undatable(journal, tmp_path):
    journal.record(succeeded("demand-calibrate", 10), tag="v0.1.0")
    verdict = assess("demand-calibrate", journal, tmp_path / "absent.db", "v0.1.0")
    assert verdict.state == "unknown"
    assert set(verdict.undatable) >= set(MODEL_INPUTS["demand-calibrate"].sources)


def test_source_times_ignores_an_unreadable_database(tmp_path):
    path = tmp_path / "not.db"
    path.write_text("definitely not sqlite", encoding="utf-8")
    assert source_times(path) == {}


# ------------------------------------------------------------------ the whole set

def test_every_calibration_is_assessed(journal, database):
    verdicts = assess_all(journal, database, "v0.1.0")
    assert [v.job_id for v in verdicts] == list(MODEL_INPUTS)
    assert all(v.state == "never" for v in verdicts)


def test_only_calibrations_are_listed():
    """A projection is stale when its calibration is, and `requires` already carries that
    edge — duplicating it here would give two answers to one question."""
    assert all(job_id.endswith(("-calibrate", "-fit")) for job_id in MODEL_INPUTS)


def test_every_declared_input_job_is_a_real_job():
    from drivers.inventory import job as find_job

    for inputs in MODEL_INPUTS.values():
        for dependency in inputs.jobs:
            find_job(dependency)


# ------------------------------------------------------------------ shipped fits

def test_a_shipped_fit_is_not_reported_as_never_fitted(journal, database):
    """The release carries the author's own fitted models — 'Ship the model fits too,
    they're my own work.' Reporting four models as never fitted on a perfectly working
    fresh install would push users into multi-hour calibrations to reproduce something they
    already have, and on a partial local history probably reproduce it worse."""
    verdict = assess("demand-calibrate", journal, database, "v0.1.0", fits_shipped=True)
    assert verdict.state == "shipped"
    assert not verdict.needs_refit
    assert "came with the model release" in verdict.sentence


def test_without_shipped_fits_it_still_reads_as_never(journal, database):
    assert assess("demand-calibrate", journal, database, "v0.1.0").state == "never"


def test_a_local_refit_supersedes_the_shipped_one(journal, database):
    full_inputs(journal, database, fit_day=10, source_day=5, job_day=5)
    verdict = assess("demand-calibrate", journal, database, "v0.1.0", fits_shipped=True)
    assert verdict.state == "current", "a real local fit is dated and beats 'shipped'"


def test_assess_all_passes_the_flag_through(journal, database):
    verdicts = assess_all(journal, database, "v0.1.0", fits_shipped=True)
    assert {v.state for v in verdicts} == {"shipped"}
    assert not any(v.needs_refit for v in verdicts)
