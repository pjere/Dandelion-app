"""How current is the data, answered cheaply.

`python -m pricemodeling status` runs `SELECT COUNT(*)` over every table. On a 16.5 GB master
that is a full scan of each one, so it cannot sit behind a dashboard that refreshes — and its
output is prose that would have to be parsed anyway.

`ingest_log` answers the question properly. It is the idempotency table the ETL already
maintains: one row per source and chunk, with the row count, a status and the time it was
written. Measured against the owner's real database: **183 sources in 0.02 s**.

Everything here opens the database **read-only**. A job may be writing at the same moment, and
a dashboard has no business taking a write lock on 16.5 GB of someone's work.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

#: `source` values follow a `family:kind:zone` convention, except Elexon which uses
#: underscores and SYNOP which is bare. Verified against the owner's database.
FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("synop", "Weather observations", "Météo-France SYNOP"),
    ("rte:", "French detail", "RTE"),
    ("entsoe:price", "Prices", "ENTSO-E day-ahead"),
    ("entsoe:load", "Load", "ENTSO-E"),
    ("entsoe:gen", "Generation", "ENTSO-E"),
    ("entsoe:flow", "Cross-border flows", "ENTSO-E"),
    ("entsoe:ntc", "Interconnector capacity", "ENTSO-E"),
    ("entsoe:cap", "Installed capacity", "ENTSO-E"),
    ("entsoe:hydro", "Hydro reservoirs", "ENTSO-E"),
    ("entsoe:unavail", "Outages", "ENTSO-E REMIT"),
    ("elexon_", "GB market", "Elexon"),
    ("ecb", "Exchange rates", "ECB"),
)

#: Chunk keys embed the period they cover, in several shapes:
#:   FR_2026-01-01 · 2026-05-31_2026-06-30 · GB:2026-07-30 · 202601
_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_YEAR_MONTH = re.compile(r"^(\d{4})(\d{2})$")


@dataclass
class SourceRow:
    source: str
    chunks: int
    rows: int
    last_ingested: str
    newest_key: str
    problems: int = 0

    @property
    def coverage_end(self) -> str:
        """The end of the period this source has data for, from its newest chunk key."""
        dates = _ISO_DATE.findall(self.newest_key or "")
        if dates:
            return max(dates)
        month = _YEAR_MONTH.match((self.newest_key or "").strip())
        if month:
            return f"{month.group(1)}-{month.group(2)}"
        return self.newest_key or ""


@dataclass
class Family:
    label: str
    provider: str
    sources: list[SourceRow] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return sum(s.rows for s in self.sources)

    @property
    def last_ingested(self) -> str:
        return max((s.last_ingested for s in self.sources if s.last_ingested), default="")

    @property
    def coverage_end(self) -> str:
        return max((s.coverage_end for s in self.sources if s.coverage_end), default="")

    @property
    def problems(self) -> int:
        return sum(s.problems for s in self.sources)

    @property
    def present(self) -> bool:
        return bool(self.sources)


@dataclass
class DataPicture:
    """Everything the home page needs to say about the data, and nothing expensive."""

    database: str = ""
    exists: bool = False
    readable: bool = False
    error: str = ""
    families: list[Family] = field(default_factory=list)
    total_rows: int = 0
    total_sources: int = 0
    last_ingested: str = ""

    @property
    def empty(self) -> bool:
        return self.exists and self.readable and not self.total_rows


def _connect(path: Path) -> sqlite3.Connection:
    """Read-only, and never blocking on somebody else's write."""
    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.execute("PRAGMA query_only = 1")
    return connection


def read_sources(database: Path) -> list[SourceRow]:
    """One row per ingested source. Cheap: `ingest_log` is small and indexed by nothing bigger."""
    with _connect(database) as connection:
        rows = connection.execute(
            """
            SELECT source,
                   SUM(status = 'ok'),
                   COALESCE(SUM(CASE WHEN status = 'ok' THEN rows END), 0),
                   MAX(CASE WHEN status = 'ok' THEN updated_at END),
                   MAX(CASE WHEN status = 'ok' THEN chunk_key END),
                   SUM(status = 'missing')
            FROM ingest_log
            GROUP BY source
            ORDER BY source
            """
        ).fetchall()
    return [
        SourceRow(source=source, chunks=chunks or 0, rows=count or 0,
                  last_ingested=(last or "")[:19], newest_key=key or "",
                  problems=problems or 0)
        for source, chunks, count, last, key, problems in rows
        if chunks
    ]


def group(sources: list[SourceRow]) -> list[Family]:
    """Fold 183 source keys into the dozen groups a person actually thinks in."""
    families = [Family(label=label, provider=provider) for _, label, provider in FAMILIES]
    index = {prefix: family for (prefix, _, _), family in zip(FAMILIES, families, strict=True)}
    other = Family(label="Other", provider="")

    for row in sources:
        for prefix, family in index.items():
            if row.source.startswith(prefix):
                family.sources.append(row)
                break
        else:
            other.sources.append(row)

    result = [f for f in families if f.present]
    if other.present:
        result.append(other)
    return result


def describe(database: Path) -> DataPicture:
    """The whole picture, safe to call on every dashboard refresh."""
    picture = DataPicture(database=str(database), exists=database.is_file())
    if not picture.exists:
        return picture
    try:
        sources = read_sources(database)
    except sqlite3.DatabaseError as exc:
        picture.error = f"The database could not be read: {exc}"
        return picture
    except OSError as exc:
        picture.error = str(exc)
        return picture

    picture.readable = True
    picture.families = group(sources)
    picture.total_rows = sum(s.rows for s in sources)
    picture.total_sources = len(sources)
    picture.last_ingested = max((s.last_ingested for s in sources if s.last_ingested),
                                default="")
    return picture


def age_in_days(timestamp: str, now: datetime | None = None) -> float | None:
    """How long ago, in days. None when the timestamp is unparseable rather than guessing."""
    if not timestamp:
        return None
    text = timestamp.strip().replace(" ", "T")[:19]
    try:
        moment = datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError:
        return None
    return (( now or datetime.now(UTC)) - moment).total_seconds() / 86400


def staleness(timestamp: str, now: datetime | None = None) -> tuple[str, str]:
    """A phrase and a severity for the interface.

    Thresholds are deliberately loose. This model is built on years of history; data a week
    old is entirely normal and colouring it as a problem would train people to ignore the
    colour that matters.
    """
    days = age_in_days(timestamp, now)
    if days is None:
        return ("never", "unknown")
    if days < 1:
        return ("today", "fresh")
    if days < 2:
        return ("yesterday", "fresh")
    if days < 31:
        return (f"{int(days)} days ago", "fresh")
    if days < 120:
        return (f"{int(days / 30)} months ago", "ageing")
    return (f"{int(days / 30)} months ago", "stale")


def human_rows(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)
