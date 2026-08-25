"""The journal, which is what lets the product answer "may this run?" across sessions.

Phase 3's history lived in memory and died with the window. The `requires` edge on a Job was
declared and enforced nowhere — the third instance of that pattern after `preflight` and
`needs_credentials` — because there was nothing durable to enforce it against.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion.jobs import JobResult  # noqa: E402
from dandelion.journal import Journal  # noqa: E402


def result(job_id="weathergen-fit", outcome="succeeded", **kwargs) -> JobResult:
    base = {"job_id": job_id, "argv": [], "cwd": ".", "log_path": "x.log",
            "started_at": "2026-08-25T09:00:00+00:00",
            "finished_at": "2026-08-25T09:05:00+00:00",
            "outcome": outcome, "duration_s": 300.0}
    return JobResult(**{**base, **kwargs})


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path / "journal.jsonl")


# ------------------------------------------------------------------ round trip

def test_a_run_survives_the_window_closing(journal):
    journal.record(result(), tag="v0.1.0")
    reopened = Journal(journal.path)
    assert [e.job_id for e in reopened.entries()] == ["weathergen-fit"]


def test_newest_first(journal):
    for i in range(3):
        journal.record(result(job_id=f"job-{i}"))
    assert [e.job_id for e in journal.entries()] == ["job-2", "job-1", "job-0"]


def test_the_file_is_readable_by_a_person(journal):
    journal.record(result(), tag="v0.1.0", params={"year": 2019})
    line = json.loads(journal.path.read_text(encoding="utf-8").splitlines()[0])
    assert line["job_id"] == "weathergen-fit" and line["params"] == {"year": 2019}


# ------------------------------------------------------------------ robustness

def test_a_truncated_last_line_costs_one_record_not_the_file(journal):
    """The expected failure mode after a hard kill mid-append."""
    journal.record(result(job_id="first"))
    journal.record(result(job_id="second"))
    with journal.path.open("a", encoding="utf-8") as fh:
        fh.write('{"job_id": "third", "outcome": "succ')
    assert [e.job_id for e in journal.entries()] == ["second", "first"]


def test_a_line_that_is_not_a_run_is_skipped(journal):
    journal.path.write_text('{"unrelated": 1}\n[]\nnot json at all\n', encoding="utf-8")
    assert journal.entries() == []


def test_an_unknown_field_from_a_future_version_does_not_break_reading(journal):
    journal.path.write_text(
        json.dumps({"job_id": "x", "outcome": "succeeded",
                    "started_at": "2026-08-25T09:00:00+00:00",
                    "something_new": 42}) + "\n", encoding="utf-8")
    assert [e.job_id for e in journal.entries()] == ["x"]


def test_a_missing_file_reads_as_empty_and_is_not_created(tmp_path):
    absent = tmp_path / "nothing.jsonl"
    assert Journal(absent).entries() == []
    assert not absent.exists()


# ------------------------------------------------------------------ secrets

def test_a_secret_in_a_parameter_never_reaches_the_file(journal, monkeypatch):
    """Params are recorded, and a job could in principle be handed one. The logs are
    scrubbed for this reason; the journal is no different."""
    monkeypatch.setattr("dandelion.credentials.scrub",
                        lambda text: text.replace("hunter2", "<redacted>"))
    journal.record(result(), params={"token": "hunter2", "nested": {"k": ["hunter2"]}})
    text = journal.path.read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert text.count("<redacted>") == 2


def test_a_secret_echoed_in_a_failure_reason_is_scrubbed(journal, monkeypatch):
    monkeypatch.setattr("dandelion.credentials.scrub",
                        lambda text: text.replace("hunter2", "<redacted>"))
    journal.record(result(outcome="failed", reason="rejected token hunter2"))
    assert "hunter2" not in journal.path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ prerequisites

def test_only_a_success_counts_as_having_run(journal):
    for outcome in ("failed", "cancelled", "refused"):
        journal.record(result(outcome=outcome))
    assert not journal.has_succeeded("weathergen-fit")
    journal.record(result(outcome="succeeded"))
    assert journal.has_succeeded("weathergen-fit")


def test_a_run_from_another_release_does_not_count(journal):
    """Output produced by a different build of the model is not evidence that this one has
    been run."""
    journal.record(result(), tag="v0.0.9")
    assert journal.has_succeeded("weathergen-fit")
    assert not journal.has_succeeded("weathergen-fit", tag="v0.1.0")


def test_missing_prerequisites_names_the_unmet_edge(journal):
    assert journal.missing_prerequisites("weathergen-simulate") == ["weathergen-fit"]
    journal.record(result(job_id="weathergen-fit"))
    assert journal.missing_prerequisites("weathergen-simulate") == []


def test_a_job_with_no_prerequisites_is_never_blocked(journal):
    assert journal.missing_prerequisites("status") == []


# ------------------------------------------------------------------ timestamps

def test_when_is_the_finish_because_that_is_when_the_output_became_real(journal):
    journal.record(result())
    assert journal.last("weathergen-fit").when == datetime(2026, 8, 25, 9, 5, tzinfo=UTC)


def test_a_naive_timestamp_is_read_as_utc_rather_than_local(journal):
    """Mixing a naive local time into UTC comparisons would make freshness wrong by the
    offset, silently, and only for users outside UTC."""
    journal.record(result(finished_at="2026-08-25T09:05:00"))
    assert journal.last("weathergen-fit").when == datetime(2026, 8, 25, 9, 5, tzinfo=UTC)


def test_an_unparseable_timestamp_is_none_rather_than_now(journal):
    journal.record(result(finished_at="whenever", started_at=""))
    assert journal.last("weathergen-fit").when is None


def test_the_most_recent_success_wins_over_a_later_failure(journal):
    """A failed re-run does not un-run the successful one before it — the output is still
    on disk."""
    journal.record(result(finished_at="2026-08-25T09:00:00+00:00"))
    journal.record(result(outcome="failed", finished_at="2026-08-25T10:00:00+00:00"))
    good = journal.last_success("weathergen-fit")
    assert good is not None and good.when.hour == 9
    assert journal.last("weathergen-fit").outcome == "failed"


def test_recency_is_measured_from_the_journal_not_the_clock(journal):
    now = datetime.now(UTC)
    journal.record(result(finished_at=(now - timedelta(days=40)).isoformat()))
    age = (now - journal.last_success("weathergen-fit").when).days
    assert age == 40
