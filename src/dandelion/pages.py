"""The Data and Models pages.

Both answer one question each, and neither offers to do everything at once.

**Data** — is what I have current, and what would I run to change that? The jobs are grouped
by the account they need rather than by the upstream package that implements them, because
"I have an RTE login and not an ENTSO-E one" is the shape a user's situation actually takes.

**Models** — has the data moved on from what my models were fitted on? That is two dates,
not a file listing, and `staleness` computes it.

DELIBERATELY NO "UPDATE EVERYTHING" BUTTON. The chain measured on this machine is about
ninety minutes of somebody else's API quota, and one click hiding that is the kind of thing
pressed at five o'clock and abandoned. Every job is its own button with its own measured
duration next to it, so the cost is visible before it is incurred rather than after.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nicegui import ui  # noqa: E402

from dandelion import credentials, expectations, freshness, staleness  # noqa: E402
from drivers.inventory import job as find_job  # noqa: E402

#: Data jobs grouped by the account they need. Order within a group is the order they must
#: run: `reconcile-units` and `build-master` read what the ingests wrote.
DATA_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Prices and generation across Europe", "entsoe",
     ("backfill-entsoe", "backfill-entsoe-clusters", "backfill-entsoe-extras",
      "ingest-remit")),
    ("French detail", "rte", ("extract-rte",)),
    ("Weather observations", "", ("extract-meteo",)),
    ("Great Britain", "", ("elexon-gb", "fx-ecb")),
    ("Power-station registries", "", ("registry-mastr", "registry-odre", "registry-opsd",
                                      "registry-repd")),
    ("Then, once downloads are done", "", ("reconcile-units", "build-master", "qc-sources")),
)

#: Data jobs deliberately absent from the page, and why. Anything not here and not in a
#: group is a hole: a job nobody can reach from the window.
NOT_OFFERED: dict[str, str] = {
    # pipeline.py:130 ingests FR day-ahead prices for ONE bidding zone. `backfill-entsoe`
    # fetches the same series for all eight, so offering both would be two buttons where
    # the narrower one is never the right choice.
    "extract-entsoe": "superseded by the multi-zone backfill",
}

#: Which freshness family a job refreshes, so a row can show what it last brought in.
JOB_FAMILY: dict[str, str] = {
    "backfill-entsoe": "Prices",
    "backfill-entsoe-clusters": "Load",
    "backfill-entsoe-extras": "Installed capacity",
    "ingest-remit": "Outages",
    "extract-rte": "French detail",
    "extract-meteo": "Weather observations",
    "elexon-gb": "GB market",
    "fx-ecb": "Exchange rates",
}

STATE_STYLE = {
    "current": ("check_circle", "text-positive"),
    "shipped": ("inventory_2", "text-primary"),
    "stale": ("update", "text-warning"),
    "never": ("radio_button_unchecked", "text-grey-5"),
    "unknown": ("help", "text-grey-6"),
}

#: Years upstream calibrated against (scripts/backfill_entsoe.py:25).
YEARS = [2019, 2022, 2023, 2024, 2025]


def _blocked_by(job_id: str) -> str:
    """Which credential this job is waiting on, in words, or ""."""
    job = find_job(job_id)
    if not job.needs_credentials:
        return ""
    stored = credentials.status_by_field()
    absent = [f for f in job.needs_credentials if not stored.get(f)]
    if not absent:
        return ""
    return ", ".join(credentials.field_label(f) for f in absent)


def _job_row(studio, job_id: str, params_for, running: bool) -> None:
    """One job: what it does, what it would cost, and what is stopping it."""
    job = find_job(job_id)
    blocked = _blocked_by(job_id)
    with ui.row().classes("items-center no-wrap w-full q-py-xs"):
        with ui.column().classes("gap-0").style("width:330px"):
            ui.label(job.title).classes("text-body2")
            note = expectations.before(job_id, years=1)
            if blocked:
                ui.label(f"needs your {blocked}").classes("text-caption text-warning")
            elif note:
                ui.label(note).classes("text-caption text-grey-6")
        ui.space()
        ui.button("Run", on_click=lambda j=job_id: studio.start(j, **params_for(j))) \
            .props("outline dense").set_enabled(not running and not blocked)


def data_page(studio, running: bool) -> None:
    """What you have, and what each download would cost you."""
    picture = studio.picture()
    families = {f.label: f for f in picture.families}

    with ui.card().classes("w-full q-mb-md"):
        ui.label("Downloading data").classes("text-subtitle1")
        ui.label("Each of these uses your own account with the provider. They are separate "
                 "on purpose: together they take well over an hour, and you should be able "
                 "to see what you are starting.").classes("text-caption text-grey-7")

    year = {"value": studio.data_year}

    with ui.card().classes("w-full q-mb-md"):
        with ui.row().classes("items-center no-wrap"):
            ui.label("Year to download").classes("text-body2")
            ui.select(YEARS, value=studio.data_year).props("outlined dense") \
                .style("width:110px").bind_value(studio, "data_year")
            ui.label("Applies to the jobs that work a year at a time.") \
                .classes("text-caption text-grey-6")

    def params_for(job_id: str) -> dict:
        job = find_job(job_id)
        placeholders = " ".join(job.argv) + " ".join(t for g in job.optional_argv for t in g)
        out: dict[str, object] = {}
        if "{years}" in placeholders:
            out["years"] = int(studio.data_year)
        if "{year}" in placeholders or job.id.endswith(("-extras", "-clusters")):
            out["year"] = int(studio.data_year)
        if "{start}" in placeholders:
            out["start"] = f"{studio.data_year}-01-01"
            out["end"] = f"{int(studio.data_year) + 1}-01-01"
        return out

    for title, credential, job_ids in DATA_GROUPS:
        with ui.card().classes("w-full q-mb-sm"):
            with ui.row().classes("items-center w-full justify-between"):
                ui.label(title).classes("text-subtitle2")
                if credential:
                    state = credentials.status().get(credential)
                    ui.label("account set up" if state else "no account yet") \
                        .classes(f"text-caption {'text-positive' if state else 'text-warning'}")
            for job_id in job_ids:
                family = families.get(JOB_FAMILY.get(job_id, ""))
                if family is not None:
                    phrase, severity = freshness.staleness(family.last_ingested)
                    with ui.row().classes("items-center no-wrap w-full"):
                        ui.icon("circle").classes(
                            f"{'text-positive' if severity == 'fresh' else 'text-warning'} "
                            f"text-xs")
                        ui.label(f"{freshness.human_rows(family.rows)} rows, to "
                                 f"{family.coverage_end}, fetched {phrase}") \
                            .classes("text-caption text-grey-7")
                _job_row(studio, job_id, params_for, running)
    _ = year


def models_page(studio, running: bool) -> None:
    """Whether the data has moved on from what the models were fitted on."""
    verdicts = staleness.assess_all(studio.journal, studio.install.data_dir /
                                    "pricemodeling.db", studio.tag,
                                    fits_shipped=studio.fits_shipped)

    with ui.card().classes("w-full q-mb-md"):
        ui.label("Fitted models").classes("text-subtitle1")
        ui.label("A model is out of date when data it was fitted on has changed since. "
                 "That is two dates, and neither the file nor its size can tell you.") \
            .classes("text-caption text-grey-7")

    needing = [v for v in verdicts if v.needs_refit]
    if not needing:
        with ui.card().classes("w-full bg-green-1 q-mb-md"):
            with ui.row().classes("items-center no-wrap"):
                ui.icon("check_circle").classes("text-positive")
                ui.label("Nothing here needs refitting.").classes("text-body2")

    for verdict in verdicts:
        job = find_job(verdict.job_id)
        icon, colour = STATE_STYLE.get(verdict.state, ("help", "text-grey-6"))
        with ui.card().classes("w-full q-mb-sm"):
            with ui.row().classes("items-center no-wrap w-full"):
                ui.icon(icon).classes(colour)
                with ui.column().classes("gap-0").style("width:300px"):
                    ui.label(job.title).classes("text-body2")
                    ui.label(verdict.sentence).classes(f"text-caption {colour}")
                ui.space()
                note = expectations.before(verdict.job_id)
                if note:
                    ui.label(note).classes("text-caption text-grey-6")
                ui.button("Refit", on_click=lambda j=verdict.job_id: studio.start(j)) \
                    .props("outline dense").set_enabled(not running)

            if verdict.state == "shipped":
                ui.label("Refit only if your own history differs from what the release was "
                         "built on. On a partial history a refit is usually worse.") \
                    .classes("text-caption text-grey-6")
            if verdict.undatable:
                ui.label("Cannot date: " + ", ".join(verdict.undatable)) \
                    .classes("text-caption text-grey-6")

    with ui.card().classes("w-full q-mt-md"):
        ui.label("Projections").classes("text-subtitle2")
        ui.label("Each one needs its own model fitted first; the button says so if not.") \
            .classes("text-caption text-grey-7")
        for job_id in ("weathergen-simulate", "demand-project", "res-project",
                       "avail-project"):
            unmet = studio.journal.missing_prerequisites(job_id, studio.tag)
            job = find_job(job_id)
            with ui.row().classes("items-center no-wrap w-full q-py-xs"):
                with ui.column().classes("gap-0").style("width:330px"):
                    ui.label(job.title).classes("text-body2")
                    if unmet:
                        names = ", ".join(find_job(d).title for d in unmet)
                        ui.label(f"waiting on {names}").classes("text-caption text-grey-6")
                ui.space()
                ui.button("Run", on_click=lambda j=job_id: studio.start(j)) \
                    .props("outline dense").set_enabled(not running and not unmet)
