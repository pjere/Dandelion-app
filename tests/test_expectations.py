"""Telling someone what to expect, without inventing it.

`ingest-remit` ran seventeen minutes and printed one line, at the end. Under a pipe that is
indistinguishable from a hang, and the reasonable response to a hang is to kill it — losing
someone else's API quota and leaving a half-filled table.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion.expectations import before, during, human_duration, roughly  # noqa: E402
from drivers.inventory import job as find_job  # noqa: E402


@pytest.mark.parametrize("seconds,text", [
    (0, "0s"), (45, "45s"), (60, "1m"), (125, "2m 05s"), (3600, "1h 00m"), (5400, "1h 30m"),
])
def test_durations_read_as_a_person_would_say_them(seconds, text):
    assert human_duration(seconds) == text


@pytest.mark.parametrize("seconds,text", [
    (30, "under a minute"), (240, "about 4 minutes"),
    (1025, "about 15 minutes"), (1700, "about 30 minutes"), (7200, "about 2 hours"),
])
def test_estimates_are_rounded_not_precise(seconds, text):
    """'about 17m 05s' claims a precision that a network-bound job does not have."""
    assert roughly(seconds) == text


# ------------------------------------------------------------------------ before

def test_a_silent_job_says_so_before_it_starts():
    text = before("ingest-remit")
    assert "prints nothing" in text and "15 minutes" in text


def test_a_talkative_job_just_gives_the_estimate():
    assert before("backfill-entsoe") == "about 30 minutes"


def test_a_job_with_no_measurement_says_nothing_rather_than_guessing():
    assert before("status") == ""


def test_the_estimate_scales_with_the_years_asked_for():
    one, four = before("backfill-entsoe", years=1), before("backfill-entsoe", years=4)
    assert one == "about 30 minutes" and "hours" in four


# ------------------------------------------------------------------------ during

def test_nothing_is_said_when_upstream_reports_real_progress():
    """A measured typical must never compete with a real ETA from the job itself."""
    assert during("dispatch-backtest", 100, has_real_progress=True) == ""


def test_a_running_job_is_placed_against_its_typical():
    assert during("backfill-entsoe", 300) == "running for 5m of about 30 minutes"


def test_a_silent_job_repeats_the_reassurance_while_it_runs():
    assert "prints nothing until it is done" in during("ingest-remit", 120)


def test_running_long_is_said_plainly_and_not_re_estimated():
    """Longer than usual is not necessarily wrong -- a wider range or a slow provider both
    do it -- and a fresh estimate here would be a second guess dressed as information."""
    text = during("ingest-remit", 1025 * 2)
    assert "longer than the usual" in text
    assert "Still working" in text


def test_an_unmeasured_job_reports_only_elapsed():
    assert during("status", 90) == "running for 1m 30s"


# ------------------------------------------------------------------------- honesty

def test_a_typical_is_never_turned_into_a_percentage():
    """A job at 90% of its usual duration is not 90% done, and a bar saying so becomes a
    lie the moment it runs long."""
    for elapsed in (10, 500, 1000, 5000):
        assert "%" not in during("ingest-remit", elapsed)


def test_every_recorded_duration_came_from_a_real_run():
    """These are measurements from this project's own runs on 2026-08-24/25, not guesses.
    If one is ever invented, the sentence built from it is false."""
    measured = {j.id: j.typical_seconds for j in
                [find_job(i) for i in ("backfill-entsoe", "extract-rte", "ingest-remit",
                                       "build-master", "reconcile-units")]}
    assert measured == {"backfill-entsoe": 1700, "extract-rte": 645,
                        "ingest-remit": 1025, "build-master": 100, "reconcile-units": 12}
