"""Cancelling a run must leave nothing behind.

A Monte-Carlo run spawns a ProcessPoolExecutor whose workers each hold a HiGHS model and
gigabytes of RAM. `Popen.terminate()` kills only the parent, so those workers would survive
cancellation, keep writing to the lake, and leave the next run facing a locked database.

The contract asserted here is not "the job object works" — measurement showed it does not
always contain grandchildren — but "no orphan survives", whichever mechanism achieves it.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion.process_group import (  # noqa: E402
    ProcessGroup,
    TerminationResult,
    orphan_check,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows")

POOL_PARENT = textwrap.dedent("""
    import time
    from concurrent.futures import ProcessPoolExecutor

    def work(n):
        time.sleep(600)

    if __name__ == "__main__":
        with ProcessPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(work, i) for i in range(2)]
            print("UP", flush=True)
            for f in futures:
                f.result()
""")


@pytest.fixture
def pool_script(tmp_path: Path) -> Path:
    script = tmp_path / "pool_parent.py"
    script.write_text(POOL_PARENT, encoding="utf-8")
    return script


def start_pool(group: ProcessGroup, script: Path) -> subprocess.Popen:
    process = group.spawn([sys.executable, str(script)], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    process.stdout.readline()                            # blocks until the pool prints UP
    time.sleep(1.5)                                      # let the workers finish starting
    return process


# --------------------------------------------------------------------- the contract

@windows_only
def test_cancelling_a_worker_pool_leaves_no_orphans(pool_script):
    """The Monte-Carlo cancellation case, in miniature."""
    group = ProcessGroup("test-pool")
    try:
        start_pool(group, pool_script)
        assert len(group.snapshot_tree()) > 1, "the pool never started"
        result = group.terminate()
        assert result.clean, f"orphans survived cancellation: {result.survivors}"
    finally:
        group.close()


@windows_only
def test_termination_reports_which_mechanism_was_needed(pool_script):
    """`escalated` is how the clean-VM run answers a question this machine cannot."""
    group = ProcessGroup("test-report")
    try:
        start_pool(group, pool_script)
        result = group.terminate()
        assert result.job_object_used is True
        assert isinstance(result.escalated, bool)
        assert result.pids, "the tree snapshot was empty"
    finally:
        group.close()


@windows_only
def test_a_lone_process_is_killed():
    group = ProcessGroup("test-single")
    try:
        process = group.spawn([sys.executable, "-c", "import time; time.sleep(600)"])
        result = group.terminate()
        assert result.clean
        assert not orphan_check([process.pid])
    finally:
        group.close()


@windows_only
def test_the_tree_is_snapshotted_before_the_kill(pool_script):
    """Once the parent dies there is nothing left to walk, so the snapshot must come first."""
    group = ProcessGroup("test-snapshot")
    try:
        start_pool(group, pool_script)
        before = group.snapshot_tree()
        result = group.terminate()
        assert set(before) <= set(result.pids)
    finally:
        group.close()


# ------------------------------------------------------------------------- details

def test_a_clean_result_has_no_survivors():
    assert TerminationResult(pids=[1, 2]).clean
    assert not TerminationResult(pids=[1], survivors=[1]).clean


def test_orphan_check_ignores_pids_that_are_gone():
    assert orphan_check([999_999_999]) == []


@windows_only
def test_terminating_an_empty_group_is_harmless():
    group = ProcessGroup("test-empty")
    try:
        assert group.terminate().clean
    finally:
        group.close()


@windows_only
def test_the_group_can_be_used_as_a_context_manager():
    with ProcessGroup("test-ctx") as group:
        assert group.assign(0) is False                  # a pid that cannot be opened
