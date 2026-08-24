"""Run slow work off the interface thread, and report it honestly.

Provisioning takes about eight minutes, a credential check up to two, and a Monte-Carlo run
several hours. None of that can happen on the thread drawing the window.

The pattern here is deliberately dull: the worker thread never touches a UI element. It puts
events on a queue, and the interface drains that queue on a timer. Cross-thread widget
mutation is the classic source of GUI bugs that only appear on someone else's machine, and
avoiding it entirely costs one queue.

Progress is reported only where it is genuinely known. A step that cannot say how far along
it is says so, rather than animating a bar that means nothing.
"""
from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    """Something the worker wants the interface to know."""

    kind: str                      # "step" | "progress" | "log" | "done" | "error"
    message: str = ""
    detail: str = ""
    ok: bool | None = None
    #: 0.0-1.0 where genuinely known, None where it is not.
    fraction: float | None = None
    payload: Any = None


class BackgroundTask:
    """One unit of slow work, with a queue of events for the interface to drain.

    Single-shot by design: a finished task is replaced, not restarted, which keeps
    `running`, `result` and `error` unambiguous at every moment.
    """

    def __init__(self, name: str):
        self.name = name
        self._events: queue.Queue[Event] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._result: Any = None
        self._error: BaseException | None = None
        self._traceback: str = ""
        self._cancelled = threading.Event()

    # ------------------------------------------------------------------ producing
    def emit(self, event: Event) -> None:
        self._events.put(event)

    def step(self, message: str, detail: str = "", ok: bool | None = None) -> None:
        self.emit(Event("step", message, detail, ok))

    def progress(self, message: str, fraction: float | None, detail: str = "") -> None:
        self.emit(Event("progress", message, detail, fraction=fraction))

    def start(self, fn: Callable[..., Any], *args, **kwargs) -> None:
        if self._thread is not None:
            raise RuntimeError(f"{self.name} has already been started")

        def wrapper() -> None:
            try:
                self._result = fn(*args, **kwargs)
            except BaseException as exc:                  # noqa: BLE001 - reported, not swallowed
                self._error = exc
                self._traceback = traceback.format_exc()
                self.emit(Event("error", str(exc) or exc.__class__.__name__, payload=exc))
            else:
                self.emit(Event("done", f"{self.name} finished", payload=self._result))

        self._thread = threading.Thread(target=wrapper, name=self.name, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ consuming
    def drain(self) -> Iterator[Event]:
        """Every event queued since the last call. Never blocks."""
        while True:
            try:
                yield self._events.get_nowait()
            except queue.Empty:
                return

    @property
    def started(self) -> bool:
        return self._thread is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def finished(self) -> bool:
        return self._thread is not None and not self._thread.is_alive()

    @property
    def succeeded(self) -> bool:
        return self.finished and self._error is None

    @property
    def result(self) -> Any:
        return self._result

    @property
    def error(self) -> BaseException | None:
        return self._error

    @property
    def error_traceback(self) -> str:
        return self._traceback

    # ------------------------------------------------------------------ cancelling
    def request_cancel(self) -> None:
        """Ask the work to stop. Cooperative: the worker must check `cancelled`.

        Python cannot safely kill a thread, so anything genuinely uninterruptible - a
        subprocess - is stopped by the job engine through a Windows Job Object instead.
        """
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()


@dataclass
class StepView:
    """One line in the interface's progress list."""

    message: str
    detail: str = ""
    ok: bool | None = None


@dataclass
class TaskView:
    """What the interface renders: an ordered list of steps plus the current activity."""

    steps: list[StepView] = field(default_factory=list)
    current: str = ""
    fraction: float | None = None
    error: str = ""
    done: bool = False

    def apply(self, event: Event) -> None:
        if event.kind == "step":
            self.steps.append(StepView(event.message, event.detail, event.ok))
            self.current = event.message
            self.fraction = None
        elif event.kind == "progress":
            self.current = event.message
            self.fraction = event.fraction
        elif event.kind == "log":
            self.current = event.message
        elif event.kind == "error":
            self.error = event.message
            self.done = True
        elif event.kind == "done":
            self.done = True
            self.current = ""
            self.fraction = None

    def apply_all(self, events: Iterator[Event]) -> bool:
        """Returns whether anything changed, so the interface can skip a redraw."""
        changed = False
        for event in events:
            self.apply(event)
            changed = True
        return changed
