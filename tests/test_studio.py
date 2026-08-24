"""Studio's decision logic, which is testable even though its rendering is not.

The Phase 3 report already records that opening the page catches defects unit tests cannot
(a stale timer repainting detached containers). The converse is also true: how a result is
ROUTED — a refusal to its own card, a finish to the history list — is ordinary logic, and
it decides whether a user sees the one message that names their fix.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion import studio  # noqa: E402
from dandelion.jobs import JobResult  # noqa: E402
from dandelion.paths import Install  # noqa: E402


class FakeEngine:
    """Stands in for JobEngine: records the call and returns a prepared result."""

    def __init__(self, result: JobResult):
        self.result = result
        self.calls: list[tuple[str, dict]] = []
        self.tail: list[str] = []
        self.progress = None

    def run(self, job_id, params=None, **kwargs):
        self.calls.append((job_id, dict(params or {})))
        return self.result


def make_result(**kwargs) -> JobResult:
    base = {"job_id": "dispatch-backtest", "argv": [], "cwd": ".", "log_path": "x.log",
            "started_at": "2026-08-24T09:00:00+00:00"}
    return JobResult(**{**base, **kwargs})


@pytest.fixture
def desk(tmp_path, monkeypatch) -> studio.Studio:
    monkeypatch.setattr(studio.Studio, "__init__", lambda self, *a, **k: None)
    desk = studio.Studio.__new__(studio.Studio)
    desk.install = Install(app_root=tmp_path / "app", data_root=tmp_path / "data")
    desk.tag = "v0.1.0"
    desk.task = None
    desk.history = []
    desk.refusal = None
    desk.backtest_year = 2019
    desk.view = studio.TaskView()
    desk._repaint_activity = lambda: None
    return desk


def drain(desk) -> None:
    for _ in range(200):
        if desk.task is not None and not desk.task.running:
            return
        time.sleep(0.01)
    raise AssertionError("the background task never finished")


# ------------------------------------------------------------------ routing

def test_a_refusal_goes_to_its_own_card_not_the_history_list(desk):
    """The one outcome that names its own fix must not be buried among finished runs."""
    desk.engine = FakeEngine(make_result(
        outcome="refused", reason="Power-station outage notifications have not been "
                                  "downloaded."))
    desk._start("dispatch-backtest", year=2019)
    drain(desk)
    assert desk.refusal is not None
    job_id, reason = desk.refusal
    assert job_id == "dispatch-backtest" and "outage" in reason
    assert desk.history == [], "a refusal is not a run that happened"


def test_a_finished_run_goes_to_history_and_clears_a_previous_refusal(desk):
    desk.refusal = ("dispatch-backtest", "an older complaint")
    desk.engine = FakeEngine(make_result(outcome="succeeded", duration_s=735))
    desk._start("dispatch-backtest", year=2019)
    drain(desk)
    assert desk.refusal is None, "the fix worked; stop showing the complaint"
    assert len(desk.history) == 1 and "finished in 12m 15s" in desk.history[0]


def test_a_failure_keeps_its_reason_in_the_history_entry(desk):
    desk.engine = FakeEngine(make_result(
        outcome="failed", duration_s=113, reason="Exited with code 1."))
    desk._start("dispatch-backtest", year=2019)
    drain(desk)
    assert "failed after 1m 53s" in desk.history[0]
    assert "Exited with code 1." in desk.history[0]


# ------------------------------------------------------------------ the button

def test_the_backtest_button_passes_the_selected_year(desk):
    desk.backtest_year = 2023
    desk.engine = FakeEngine(make_result(outcome="succeeded", duration_s=1))
    desk._start("dispatch-backtest", year=int(desk.backtest_year))
    drain(desk)
    assert desk.engine.calls == [("dispatch-backtest", {"year": 2023})]


def test_the_offered_years_are_ones_upstream_calibrated_against():
    """scripts/backfill_entsoe.py:25 — 2019 normal, 2022 crisis, 2023-24 high RES. A
    free-text field would invite a year with no data behind it."""
    assert studio.BACKTEST_YEARS == [2019, 2022, 2023, 2024]


# ------------------------------------------------------------------ disk

def test_directory_size_does_not_follow_a_junction_into_another_store(tmp_path):
    """Data root and code tree are bridged by junctions; counting through one would report
    the same bytes twice and tell the user they are twice as short of space as they are."""
    real = tmp_path / "store"
    real.mkdir()
    (real / "big.bin").write_bytes(b"x" * 5000)
    here = tmp_path / "code"
    here.mkdir()
    (here / "own.txt").write_bytes(b"y" * 100)

    if os.name == "nt":
        import subprocess
        subprocess.run(["cmd", "/c", "mklink", "/J", str(here / "linked"), str(real)],
                       capture_output=True, check=False)
        if not (here / "linked").exists():
            pytest.skip("could not create a junction here")
    else:
        pytest.skip("junction behaviour is the Windows case")

    assert studio.directory_size(here) == 100


def test_a_missing_directory_is_zero_not_an_error(tmp_path):
    assert studio.directory_size(tmp_path / "nope") == 0


@pytest.mark.parametrize("n,text", [(0, "0.0 B"), (1536, "1.5 KB"),
                                    (5 * 1024**3, "5.0 GB")])
def test_sizes_read_naturally(n, text):
    assert studio.human_bytes(n) == text
