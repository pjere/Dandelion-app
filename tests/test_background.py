"""Slow work off the interface thread.

The rule this file enforces is that the worker never touches a widget: it queues events, the
interface drains them. Cross-thread widget mutation is the classic source of GUI bugs that
only reproduce on someone else's machine.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion.background import BackgroundTask, Event, TaskView  # noqa: E402


def wait_for(task: BackgroundTask, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while task.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert task.finished, f"{task.name} did not finish within {timeout}s"


def test_a_result_comes_back():
    task = BackgroundTask("sum")
    task.start(lambda a, b: a + b, 2, 3)
    wait_for(task)
    assert task.succeeded and task.result == 5


def test_an_exception_is_reported_not_swallowed():
    task = BackgroundTask("boom")
    task.start(lambda: 1 / 0)
    wait_for(task)
    assert task.finished and not task.succeeded
    assert isinstance(task.error, ZeroDivisionError)
    assert "ZeroDivisionError" in task.error_traceback


def test_a_failure_emits_an_error_event():
    task = BackgroundTask("boom")
    task.start(lambda: (_ for _ in ()).throw(RuntimeError("no network")))
    wait_for(task)
    kinds = [e.kind for e in task.drain()]
    assert "error" in kinds and "done" not in kinds


def test_success_emits_done_exactly_once():
    task = BackgroundTask("ok")
    task.start(lambda: "fine")
    wait_for(task)
    assert [e.kind for e in task.drain()].count("done") == 1


def test_events_arrive_in_order():
    def work(t: BackgroundTask):
        for n in range(5):
            t.step(f"step {n}")

    task = BackgroundTask("ordered")
    task.start(work, task)
    wait_for(task)
    messages = [e.message for e in task.drain() if e.kind == "step"]
    assert messages == [f"step {n}" for n in range(5)]


def test_draining_never_blocks_when_empty():
    task = BackgroundTask("idle")
    assert list(task.drain()) == []


def test_a_task_cannot_be_started_twice():
    """Restarting would make running/result/error ambiguous; replace it instead."""
    task = BackgroundTask("once")
    task.start(lambda: None)
    wait_for(task)
    with pytest.raises(RuntimeError):
        task.start(lambda: None)


def test_cancellation_is_cooperative():
    def work(t: BackgroundTask):
        while not t.cancelled:
            time.sleep(0.01)
        return "stopped"

    task = BackgroundTask("cancellable")
    task.start(work, task)
    task.request_cancel()
    wait_for(task)
    assert task.result == "stopped"


# ------------------------------------------------------------------------- TaskView

def test_view_accumulates_steps():
    view = TaskView()
    view.apply(Event("step", "one", ok=True))
    view.apply(Event("step", "two", ok=True))
    assert [s.message for s in view.steps] == ["one", "two"]


def test_a_step_clears_a_stale_percentage():
    """A fraction from the previous step must not linger over the next one."""
    view = TaskView()
    view.apply(Event("progress", "downloading", fraction=0.5))
    view.apply(Event("step", "extracting"))
    assert view.fraction is None


def test_unknown_progress_stays_unknown():
    """Where no honest percentage exists, none is invented."""
    view = TaskView()
    view.apply(Event("progress", "installing", fraction=None))
    assert view.fraction is None and view.current == "installing"


def test_error_marks_the_view_done():
    view = TaskView()
    view.apply(Event("error", "no network"))
    assert view.done and view.error == "no network"


def test_done_clears_the_spinner_state():
    view = TaskView()
    view.apply(Event("progress", "working", fraction=0.3))
    view.apply(Event("done", "finished"))
    assert view.done and view.current == "" and view.fraction is None


def test_apply_all_reports_whether_anything_changed():
    task = BackgroundTask("quiet")
    view = TaskView()
    assert view.apply_all(task.drain()) is False
    task.step("something")
    assert view.apply_all(task.drain()) is True


def test_a_failed_step_is_recorded_as_failed():
    view = TaskView()
    view.apply(Event("step", "junction", ok=False))
    assert view.steps[0].ok is False
