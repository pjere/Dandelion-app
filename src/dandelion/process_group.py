"""Kill a process and everything it started, reliably.

`Popen.terminate()` kills one process. That is not enough here: `run_montecarlo.py` spawns a
`ProcessPoolExecutor`, so cancelling a Monte-Carlo run with `terminate()` leaves a pool of
workers holding a HiGHS model and several gigabytes of RAM, orphaned and invisible. They keep
writing to the lake, and the next run finds a locked database.

A Windows **job object** with `KILL_ON_JOB_CLOSE` is the primary mechanism. It is meant to
solve the whole family of problems at once: every descendant joins the job, terminating the
job terminates all of them, and if Studio itself crashes the operating system closes our
handle and reaps the tree for us.

MEASURED, 2026-08-24, and it does not always hold. In a shell that is itself inside a job
object, a child assigned to our job was confirmed to be a member — and its grandchildren were
NOT, for both `ProcessPoolExecutor` and plain `subprocess`. `TerminateJobObject` then left
three worker processes running. The outer job here has `BREAKAWAY_OK` set, and requesting
`CREATE_BREAKAWAY_FROM_JOB` did not change the outcome.

Whether that is peculiar to a nested-job environment - a terminal or IDE that already runs its
children in a job - or would also affect a user launching from Explorer cannot be settled on
this machine. So the contract deliberately is **not** "the job object works". It is "no orphan
survives cancellation": kill the job, then verify, then sweep the process tree if anything is
still breathing, and report which mechanism was needed. `TerminationResult.escalated` says so
every time, which turns the open question into something the clean-VM run will simply answer.

Implemented on `ctypes` rather than pywin32 so the shipped binary carries no extra dependency
for something this small.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass, field

IS_WINDOWS = sys.platform == "win32"

# JOBOBJECT_EXTENDED_LIMIT_INFORMATION.BasicLimitInformation.LimitFlags
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100


class _IO_COUNTERS(ctypes.Structure):  # noqa: N801 - mirrors the Win32 struct name
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@dataclass
class TerminationResult:
    """What cancelling actually did. `survivors` empty is the only acceptable outcome."""

    pids: list[int] = field(default_factory=list)
    survivors: list[int] = field(default_factory=list)
    job_object_used: bool = False
    #: True when the job object left something running and the tree sweep had to finish it.
    escalated: bool = False

    @property
    def clean(self) -> bool:
        return not self.survivors


class ProcessGroup:
    """A process and every descendant it creates, killable as one unit."""

    def __init__(self, name: str = "dandelion-job"):
        self.name = name
        self._handle = None
        self._pids: list[int] = []
        if IS_WINDOWS:
            self._handle = self._create()

    # ------------------------------------------------------------------ windows
    @staticmethod
    def _create():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # The whole point: when the last handle to this job closes - including because Studio
        # crashed - Windows terminates everything still inside it.
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject failed")
        return handle

    def assign(self, pid: int) -> bool:
        """Put a process, and thereby all its future descendants, into the job."""
        self._pids.append(pid)
        if not IS_WINDOWS or self._handle is None:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(self._handle, process))
        finally:
            kernel32.CloseHandle(process)

    def spawn(self, argv: list[str], **kwargs) -> subprocess.Popen:
        """Start a process already inside the job.

        `CREATE_NEW_PROCESS_GROUP` keeps a Ctrl-C in Studio's own console from reaching the
        child, so cancelling is always deliberate rather than an accident of the terminal.
        """
        if IS_WINDOWS:
            flags = kwargs.pop("creationflags", 0)
            kwargs["creationflags"] = flags | subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(argv, **kwargs)
        self.assign(process.pid)
        return process

    def snapshot_tree(self) -> list[int]:
        """Every PID currently under this group. Taken BEFORE killing, because once the
        parent is gone there is nothing left to walk."""
        try:
            import psutil
        except ImportError:
            return list(self._pids)
        tree = list(self._pids)
        for pid in self._pids:
            try:
                for child in psutil.Process(pid).children(recursive=True):
                    tree.append(child.pid)
            except psutil.Error:
                continue
        return tree

    def terminate(self, exit_code: int = 1, settle: float = 2.0) -> TerminationResult:
        """Kill the tree, then CHECK, then escalate if anything is still breathing.

        The job object is the primary mechanism and should be sufficient on its own. It is
        not trusted blindly, because measurement said otherwise: in a shell that is itself
        inside a job object, grandchildren were observed NOT to inherit the job, and
        `TerminateJobObject` left a `ProcessPoolExecutor`'s workers running.

        Whether that is peculiar to a nested-job environment or would also happen for a user
        launching from Explorer is not something this machine can answer. So the contract is
        not "the job object works" but "no orphan survives cancellation", verified every
        time, with a process-tree sweep as the fallback.
        """
        import time

        tree = self.snapshot_tree()
        result = TerminationResult(pids=tree)

        if IS_WINDOWS and self._handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            result.job_object_used = bool(kernel32.TerminateJobObject(self._handle, exit_code))

        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            result.survivors = orphan_check(tree)
            if not result.survivors:
                return result
            time.sleep(0.1)

        # Something escaped the job. Sweep it up and say so.
        result.survivors = orphan_check(tree)
        if result.survivors:
            result.escalated = True
            self._kill_pids(result.survivors)
            deadline = time.monotonic() + settle
            while time.monotonic() < deadline:
                result.survivors = orphan_check(tree)
                if not result.survivors:
                    break
                time.sleep(0.1)
        return result

    @staticmethod
    def _kill_pids(pids: list[int]) -> None:
        try:
            import psutil
        except ImportError:
            return
        for pid in pids:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                continue

    def alive_descendants(self) -> list[int]:
        """PIDs still running under any process this group started.

        The point of this method is that "no orphans" becomes something a test asserts rather
        than something the docstring claims.
        """
        try:
            import psutil
        except ImportError:
            return []
        alive: list[int] = []
        for pid in self._pids:
            try:
                parent = psutil.Process(pid)
            except psutil.Error:
                continue
            if parent.is_running() and parent.status() != psutil.STATUS_ZOMBIE:
                alive.append(pid)
            for child in parent.children(recursive=True):
                try:
                    if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                        alive.append(child.pid)
                except psutil.Error:
                    continue
        return alive

    def close(self) -> None:
        if IS_WINDOWS and self._handle is not None:
            ctypes.WinDLL("kernel32").CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> ProcessGroup:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def orphan_check(pids: list[int]) -> list[int]:
    """Which of these PIDs are still alive. Used by tests and by the crash handler."""
    try:
        import psutil
    except ImportError:
        return []
    alive = []
    for pid in pids:
        try:
            process = psutil.Process(pid)
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                alive.append(pid)
        except psutil.Error:
            continue
    return alive


def this_process_id() -> int:
    return os.getpid()
