"""How current the data is, answered without scanning 16.5 GB.

`pricemodeling status` counts every row of every table. `ingest_log` answers the same question
from a small table the ETL already maintains — measured at 183 sources in 0.02 s against the
owner's real database, which is what lets the dashboard redraw freely.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion import freshness  # noqa: E402
from dandelion.freshness import SourceRow, describe, group, human_rows, staleness  # noqa: E402

#: Shapes taken from the owner's real ingest_log.
SAMPLE = [
    ("entsoe:price:FR", "FR_2026-01-01", 70611, "ok", "2026-07-29T08:50:11"),
    ("entsoe:price:DE_LU", "DE_LU_2026-01-01", 70611, "ok", "2026-07-29T08:51:02"),
    ("entsoe:load:FR", "FR_2026-01-01", 90219, "ok", "2026-07-29T08:53:00"),
    ("rte:generation_per_unit", "2026-06-29_2026-06-30", 14414020, "ok", "2026-06-29T11:33:00"),
    ("elexon_price", "GB:2026-07-30", 65881, "ok", "2026-08-06T20:12:00"),
    ("synop", "202601", 33304416, "ok", "2026-06-29T13:51:00"),
    ("synop", "202602", 0, "missing", "2026-06-29T13:52:00"),
    ("something:new", "X_2026-01-01", 12, "ok", "2026-08-01T00:00:00"),
]


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "pricemodeling.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ingest_log (source TEXT, chunk_key TEXT, rows INTEGER,"
                " status TEXT, updated_at TEXT)")
    con.executemany("INSERT INTO ingest_log VALUES (?,?,?,?,?)", SAMPLE)
    con.commit()
    con.close()
    return path


# ------------------------------------------------------------------------ coverage

@pytest.mark.parametrize("key,expected", [
    ("FR_2026-01-01", "2026-01-01"),
    ("2026-05-31_2026-06-30", "2026-06-30"),          # a range: the END is the coverage
    ("GB:2026-07-30", "2026-07-30"),
    ("202601", "2026-01"),
    ("snapshot_2026-06-29", "2026-06-29"),
])
def test_coverage_is_read_from_the_chunk_key(key, expected):
    assert SourceRow("s", 1, 1, "", key).coverage_end == expected


def test_an_unrecognised_key_is_shown_as_is_rather_than_guessed():
    assert SourceRow("s", 1, 1, "", "weird-key").coverage_end == "weird-key"


# ------------------------------------------------------------------------ grouping

def test_sources_fold_into_families_a_person_recognises(database):
    picture = describe(database)
    labels = {f.label for f in picture.families}
    assert {"Prices", "Load", "French detail", "GB market", "Weather observations"} <= labels


def test_an_unknown_source_is_kept_rather_than_dropped():
    """A new upstream source must show up somewhere, not vanish from the dashboard."""
    families = group([SourceRow("brand:new:thing", 1, 5, "2026-08-01T00:00:00", "X_2026-01-01")])
    assert [f.label for f in families] == ["Other"]
    assert families[0].rows == 5


def test_family_totals_add_up(database):
    prices = next(f for f in describe(database).families if f.label == "Prices")
    assert prices.rows == 70611 * 2
    assert prices.provider == "ENTSO-E day-ahead"


def test_only_successful_chunks_count_as_rows(database):
    """A `missing` chunk is a gap, not data."""
    weather = next(f for f in describe(database).families if f.label == "Weather observations")
    assert weather.rows == 33304416
    assert weather.problems == 1


# ------------------------------------------------------------------------- picture

def test_a_missing_database_is_reported_not_created(tmp_path):
    absent = tmp_path / "nothing.db"
    picture = describe(absent)
    assert not picture.exists and not picture.readable
    assert not absent.exists(), "describing a database must never create one"


def test_an_empty_database_reads_as_empty_not_broken(tmp_path):
    path = tmp_path / "empty.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ingest_log (source TEXT, chunk_key TEXT, rows INTEGER,"
                " status TEXT, updated_at TEXT)")
    con.commit()
    con.close()
    picture = describe(path)
    assert picture.readable and picture.empty and picture.total_rows == 0


def test_a_file_that_is_not_a_database_is_reported(tmp_path):
    path = tmp_path / "not.db"
    path.write_text("definitely not sqlite", encoding="utf-8")
    picture = describe(path)
    assert picture.exists and not picture.readable and picture.error


def test_totals_are_reported(database):
    picture = describe(database)
    assert picture.total_sources == 7          # synop's missing chunk is not a source
    assert picture.total_rows > 33_000_000


# ----------------------------------------------------------------------- staleness

def test_recent_data_is_fresh():
    now = datetime(2026, 8, 24, tzinfo=UTC)
    phrase, severity = staleness((now - timedelta(hours=3)).isoformat(), now)
    assert severity == "fresh" and phrase == "today"


def test_a_few_weeks_old_is_still_fresh():
    """This model runs on years of history; three weeks old is entirely normal, and
    colouring it as a problem trains people to ignore the colour that matters."""
    now = datetime(2026, 8, 24, tzinfo=UTC)
    _, severity = staleness((now - timedelta(days=20)).isoformat(), now)
    assert severity == "fresh"


def test_months_old_is_ageing_and_very_old_is_stale():
    now = datetime(2026, 8, 24, tzinfo=UTC)
    assert staleness((now - timedelta(days=60)).isoformat(), now)[1] == "ageing"
    assert staleness((now - timedelta(days=400)).isoformat(), now)[1] == "stale"


def test_a_missing_timestamp_says_never_rather_than_guessing():
    assert staleness("")[1] == "unknown"
    assert staleness("not a date")[1] == "unknown"


def test_age_of_an_unparseable_timestamp_is_none():
    assert freshness.age_in_days("garbage") is None


# -------------------------------------------------------------------------- format

@pytest.mark.parametrize("count,text", [(0, "0"), (999, "999"), (1500, "2k"), (98_000_000, "98.0M")])
def test_row_counts_read_naturally(count, text):
    assert human_rows(count) == text
