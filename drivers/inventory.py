"""The single place where knowledge of the upstream codebase lives.

Every invocation the product ever makes of pjere/Dandelion is a Job declared here:
what to run, from which working directory, which credentials it needs, how to tell
success from failure, and what it produces. Nothing else in the product may hardcode
an upstream command. When the owner cuts a new code release, this file and
`anchoring.py` are the only things that should need to change.

Verified against a fresh clone of pjere/Dandelion @ 09a2459 (2026-08-20).

    python drivers/inventory.py --list
    python drivers/inventory.py --selftest --code-root <dir> --python <venv-python>
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# --------------------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------------------
# powersim_core/progress.py renders an in-place bar ONLY on a TTY. Under a pipe - which is
# how the job engine always runs upstream - it emits plain lines instead, at most one every
# `min_interval` seconds (default 30) plus always the first and the last. That is what makes
# honest GUI progress possible without upstream changes. POWERSIM_NO_PROGRESS must stay unset.

#: `_rate()` renders exactly one of: "?", "<n> s/it", "<n> min/it". Pinning those three
#: shapes keeps the greedy tail out of the note, which is separated by two spaces.
_RATE = r"(?:\?|[\d.]+\s(?:s|min)/it)"

PROGRESS_RE = re.compile(
    r"^\[(?P<label>[^\]]+)\]\s+"
    r"(?P<done>\d+)/(?P<total>\d+)\s+(?P<pct>\d+)%\s+"
    r"elapsed\s+(?P<elapsed>[\d:]+)\s+eta\s+(?P<eta>(?:[\d:]+|--:--:--))\s+"
    rf"(?P<rate>{_RATE})"
    r"(?:\s\s+(?P<note>.*))?$"
)

#: Same module, `total <= 0` branch: no bar, no ETA, just a count.
PROGRESS_COUNTER_RE = re.compile(
    rf"^\[(?P<label>[^\]]+)\]\s+(?P<done>\d+)\s+done\s+elapsed\s+(?P<elapsed>[\d:]+)\s+"
    rf"(?P<rate>{_RATE})(?:\s\s+(?P<note>.*))?$"
)

#: Progress labels upstream actually emits, and where they come from.
PROGRESS_LABELS = {
    "weathergen fit": "weathergen/weathergen/cli.py:25 (6 fixed phases)",
    "availability draws": "availability_model/.../projection/engine.py:159",
    "backtest {year}": "dispatch_model/.../rolling/backtest.py:365",
    "{year} windows": "dispatch_model/.../rolling/projection.py:435 (inner loop)",
    "projection {start}-{end}": "dispatch_model/scripts/run_projection_20y.py:76 (outer loop)",
    "deliverables": "dispatch_model/scripts/build_projection_deliverables.py:275",
    "monte-carlo {n} draws": "dispatch_model/scripts/run_montecarlo.py:136",
}


class Kind(Enum):
    CONSOLE = "console"   # a console script installed by the package
    MODULE = "module"     # python -m <pkg>
    SCRIPT = "script"     # python <path> from inside the release tree
    WRAPPER = "wrapper"   # our own thin wrapper: upstream exposes only library functions


class Stage(Enum):
    ADMIN = "admin"
    DATA = "data"
    MODELS = "models"
    PROJECTION = "projection"
    QUALITY = "quality"


@dataclass(frozen=True)
class Job:
    """One upstream invocation, fully specified."""

    id: str
    title: str
    kind: Kind
    stage: Stage
    argv: tuple[str, ...]
    #: Argument groups appended only when EVERY placeholder in the group has a value.
    #: Upstream options like `extract-rte --start/--end` narrow a job that otherwise runs
    #: over the resource's whole declared history; omitting them entirely is the correct
    #: default, so they cannot live in `argv` where a missing value is an error.
    optional_argv: tuple[tuple[str, ...], ...] = ()
    #: working directory, relative to the extracted code root. Never empty: several
    #: upstream paths are cwd-relative (see anchoring.Anchor.CWD).
    cwd: str = "."
    needs_credentials: tuple[str, ...] = ()
    progress_label: str | None = None
    #: stdout patterns that mean the job FAILED even though the exit code is 0.
    failure_markers: tuple[str, ...] = ()
    #: stdout patterns that mean the job degraded silently - surface as a warning card.
    warning_markers: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    resumable: bool = False
    #: job ids that must have succeeded first. Upstream does not enforce these — where a
    #: missing prerequisite degrades silently, the edge is enforced here instead.
    requires: tuple[str, ...] = ()
    #: `drivers.preflight` checks that must pass before the job is allowed to start.
    preflight: tuple[str, ...] = ()
    notes: str = ""
    upstream_ref: str = ""


# Several pricemodeling commands catch their own exceptions, print a French error line and
# still exit 0. Exit code alone is therefore NOT a success signal for them.
# Three ENTSO-E series the dispatch model READS but no shipped entry point WRITES.
#
# `pricemodeling.entsoe.series` defines ingest_installed_capacity, ingest_hydro_storage and
# ingest_ntc, each with a documented rationale. Nothing calls them:
#   - `ingest_all` (series.py:306) covers prices/load/generation/flows and stops there;
#   - `extract-entsoe` (pipeline.py:130) ingests FR day-ahead prices only;
#   - `scripts/backfill_entsoe.py` calls the same four as ingest_all.
#
# The owner's own database has all three — 22 `entsoe:cap:*`, 21 `entsoe:hydro:*` and 22
# `entsoe:ntc:*` sources in its ingest_log — so they were run directly rather than through
# a reproducible path. A user of this product could not reproduce them at all.
#
# What their absence costs, quantified upstream rather than by us:
#   capacity  io/entsoe_hist.py:117 returns {} when the table is missing, and the caller
#             falls back to a p99.9-of-generation proxy. Measured on NL 2024: "proxy 9.1 GW
#             gas vs 15.6 GW real fleet, i.e. half the CCGT fleet invisible and the zone
#             artificially scarce (+22 EUR/MWh level bias, zero negative prints)".
#   hydro     "sans lui la valeur de l'eau structurelle ne peut etre calibree que pour la
#             France, alors que la Suisse - hydraulique a 80 % - est justement la zone ou le
#             modele derape le plus" (series.py:185).
#   ntc       without it the model uses one annual scalar per direction, which upstream
#             measures as wrong at both tails at once (series.py:263).
#
# This wraps the three public functions exactly as `scripts/backfill_entsoe.py` wraps the
# other four — same engine construction, same cwd-relative DB URL, same `_client` helper
# that the shipped script itself uses. No upstream file is touched.
ENTSOE_EXTRAS_RUNNER = (
    "import os;"
    "from datetime import date;"
    "from pricemodeling.db import get_engine;"
    "from pricemodeling.entsoe import series as S;"
    "eng=get_engine('sqlite:///data/pricemodeling.db');"
    "cl=S._client(os.environ['ENTSOE_TOKEN']);"
    "y=int(os.environ['DANDELION_YEAR']);"
    "s=date(y,1,1);e=date(y,12,31);"
    "print('=== %d ===' % y, flush=True);"
    "print('  capacity : %s' % S.ingest_installed_capacity(eng,cl,s,e), flush=True);"
    "print('  hydro    : %s' % S.ingest_hydro_storage(eng,cl,s,e), flush=True);"
    "print('  ntc      : %s' % S.ingest_ntc(eng,cl,s,e), flush=True);"
    "print('DONE', flush=True)"
)


ERREUR = (r"^\[ERREUR\]",)


JOBS: tuple[Job, ...] = (
    # ---------------------------------------------------------------- admin
    Job(
        id="init-db", title="Initialise the SQLite database",
        kind=Kind.MODULE, stage=Stage.ADMIN,
        argv=("{python}", "-m", "pricemodeling", "init-db"),
        produces=("data/pricemodeling.db",),
        upstream_ref="pricemodeling/pipeline.py:48",
    ),
    Job(
        id="status", title="Row counts per table",
        kind=Kind.MODULE, stage=Stage.ADMIN,
        argv=("{python}", "-m", "pricemodeling", "status"),
        notes=(
            "Prints 'Base : <path>' then one line per table. It runs SELECT COUNT(*) on EVERY "
            "table, which on the 16.5 GB master is a full scan - do NOT put this on the dashboard "
            "hot path. Per-source freshness comes from direct read-only SQLite queries against "
            "ingest_log instead."
        ),
        upstream_ref="pricemodeling/pipeline.py:224",
    ),
    Job(
        id="rte-token", title="Test the RTE credentials",
        kind=Kind.MODULE, stage=Stage.ADMIN,
        argv=("{python}", "-m", "pricemodeling", "rte-token"),
        needs_credentials=("RTE_CLIENT_ID", "RTE_CLIENT_SECRET"),
        notes="Wizard credential test. Prints 'OK - jeton obtenu (longueur N)' on success.",
        upstream_ref="pricemodeling/pipeline.py:55",
    ),

    # ---------------------------------------------------------------- data
    Job(
        id="extract-meteo", title="SYNOP weather observations",
        kind=Kind.MODULE, stage=Stage.DATA,
        argv=("{python}", "-m", "pricemodeling", "extract-meteo"),
        resumable=True,
        notes="Incremental by month unless --force. No credentials (Meteo-France open data).",
        upstream_ref="pricemodeling/pipeline.py:67",
    ),
    Job(
        id="extract-rte", title="RTE resources",
        kind=Kind.MODULE, stage=Stage.DATA,
        argv=("{python}", "-m", "pricemodeling", "extract-rte"),
        optional_argv=(("--start", "{start}"), ("--end", "{end}"), ("--only", "{only}")),
        needs_credentials=("RTE_CLIENT_ID", "RTE_CLIENT_SECRET"),
        failure_markers=ERREUR,
        resumable=True,
        notes=(
            "Loops over the resources enabled in config/rte_catalog.yaml and CONTINUES past a "
            "failing resource, printing '[ERREUR] <name>: <exc>' and exiting 0. The job engine "
            "must scan stdout, not just the exit code."
        ),
        upstream_ref="pricemodeling/pipeline.py:91",
    ),
    Job(
        id="extract-entsoe", title="FR day-ahead prices",
        kind=Kind.MODULE, stage=Stage.DATA,
        argv=("{python}", "-m", "pricemodeling", "extract-entsoe"),
        needs_credentials=("ENTSOE_TOKEN",),
        failure_markers=ERREUR,
        resumable=True,
        notes=(
            "FRANCE ONLY, despite the name - bidding_zone defaults to 10YFR-RTE------C. The other "
            "twelve zones come from the backfill-entsoe job. Also swallows exceptions and exits 0."
        ),
        upstream_ref="pricemodeling/pipeline.py:130",
    ),
    Job(
        id="backfill-entsoe", title="Multi-zone ENTSO-E (prices, load, generation, flows)",
        kind=Kind.SCRIPT, stage=Stage.DATA,
        argv=("{python}", "-X", "utf8", "scripts/backfill_entsoe.py", "{years}"),
        cwd=".",
        needs_credentials=("ENTSOE_TOKEN",),
        resumable=True,
        notes=(
            "cwd MUST be the code root: DB_URL is the cwd-relative literal "
            "'sqlite:///data/pricemodeling.db'. Years are positional argv; the default set is "
            "2019, 2022, 2023, 2024. Idempotent via the ingest_log table, so it resumes."
        ),
        upstream_ref="scripts/backfill_entsoe.py",
    ),
    Job(
        id="ingest-remit", title="REMIT outage notifications",
        kind=Kind.MODULE, stage=Stage.DATA,
        argv=("{python}", "-m", "pricemodeling", "ingest-remit"),
        optional_argv=(("--start", "{start}"), ("--end", "{end}"), ("--zones", "{zones}")),
        needs_credentials=("ENTSOE_TOKEN",),
        failure_markers=ERREUR,
        resumable=True,
        notes="Feeds step v. Exits 1 with '[ERREUR] ENTSOE_TOKEN manquant dans .env' when unset.",
        upstream_ref="pricemodeling/pipeline.py:156",
    ),
    Job(
        id="fx-ecb", title="ECB reference rates (GBP)",
        kind=Kind.WRAPPER, stage=Stage.DATA,
        argv=("{python}", "-m", "drivers.wrappers.fx", "--start", "{start}", "--end", "{end}"),
        cwd=".",
        notes=(
            "No upstream CLI. Wraps pricemodeling.fx.ingest_fx(engine, start, end). MANDATORY "
            "PREREQUISITE of elexon-prices: ingest_prices raises ValueError without a "
            "gbp_per_eur callable, refusing to mix GBP and EUR."
        ),
        upstream_ref="pricemodeling/fx.py:53 ingest_fx / :89 rate_fn",
    ),
    Job(
        id="elexon-gb", title="GB / Elexon (generation, load, flows, prices)",
        kind=Kind.WRAPPER, stage=Stage.DATA,
        argv=("{python}", "-m", "drivers.wrappers.elexon", "--start", "{start}", "--end", "{end}"),
        cwd=".",
        resumable=True,
        notes=(
            "No upstream CLI. Wraps pricemodeling.elexon.series.ingest_{generation,flows,load,"
            "prices}. BMRS Insights is a key-less open API. ingest_prices needs gbp_per_eur="
            "fx.rate_fn(config,'GBP'), so run fx-ecb first. Prices chunk at 7 days (the endpoint "
            "rejects 14)."
        ),
        upstream_ref="pricemodeling/elexon/series.py:103,140,183,207",
    ),
    Job(
        id="registry-mastr", title="German plant registry (MaStR)",
        kind=Kind.WRAPPER, stage=Stage.DATA,
        argv=("{python}", "-m", "drivers.wrappers.registries", "mastr"),
        cwd=".",
        notes=(
            "No upstream CLI and NO upstream orchestration at all - registries expose "
            "fetch_bulk/load_bulk_to_sqlite/build, and powersim_core.registry.write is called "
            "only from tests. The product must author the composition; ask the owner to confirm "
            "the exact sequence that produced the shipped lake. Downloads ~7.5 GB to "
            "~/.open-MaStR/ (not relocatable)."
        ),
        upstream_ref="pricemodeling/registries/mastr.py:37,71,167",
    ),
    Job(
        id="registry-odre", title="French plant registry (ODRE)",
        kind=Kind.WRAPPER, stage=Stage.DATA,
        argv=("{python}", "-m", "drivers.wrappers.registries", "odre"),
        cwd=".",
        notes="cwd must be the code root: DEFAULT_RAW is the cwd-relative 'data/raw/odre/...'.",
        upstream_ref="pricemodeling/registries/odre.py:40,67",
    ),
    Job(
        id="registry-opsd", title="Swiss plant registry (OPSD)",
        kind=Kind.WRAPPER, stage=Stage.DATA,
        argv=("{python}", "-m", "drivers.wrappers.registries", "opsd"),
        cwd=".",
        upstream_ref="pricemodeling/registries/opsd.py:30,40",
    ),
    Job(
        id="registry-repd", title="UK plant registry (REPD)",
        kind=Kind.WRAPPER, stage=Stage.DATA,
        argv=("{python}", "-m", "drivers.wrappers.registries", "repd"),
        cwd=".",
        upstream_ref="pricemodeling/registries/repd.py:39,67",
    ),
    Job(
        id="reconcile-units", title="Reconcile production units",
        kind=Kind.MODULE, stage=Stage.DATA,
        argv=("{python}", "-m", "pricemodeling", "reconcile-units"),
        produces=("data/reconciliation_report.csv",),
        upstream_ref="pricemodeling/pipeline.py:179",
    ),
    Job(
        id="backfill-entsoe-extras",
        title="ENTSO-E installed capacity, hydro reservoirs and NTC",
        kind=Kind.MODULE, stage=Stage.DATA,
        argv=("{python}", "-X", "utf8", "-c", ENTSOE_EXTRAS_RUNNER),
        cwd=".",
        needs_credentials=("ENTSOE_TOKEN",),
        resumable=True,
        produces=("entsoe_installed_capacity", "entsoe_hydro_storage", "entsoe_ntc"),
        notes=(
            "Fills the three series `backfill-entsoe` does not. See ENTSOE_EXTRAS_RUNNER for "
            "why they matter: without installed capacity the model sizes stacks from a "
            "generation proxy that upstream measured at +22 EUR/MWh of level bias. cwd MUST "
            "be the code root - the DB URL is the cwd-relative literal used by "
            "scripts/backfill_entsoe.py. Idempotent via ingest_log. The year is passed as "
            "DANDELION_YEAR rather than argv so the runner string stays a constant."
        ),
        upstream_ref="pricemodeling/entsoe/series.py:185,224,263",
    ),
    Job(
        id="build-master", title="Rebuild the hourly master table",
        kind=Kind.MODULE, stage=Stage.DATA,
        argv=("{python}", "-m", "pricemodeling", "build-master"),
        notes="The expensive one. Prints 'Fusion : <stats>'. Run after every ingest.",
        upstream_ref="pricemodeling/pipeline.py:189",
    ),
    Job(
        id="qc-sources", title="Cross-check RTE against ENTSO-E",
        kind=Kind.MODULE, stage=Stage.DATA,
        argv=("{python}", "-m", "pricemodeling", "qc-sources"),
        notes=(
            "Render as a table. --strict exits 1 on unexplained divergence; build-master repairs "
            "RTE gaps silently, so without this a new source defect stays invisible."
        ),
        upstream_ref="pricemodeling/pipeline.py:238",
    ),

    # ---------------------------------------------------------------- models
    Job(
        id="weathergen-cmip6", title="Download CMIP6 climate deltas",
        kind=Kind.CONSOLE, stage=Stage.MODELS,
        argv=("weathergen", "-c", "config.yaml", "fetch-cmip6-deltas",
              "--ssp", "{ssp}", "--target-year", "{target_year}"),
        cwd="weathergen",
        needs_credentials=("CDSAPI_URL", "CDSAPI_KEY"),
        produces=("weathergen/models/cmip6_deltas_{ssp}_{target_year}_mpi_esm1_2_lr.npz",),
        notes=(
            "MANDATORY at install: the product ships the climate trend ON (see "
            "drivers.code_patches.trend_patch), so every projection needs these deltas and "
            "CDS credentials stop being optional. Prerequisite of weathergen-simulate, NOT of "
            "fit. One npz per (ssp, target_year) pair, because both are simulate-time inputs - "
            "the install fetches the config default (ssp245 @ 2050) and the Studio fetches "
            "again whenever the user picks a scenario it has no deltas for. The --model CLI "
            "help says 'default ec_earth3' but the real default is mpi_esm1_2_lr "
            "(cmip6_cds.py:24) - the filename embeds it, so the stale value looks for a file "
            "nothing will ever write."
        ),
        upstream_ref="weathergen/weathergen/cli.py:109-119,164; cmip6_cds.py:116-139",
    ),
    Job(
        id="weathergen-fit", title="Fit the weather generator",
        kind=Kind.CONSOLE, stage=Stage.MODELS,
        argv=("weathergen", "-c", "config.yaml", "fit"),
        cwd="weathergen",
        needs_credentials=("CDSAPI_URL", "CDSAPI_KEY"),
        progress_label="weathergen fit",
        produces=("weathergen/models/fitted.json",),
        notes=(
            "Six fixed phases. Needs CDS because the first fit pulls ~4 GB of ERA5 lazily "
            "(era5_arco). It does NOT touch the climate trend - the trend is a simulate-time "
            "input, so one fitted model serves every scenario (cli.py:88-89)."
        ),
        upstream_ref="weathergen/weathergen/cli.py:22,80-85,155",
    ),
    Job(
        id="weathergen-simulate", title="Simulate weather trajectories",
        kind=Kind.CONSOLE, stage=Stage.MODELS,
        argv=("weathergen", "-c", "config.yaml", "simulate"),
        cwd="weathergen",
        requires=("weathergen-fit",),
        preflight=("cmip6-deltas-present",),
        produces=("weathergen/output/simulation.nc",),
        # After the preflight, this line must never appear. If it does, the check was wrong
        # or the file vanished mid-run - either way the cube is untrended and unusable, so
        # this is a FAILURE marker rather than a warning.
        failure_markers=(r"^\[trend\] enabled but deltas not found",),
        notes=(
            "THIS is where the CMIP6 deltas are consumed - _build_trend is called from "
            "cmd_simulate (cli.py:124), never from fit. With the trend enabled and the deltas "
            "missing, upstream prints one line and carries on: trend.fit returns "
            "Trend(enabled=True, deltas={}) (trend.py:94) and Trend.apply then returns the cube "
            "UNCHANGED (trend.py:45), while simulation.nc's embedded provenance still says the "
            "trend was applied. A present-day climate labelled as the target year, exit code 0. "
            "Hence the preflight. Upstream ships trend.enabled: false, but THE PRODUCT SHIPS IT "
            "ON (owner decision 2026-08-20, drivers.code_patches), so this preflight is on the "
            "default path for every install rather than an edge case. Never pass --no-trend "
            "here: the Monte-Carlo path cannot see CLI flags, so a flag would desync a single "
            "simulation from the ensemble. The trend is switched through the config patch only."
        ),
        upstream_ref="weathergen/weathergen/cli.py:88-106,124; trend.py:45,86-98",
    ),
    Job(
        id="demand-calibrate", title="Calibrate the demand model",
        kind=Kind.CONSOLE, stage=Stage.MODELS,
        argv=("demand-model", "-c", "config.yaml", "calibrate"),
        cwd="demand_model",
        notes="Needs the demand_model[calib] extra (pygam, statsmodels) in the lock.",
        upstream_ref="demand_model/demand_model/cli.py:51",
    ),
    Job(
        id="demand-project", title="Project demand",
        kind=Kind.CONSOLE, stage=Stage.MODELS,
        argv=("demand-model", "-c", "config.yaml", "project"),
        cwd="demand_model",
        upstream_ref="demand_model/demand_model/cli.py:52",
    ),
    Job(
        id="res-calibrate", title="Calibrate the RES conversion chains",
        kind=Kind.CONSOLE, stage=Stage.MODELS,
        argv=("res-model", "-c", "config.yaml", "calibrate"),
        cwd="res_model",
        needs_credentials=("CDSAPI_URL", "CDSAPI_KEY"),
        notes="Pulls ERA5 lazily through cdsapi on first fit.",
        upstream_ref="res_model/res_model/cli.py:48",
    ),
    Job(
        id="res-project", title="Project RES production",
        kind=Kind.CONSOLE, stage=Stage.MODELS,
        argv=("res-model", "-c", "config.yaml", "project"),
        cwd="res_model",
        upstream_ref="res_model/res_model/cli.py:49",
    ),
    Job(
        id="avail-calibrate", title="Fit plant availability",
        kind=Kind.CONSOLE, stage=Stage.MODELS,
        argv=("avail-model", "-c", "config.yaml", "calibrate"),
        cwd="availability_model",
        notes="Consumes the REMIT table, so run ingest-remit first.",
        upstream_ref="availability_model/availability_model/cli.py:48",
    ),
    Job(
        id="avail-project", title="Simulate unit availability",
        kind=Kind.CONSOLE, stage=Stage.MODELS,
        argv=("avail-model", "-c", "config.yaml", "project"),
        cwd="availability_model",
        progress_label="availability draws",
        upstream_ref="availability_model/availability_model/cli.py:49",
    ),
    Job(
        id="dispatch-build-inputs", title="Build commodity, neighbour and FR inputs",
        kind=Kind.CONSOLE, stage=Stage.MODELS,
        argv=("dispatch-model", "-c", "config.yaml", "build-inputs"),
        cwd="dispatch_model",
        notes=(
            "NOT mentioned in the program document but a real subcommand. Fetches the World Bank "
            "pink sheet and the ECB FX zip - network access without user credentials."
        ),
        upstream_ref="dispatch_model/dispatch_model/cli.py:40",
    ),
    Job(
        id="dispatch-backtest", title="Backtest on a historical year",
        kind=Kind.CONSOLE, stage=Stage.MODELS,
        argv=("dispatch-model", "-c", "config.yaml", "backtest", "--year", "{year}"),
        cwd="dispatch_model",
        progress_label="backtest {year}",
        preflight=("fr-history-present", "stack-inputs-present"),
        notes=(
            "The FR leg comes from master_hourly (io/fr_history.py:22), which build_master "
            "fills from the RTE consumption series. ENTSO-E is NOT a substitute: its "
            "fallback in build_master.py:84 covers prod_* generation only, by design. A "
            "backtest therefore needs RTE credentials, not just an ENTSO-E token."
        ),
        upstream_ref="dispatch_model/dispatch_model/cli.py:44",
    ),

    # ---------------------------------------------------------------- projection
    Job(
        id="dispatch-run", title="Single-year projection",
        kind=Kind.CONSOLE, stage=Stage.PROJECTION,
        argv=("dispatch-model", "-c", "{config}", "run", "--year", "{year}"),
        cwd="dispatch_model",
        progress_label="{year} windows",
        preflight=("markup-model-present",),
        notes=(
            "-c defaults to the cwd-relative 'config.yaml'. For a run bundle, pass the absolute "
            "path of the run's overlay - and remember that models_dir/reports_dir/output_dir then "
            "resolve from the OVERLAY's directory (see anchoring.dispatch_reports)."
        ),
        upstream_ref="dispatch_model/dispatch_model/cli.py:41",
    ),
    Job(
        id="projection-20y", title="20-year projection",
        kind=Kind.SCRIPT, stage=Stage.PROJECTION,
        argv=("{python}", "-u", "-X", "utf8", "-W", "ignore", "scripts/run_projection_20y.py"),
        cwd="dispatch_model",
        progress_label="projection {start}-{end}",
        resumable=True,
        preflight=("markup-model-present",),
        notes="Horizon comes from config.yaml, not from a flag.",
        upstream_ref="dispatch_model/scripts/run_projection_20y.py:76",
    ),
    Job(
        id="projection-deliverables", title="Build projection deliverables",
        kind=Kind.SCRIPT, stage=Stage.PROJECTION,
        argv=("{python}", "-u", "-X", "utf8", "scripts/build_projection_deliverables.py"),
        cwd="dispatch_model",
        progress_label="deliverables",
        upstream_ref="dispatch_model/scripts/build_projection_deliverables.py:275",
    ),
    Job(
        id="montecarlo", title="Monte-Carlo ensemble (full chain)",
        kind=Kind.SCRIPT, stage=Stage.PROJECTION,
        argv=("{python}", "-u", "-X", "utf8", "-W", "ignore", "scripts/run_montecarlo.py",
              "--draws", "{draws}", "--workers", "{workers}", "--master-seed", "{seed}"),
        cwd="dispatch_model",
        progress_label="monte-carlo {n} draws",
        resumable=True,
        # Each draw regenerates a weather cube through weathergen, so the trend guard applies
        # here too — 50 draws of untrended 2050 weather would be a very expensive silent error.
        preflight=("markup-model-present", "cmip6-deltas-present"),
        notes=(
            "cwd MUST be dispatch_model/: the cube path 'scratchpad/mc/cube_NNN.nc' is resolved "
            "against cwd while the POWERSIM_WEATHER_CUBE the script exports is built from the "
            "script location - they agree only there. Flags: --draws --start-draw --out "
            "--master-seed --keep-cubes --workers --n-weeks. Resumable: a draw whose "
            "projection_20y_analysis.xlsx exists is skipped. Size workers by RAM, not cores - "
            "upstream's own anchor is 'about 3' on the dev machine, ~4.5 h per draw with "
            "flexibility on. Spawns a ProcessPoolExecutor, so cancellation needs a Windows Job "
            "Object; Popen.terminate() will not reap the grandchildren."
        ),
        upstream_ref="dispatch_model/scripts/run_montecarlo.py",
    ),
    Job(
        id="mc-aggregate", title="Aggregate Monte-Carlo draws",
        kind=Kind.SCRIPT, stage=Stage.PROJECTION,
        argv=("{python}", "-u", "-X", "utf8", "scripts/mc_aggregate.py"),
        cwd="dispatch_model",
        upstream_ref="dispatch_model/scripts/mc_aggregate.py",
    ),
    Job(
        id="sensitivity-ensemble", title="Sensitivity ensemble (dispatch-only)",
        kind=Kind.WRAPPER, stage=Stage.PROJECTION,
        argv=("{python}", "-m", "drivers.wrappers.ensemble", "--config", "{config}",
              "--years", "{years}", "--draws", "{draws}", "--workers", "{workers}"),
        cwd="dispatch_model",
        notes=(
            "Wraps dispatch_model.rolling.montecarlo.run_ensemble(config_path, years, draws, "
            "ref_year=2019, master_seed=0, n_workers=None, avail_years=None, "
            "weather_provider=None, n_weeks=None, write_lake=False, parallel=True) and "
            "ensemble_stats. NOT equivalent to the full chain: one shared ref-year preload, "
            "weather varies only through weather_provider, no per-draw cube and no step v. "
            "Present it honestly as a fast sensitivity, not as the Monte-Carlo. ensemble_stats "
            "returns year, zone, n_draws, ens_mean, ens_p5, ens_p50, ens_p95."
        ),
        upstream_ref="dispatch_model/dispatch_model/rolling/montecarlo.py:80,110",
    ),

    # ---------------------------------------------------------------- quality
    Job(
        id="golden-check", title="Golden-harness check",
        kind=Kind.SCRIPT, stage=Stage.QUALITY,
        argv=("{python}", "tools/golden.py", "check"),
        cwd=".",
        upstream_ref="tools/golden.py",
    ),
    Job(
        id="gate-multiyear", title="Multi-year quality gate",
        kind=Kind.SCRIPT, stage=Stage.QUALITY,
        argv=("{python}", "scripts/gate_multiyear.py"),
        cwd="dispatch_model",
        upstream_ref="dispatch_model/scripts/gate_multiyear.py",
    ),
)


# --------------------------------------------------------------------------------------
# Install-time invariants
# --------------------------------------------------------------------------------------

#: Console scripts the seven editable installs must put on PATH.
CONSOLE_SCRIPTS = ("weathergen", "demand-model", "res-model", "avail-model", "dispatch-model")

#: ADR-8 install order. powersim_core and pricemodeling first; the rest depend on them.
INSTALL_ORDER = (
    "powersim_core", "pricemodeling", "weathergen",
    "demand_model", "res_model", "availability_model", "dispatch_model",
)

#: Imported lazily by upstream, declared in NO pyproject and in no requirements file.
#: Absent, the offline test suite still passes and real ingestion dies at first use.
UNDECLARED_IMPORTS = {
    "cdsapi": "cdsapi",
    "entsoe": "entsoe-py",
    "open_mastr": "open-mastr",
}

#: Declared, but only as OPTIONAL extras - so a lock compiled from `dependencies` alone
#: omits them and the fit commands fail at runtime.
REQUIRED_EXTRAS = {
    "weathergen": ("stats", "viz"),      # statsmodels, scikit-learn, pyextremes, xclim / matplotlib, jinja2
    "demand_model": ("calib", "viz"),    # pygam, statsmodels
}

#: Tracked files that live inside a directory the product wants to relocate. They ship with
#: the release, the code READS them, and a naive junction hides them. Seed and hash-check.
SEEDED_FROM_RELEASE = (
    "dispatch_model/reports/markup_model.json",
    "availability_model/reports/methodology.md",
)


#: Preflight checks implemented in `drivers.preflight`. A job may only name one of these.
IMPLEMENTED_PREFLIGHTS = ("cmip6-deltas-present", "markup-model-present",
                          "fr-history-present", "stack-inputs-present")


def validate_registry() -> list[str]:
    """Internal consistency of the registry. Returns a list of problems (empty is good).

    Cheap to run and worth running in CI: the registry is hand-maintained, and a `requires`
    pointing at a renamed job or a `preflight` naming a check nobody implemented would fail
    open — the job would simply run unguarded, which is the exact class of silent problem
    these fields exist to prevent.
    """
    problems: list[str] = []
    ids = {j.id for j in JOBS}
    if len(ids) != len(JOBS):
        problems.append("duplicate job ids")
    for j in JOBS:
        for dep in j.requires:
            if dep not in ids:
                problems.append(f"{j.id}: requires unknown job {dep!r}")
        for check in j.preflight:
            if check not in IMPLEMENTED_PREFLIGHTS:
                problems.append(f"{j.id}: preflight {check!r} is not implemented")
        for pattern in j.failure_markers + j.warning_markers:
            try:
                re.compile(pattern)
            except re.error as exc:
                problems.append(f"{j.id}: bad marker regex {pattern!r} ({exc})")
        if not j.argv:
            problems.append(f"{j.id}: empty argv")
        if j.kind is Kind.CONSOLE and j.argv[0].startswith("{"):
            problems.append(f"{j.id}: console job should invoke its script, not a placeholder")
    return problems


def job(job_id: str) -> Job:
    """Look one up, loudly."""
    for j in JOBS:
        if j.id == job_id:
            return j
    raise KeyError(f"no such job: {job_id!r}")


# --------------------------------------------------------------------------------------
# Selftest
# --------------------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True


def _run(argv: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError as exc:
        return 127, f"not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def selftest(code_root: Path, python: Path) -> list[Check]:
    """Prove that an extracted release + provisioned venv can actually be driven."""
    checks: list[Check] = []
    scripts_dir = python.parent

    # 1. the release tree looks like the monorepo we expect
    for pkg in INSTALL_ORDER:
        checks.append(Check(f"release tree has {pkg}/pyproject.toml",
                            (code_root / pkg / "pyproject.toml").is_file()))
    checks.append(Check("release tree has config/settings.yaml",
                        (code_root / "config" / "settings.yaml").is_file()))
    checks.append(Check("release tree has scenarios.xlsx",
                        (code_root / "scenarios.xlsx").is_file()))

    # 2. editable install anchored the package at the release root, not at site-packages
    rc, out = _run([str(python), "-c",
                    "import pricemodeling.config as c; print(c.PROJECT_ROOT)"])
    anchored = rc == 0 and out.strip() and Path(out.strip()) == code_root.resolve()
    checks.append(Check(
        "pricemodeling.PROJECT_ROOT == code root",
        bool(anchored),
        f"got {out.strip() or rc!r}, expected {code_root.resolve()}"
        + ("" if rc else "  (a non-editable install lands in site-packages and "
                         "load_settings() then raises FileNotFoundError)"),
    ))

    # 3. settings actually load through that anchoring
    rc, out = _run([str(python), "-c",
                    "from pricemodeling.config import load_settings as l; print(l().db_path)"],
                   cwd=code_root)
    checks.append(Check("load_settings() resolves the database path", rc == 0, out.strip()[:200]))

    # 4. the five console scripts resolve
    for name in CONSOLE_SCRIPTS:
        exe = scripts_dir / f"{name}.exe"
        found = exe.is_file() or (scripts_dir / name).is_file()
        checks.append(Check(f"console script '{name}' installed", found, str(exe)))

    # 5. every entry point answers --help
    rc, out = _run([str(python), "-m", "pricemodeling", "--help"], cwd=code_root)
    checks.append(Check("python -m pricemodeling --help", rc == 0, out.strip().splitlines()[:1]
                        and out.strip().splitlines()[0][:120] or ""))
    for name, sub in (("weathergen", "weathergen"), ("demand-model", "demand_model"),
                      ("res-model", "res_model"), ("avail-model", "availability_model"),
                      ("dispatch-model", "dispatch_model")):
        rc, out = _run([str(scripts_dir / name), "--help"], cwd=code_root / sub)
        checks.append(Check(f"{name} --help", rc == 0, out.strip()[:120]))

    # 6. the undeclared lazy imports are present
    for mod, dist in UNDECLARED_IMPORTS.items():
        rc, out = _run([str(python), "-c", f"import {mod}"])
        checks.append(Check(f"lazy import '{mod}' available (pip: {dist})", rc == 0,
                            "declared in no upstream pyproject - the lock must supply it"))

    # 7. the optional extras that the fit commands need
    for mod in ("statsmodels", "sklearn", "pyextremes", "xclim", "pygam", "matplotlib"):
        rc, _ = _run([str(python), "-c", f"import {mod}"])
        checks.append(Check(f"extra '{mod}' available", rc == 0,
                            "declared only as an optional extra - compile the lock WITH extras",
                            fatal=False))

    # 8. tracked artifacts that a naive junction would hide
    for rel in SEEDED_FROM_RELEASE:
        checks.append(Check(f"tracked artifact present: {rel}", (code_root / rel).is_file(),
                            "ships with the release and is READ at runtime - seed it into the "
                            "relocated store and hash-check it"))

    # 9. progress lines will be parseable (the module exists and is not silenced)
    rc, out = _run([str(python), "-c",
                    "from powersim_core.progress import Progress; print('ok')"])
    checks.append(Check("powersim_core.progress importable (GUI progress source)", rc == 0,
                        out.strip()[:120]))

    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="print the job registry")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--selftest", action="store_true", help="probe a real release + venv")
    ap.add_argument("--code-root", type=Path, help="extracted release tree")
    ap.add_argument("--python", type=Path, help="the provisioned venv's python.exe")
    args = ap.parse_args(argv)

    problems = validate_registry()
    if problems:
        print("job registry is inconsistent:")
        for p in problems:
            print(f"  - {p}")
        return 2

    if args.list or not args.selftest:
        if args.json:
            print(json.dumps([{
                "id": j.id, "title": j.title, "kind": j.kind.value, "stage": j.stage.value,
                "argv": list(j.argv), "cwd": j.cwd, "credentials": list(j.needs_credentials),
                "progress_label": j.progress_label, "resumable": j.resumable,
                "requires": list(j.requires), "preflight": list(j.preflight),
                "produces": list(j.produces),
                "failure_markers": list(j.failure_markers),
                "warning_markers": list(j.warning_markers),
                "upstream_ref": j.upstream_ref, "notes": j.notes,
            } for j in JOBS], indent=2))
        else:
            for stage in Stage:
                rows = [j for j in JOBS if j.stage is stage]
                if not rows:
                    continue
                print(f"\n=== {stage.value.upper()} ===")
                for j in rows:
                    cred = f"  [{','.join(j.needs_credentials)}]" if j.needs_credentials else ""
                    print(f"  {j.id:24s} {j.kind.value:8s} cwd={j.cwd:16s} {j.title}{cred}")
            print(f"\n{len(JOBS)} jobs - "
                  f"{sum(1 for j in JOBS if j.kind is Kind.WRAPPER)} need a drivers/ wrapper "
                  f"(no upstream CLI exists)")
        return 0

    if not args.code_root or not args.python:
        ap.error("--selftest needs --code-root and --python")

    checks = selftest(args.code_root, args.python)
    width = max(len(c.name) for c in checks)
    failed = 0
    for c in checks:
        mark = "OK  " if c.ok else ("FAIL" if c.fatal else "WARN")
        if not c.ok and c.fatal:
            failed += 1
        print(f"[{mark}] {c.name:{width}s}  {c.detail if not c.ok else ''}".rstrip())
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
