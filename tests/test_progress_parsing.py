"""Pin our progress parser to what upstream actually prints.

The GUI's honest progress depends entirely on parsing powersim_core.progress output.
Upstream will keep evolving, so this test does two things: it checks our regexes against
captured real lines (always), and — when an upstream release happens to be installed —
it regenerates lines from the live module and checks those too. A format change then
fails here instead of silently degrading the GUI to a spinner.

Captured from powersim_core.progress @ 09a2459 under a non-TTY stream.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.inventory import PROGRESS_COUNTER_RE, PROGRESS_RE  # noqa: E402

CAPTURED_BAR = [
    "[projection 2027-2046] 0/3   0%  elapsed 0:00:00  eta --:--:--  ?",
    "[projection 2027-2046] 1/3  33%  elapsed 0:00:00  eta 0:00:00  ?  2027 done",
    "[projection 2027-2046] 3/3 100%  elapsed 0:00:00  eta 0:00:00  ?  2029 done",
    "[monte-carlo 50 draws] 7/50  14%  elapsed 1:02:03  eta 8:15:00  8.9 min/it",
    "[2031 windows] 12/52  23%  elapsed 0:04:10  eta 0:14:35  20.8 s/it  window 12",
]


@pytest.mark.parametrize("line", CAPTURED_BAR)
def test_bar_lines_parse(line):
    m = PROGRESS_RE.match(line)
    assert m is not None, f"did not parse: {line!r}"
    assert int(m["done"]) <= int(m["total"])


def test_fields_are_extracted_exactly():
    m = PROGRESS_RE.match(
        "[2031 windows] 12/52  23%  elapsed 0:04:10  eta 0:14:35  20.8 s/it  window 12"
    )
    assert m["label"] == "2031 windows"
    assert (m["done"], m["total"], m["pct"]) == ("12", "52", "23")
    assert m["elapsed"] == "0:04:10"
    assert m["eta"] == "0:14:35"
    assert m["rate"] == "20.8 s/it"
    assert m["note"] == "window 12"


def test_note_is_not_swallowed_by_an_unknown_rate():
    """The rate is '?' until the first item completes; the note must survive that."""
    m = PROGRESS_RE.match("[deliverables] 1/20   5%  elapsed 0:00:01  eta 0:00:19  ?  2027 done")
    assert m["rate"] == "?"
    assert m["note"] == "2027 done"


def test_unknown_eta_placeholder():
    m = PROGRESS_RE.match("[weathergen fit] 0/6   0%  elapsed 0:00:00  eta --:--:--  ?")
    assert m is not None and m["eta"] == "--:--:--" and m["note"] is None


def test_counter_branch():
    """`total <= 0` degrades to a plain counter: no bar, no ETA."""
    m = PROGRESS_COUNTER_RE.match("[ingest] 42 done  elapsed 0:01:00  1.4 s/it")
    assert m is not None
    assert m["label"] == "ingest" and m["done"] == "42" and m["rate"] == "1.4 s/it"


def test_ordinary_output_is_not_mistaken_for_progress():
    for line in (
        "Base : C:/data/pricemodeling.db",
        "[ERREUR] ENTSO-E: HTTP 401",
        "[thermique] generation_per_unit: 12345 lignes -> rte_generation",
        "  master_hourly                                 105,192 lignes",
    ):
        assert PROGRESS_RE.match(line) is None
        assert PROGRESS_COUNTER_RE.match(line) is None


# --------------------------------------------------------------------------------------
# Live check — only when an upstream release is importable in the running interpreter.
# --------------------------------------------------------------------------------------

progress_mod = pytest.importorskip(
    "powersim_core.progress", reason="no upstream release installed in this interpreter"
)


class _Piped:
    """Stands in for a subprocess pipe: writable, flushable, and not a TTY."""

    def __init__(self):
        self.lines: list[str] = []
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self.lines.append(line)

    def flush(self):
        pass

    @staticmethod
    def isatty():
        return False


def test_live_bar_output_matches_our_regex():
    out = _Piped()
    with progress_mod.Progress(3, "projection 2027-2046", stream=out, min_interval=0) as p:
        for i in range(3):
            p.update(note=f"{2027 + i} done")
    assert out.lines, "upstream emitted nothing under a pipe"
    for line in out.lines:
        assert PROGRESS_RE.match(line), f"live line no longer parses: {line!r}"


def test_live_counter_output_matches_our_regex():
    out = _Piped()
    with progress_mod.Progress(0, "ingest", stream=out, min_interval=0) as p:
        for _ in range(3):
            p.update()
    assert out.lines
    for line in out.lines:
        assert PROGRESS_COUNTER_RE.match(line), f"live counter line no longer parses: {line!r}"


def test_live_output_carries_no_carriage_returns_under_a_pipe():
    """A \\r bar would make one unreadable mega-line in the persisted job log."""
    out = _Piped()
    with progress_mod.Progress(2, "backtest 2019", stream=out, min_interval=0) as p:
        p.update()
        p.update()
    assert not any("\r" in line for line in out.lines)
