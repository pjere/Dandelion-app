"""Which fitted models the data has moved on from.

A model file's own timestamp says when it was written, not whether it is still right. The
question a refits page exists to answer is different: **has anything it was fitted on
changed since?** Two timestamps answer it — when the fit last succeeded (`journal`) and when
its inputs last moved (`ingest_log`, or another job's success) — and neither is any use
alone.

Getting this wrong in the safe direction matters more than precision. Saying "stale" when
nothing changed costs a refit; saying "current" when the data moved leaves someone
projecting prices off a model that never saw the last two years of history. So an input we
cannot date at all reads as *unknown*, never as fresh.

The dependency map below is grounded in what each package actually reads, checked in the
release tree rather than inferred from names:

    weathergen          master_hourly's meteo columns, built from `synop`
    demand_model        `rte_consumption_short_term`, `rte_generation_per_type`
                        — its own config declares both (demand_model/config.yaml `data:`)
    res_model           master_hourly + the same two RTE series (grep: FROM ...)
    availability_model  `rte_generation_per_unit`, `rte_water_reserves`,
                        `dim_production_unit` (the last written by `reconcile-units`)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dandelion.journal import Journal


@dataclass(frozen=True)
class Inputs:
    """What a fitted model was built from."""

    #: `ingest_log.source` values. `rte_consumption_short_term` is logged as
    #: `rte:consumption_short_term`; SYNOP is logged bare.
    sources: tuple[str, ...] = ()
    #: Job ids whose success is itself an input — a merge or a reconciliation, which
    #: produce tables rather than ingesting them and so have no `ingest_log` row.
    jobs: tuple[str, ...] = ()


#: Fit job -> what it consumes. Only calibrations appear: a *projection* is stale when its
#: calibration is, and `requires` already carries that edge.
MODEL_INPUTS: dict[str, Inputs] = {
    "weathergen-fit": Inputs(sources=("synop",), jobs=("build-master",)),
    "demand-calibrate": Inputs(
        sources=("rte:consumption_short_term", "rte:generation_per_type"),
        jobs=("build-master",)),
    "res-calibrate": Inputs(
        sources=("rte:consumption_short_term", "rte:generation_per_type"),
        jobs=("build-master",)),
    "avail-calibrate": Inputs(
        sources=("rte:generation_per_unit", "rte:water_reserves"),
        jobs=("reconcile-units",)),
}


@dataclass
class Verdict:
    """Whether one fitted model is still current, and why."""

    job_id: str
    #: "shipped" | "never" | "current" | "stale" | "unknown"
    state: str
    fitted_at: datetime | None = None
    newest_input_at: datetime | None = None
    #: What moved after the fit, for a message that names the cause.
    moved: list[str] = field(default_factory=list)
    undatable: list[str] = field(default_factory=list)

    @property
    def needs_refit(self) -> bool:
        """A shipped fit is NOT a thing to refit by default.

        The release carries the author's own fitted models. Calling those "never fitted"
        and offering a refit would send users into multi-hour calibrations to reproduce
        something they already have — and, on a partial local history, probably worse.
        Whether their data differs enough to justify a refit is a judgement the product
        cannot make for them, so it states the situation and leaves the choice.
        """
        return self.state in ("never", "stale")

    @property
    def sentence(self) -> str:
        if self.state == "shipped":
            return "came with the model release, and has not been refitted here"
        if self.state == "never":
            return "has never been fitted here"
        if self.state == "stale":
            what = ", ".join(self.moved[:3])
            return f"was fitted before {what} last changed"
        if self.state == "unknown":
            return "cannot be checked - some of its inputs have no recorded date"
        return "is up to date with the data"


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).strip().replace(" ", "T")[:19])
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def source_times(database: Path) -> dict[str, datetime]:
    """When each ingested source last successfully changed. Read-only, and cheap."""
    database = Path(database)
    if not database.is_file():
        return {}
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True,
                                     timeout=2.0)
        connection.execute("PRAGMA query_only = 1")
        with connection:
            rows = connection.execute(
                "SELECT source, MAX(updated_at) FROM ingest_log "
                "WHERE status = 'ok' GROUP BY source").fetchall()
        connection.close()
    except sqlite3.DatabaseError:
        return {}
    out: dict[str, datetime] = {}
    for source, stamp in rows:
        moment = _parse(stamp)
        if moment:
            out[str(source)] = moment
    return out


def assess(job_id: str, journal: Journal, database: Path,
           tag: str | None = None, fits_shipped: bool = False) -> Verdict:
    """Is this fitted model still current with what it was fitted on?

    `fits_shipped` says the release brought its own fitted models (manifest.fits.source ==
    "downloaded"). Without it a fresh, perfectly working install reports four models as
    never fitted, which is true of this machine and misleading about the situation.
    """
    inputs = MODEL_INPUTS.get(job_id, Inputs())
    fit = journal.last_success(job_id, tag)
    if fit is None or fit.when is None:
        return Verdict(job_id, "shipped" if fits_shipped else "never")

    times = source_times(database)
    moved: list[str] = []
    undatable: list[str] = []
    newest: datetime | None = None

    for source in inputs.sources:
        when = times.get(source)
        if when is None:
            undatable.append(source)
            continue
        newest = when if newest is None else max(newest, when)
        if when > fit.when:
            moved.append(source)

    for dependency in inputs.jobs:
        entry = journal.last_success(dependency, tag)
        when = entry.when if entry else None
        if when is None:
            undatable.append(dependency)
            continue
        newest = when if newest is None else max(newest, when)
        if when > fit.when:
            moved.append(dependency)

    if moved:
        return Verdict(job_id, "stale", fit.when, newest, moved, undatable)
    # An input we cannot date is not evidence of freshness. Reporting "current" here would
    # be the one error that actually costs something: a projection built on a model that
    # never saw the last two years.
    if undatable:
        return Verdict(job_id, "unknown", fit.when, newest, moved, undatable)
    return Verdict(job_id, "current", fit.when, newest, moved, undatable)


def assess_all(journal: Journal, database: Path, tag: str | None = None,
               fits_shipped: bool = False) -> list[Verdict]:
    """Every fitted model, in the order they must be run."""
    return [assess(job_id, journal, database, tag, fits_shipped)
            for job_id in MODEL_INPUTS]
