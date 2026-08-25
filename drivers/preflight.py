"""Checks that must pass BEFORE a job runs, because upstream will not fail on its own.

Upstream is a research codebase: several paths degrade quietly rather than stopping, which
is the right choice for the person who wrote them and the wrong one for a user who cannot
read the source. Where a degradation would change numbers without changing the exit code,
the product refuses to start the job instead.

Each check returns a `Preflight` rather than raising, so the GUI can explain the problem
and offer the fixing job.

Verified against pjere/Dandelion @ 09a2459.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------------------
# CMIP6 climate-trend deltas
# --------------------------------------------------------------------------------------
# THE FAILURE THIS PREVENTS
#
# `weathergen simulate` builds the climate trend at simulate time (cli.py:124 ->
# _build_trend). When `trend.enabled` is true but the deltas file is missing:
#
#   1. cli.py:105  prints "[trend] enabled but deltas not found: <file>. Run
#                  'fetch-cmip6-deltas' first."  and CONTINUES;
#   2. trend.py:94 only loads deltas `if t.enabled and path and Path(path).exists()`, so it
#                  returns Trend(enabled=True, deltas={});
#   3. trend.py:45 `if not self.enabled or not self.deltas: return cube` — the cube comes
#                  back UNCHANGED, i.e. a zero climate adjustment;
#   4. cli.py:130+ embeds the full config in simulation.nc's attributes, which still says
#                  trend enabled, ssp245, target 2050.
#
# The result is a 2050 weather cube carrying present-day climate while every downstream
# price and every manifest claims it is trended. Exit code 0 throughout.

#: cmip6_cds.py:24. NOTE: the CLI help for `--model` still says "default ec_earth3"; the
#: real default changed to MPI ("EC-Earth3 has broken roocs subsetting on CDS"). The
#: filename embeds the model, so using the stale value silently looks for the wrong file.
CMIP6_DEFAULT_MODEL = "mpi_esm1_2_lr"

#: Hours of French demand a backtest year may be missing before it is refused. ABSOLUTE,
#: not proportional: 1% of a year is 88 hours, enough to hide a missing Feb 29 or three
#: whole days. Twelve covers a DST transition plus the odd genuine hole and nothing more.
MISSING_HOURS_ALLOWED = 12

#: Every column `io/fr_history.py:26` puts in its SELECT. It is a fixed list, so a master
#: table missing ANY of them fails the query outright — `conso_realised` being present
#: proves nothing about the rest.
#:
#: These columns are created by build_master's PIVOT of the RTE generation series, which
#: means the master's shape depends on WHICH YEARS were ingested. Measured against the
#: owner's full 2014-2026 master: every column below carries data from 2014 EXCEPT
#: `prod_wind_offshore`, whose first non-null value is in 2023 — France had no offshore
#: wind generation to report before then. So a master built from 2019 alone has no such
#: column, and the 2019 backtest died with `no such column: prod_wind_offshore` on exactly
#: that database. Offshore wind is the only member of this list with the problem, which is
#: why the remedy fetches a 2023 slice specifically.
FR_HISTORY_COLUMNS = (
    "conso_realised",
    "prod_solar", "prod_wind_onshore", "prod_wind_offshore",
    "prod_hydro_run_of_river_and_poundage",
    "prod_nuclear", "prod_fossil_gas", "prod_fossil_hard_coal", "prod_fossil_oil",
    "prod_biomass", "prod_waste", "prod_hydro_water_reservoir", "prod_hydro_pumped_storage",
)


@dataclass
class Preflight:
    """Outcome of one pre-run check."""

    ok: bool
    check: str
    reason: str = ""
    #: job id that would fix this, for the GUI to offer as a one-click remedy.
    remedy_job: str | None = None
    #: arguments the remedy job needs.
    remedy_args: dict[str, Any] | None = None
    detail: dict[str, Any] | None = None


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def models_dir_of(config_path: Path) -> Path:
    """Resolve a weathergen-style `run.models_dir` the way upstream does.

    Config-anchored: relative to the config FILE's directory, absolute passes through
    (weathergen/config.py:44).
    """
    raw = _load_yaml(config_path)
    rel = (raw.get("run") or {}).get("models_dir", "models")
    p = Path(rel)
    return p if p.is_absolute() else (config_path.parent / p).resolve()


def deltas_filename(ssp: str, target_year: int, model: str = CMIP6_DEFAULT_MODEL) -> str:
    """The exact name `_build_trend` looks for (cli.py:101, cmip6_cds.py:137)."""
    return f"cmip6_deltas_{ssp}_{target_year}_{model}.npz"


def resolve_trend(config_path: Path, ssp: str | None = None,
                  target_year: int | None = None) -> dict[str, Any]:
    """The effective trend settings, applying simulate-time overrides like upstream does.

    `--ssp` and `--target-year` are SIMULATE-time inputs that override the config, so the
    deltas a run needs are not fixed at install time: one fitted model serves any scenario,
    and each (ssp, target_year) pair needs its own npz.
    """
    tcfg = dict(_load_yaml(config_path).get("trend") or {})
    return {
        "enabled": bool(tcfg.get("enabled", False)),
        "ssp": ssp or tcfg.get("ssp", "ssp245"),
        "target_year": int(target_year or tcfg.get("target_year", 2050)),
        "explicit_path": tcfg.get("cmip6_deltas_path"),
    }


def check_cmip6_deltas(config_path: Path, ssp: str | None = None,
                       target_year: int | None = None,
                       model: str = CMIP6_DEFAULT_MODEL,
                       cwd: Path | None = None) -> Preflight:
    """Refuse to simulate with the trend on and no deltas to apply.

    Passes trivially when the trend is off — that is an honest untrended run, and upstream's
    shipped config has `trend.enabled: false`.
    """
    check = "cmip6-deltas-present"
    if not config_path.is_file():
        return Preflight(False, check, f"weathergen config not found: {config_path}")

    trend = resolve_trend(config_path, ssp, target_year)
    if not trend["enabled"]:
        return Preflight(True, check, "climate trend disabled - no deltas required",
                         detail=trend)

    # An explicit path in the config wins over the derived name (cli.py:100).
    #
    # It is resolved differently from every other path in this config. `Config.resolve`
    # (weathergen/config.py:35) is config-file-relative, and `models_dir` likewise — but an
    # explicit `cmip6_deltas_path` never passes through either. It goes to trend.fit, which
    # tests it with a BARE `Path(path).exists()` (trend.py:94), i.e. relative to the
    # PROCESS's working directory. Resolving it config-relative here would let this check
    # pass on a file upstream will not find, or refuse one it would.
    if trend["explicit_path"]:
        p = Path(trend["explicit_path"])
        if not p.is_absolute():
            p = ((cwd or Path.cwd()) / p).resolve()
        if p.is_file():
            return Preflight(True, check, f"deltas present: {p.name}", detail={**trend, "path": str(p)})
        return Preflight(
            False, check,
            f"trend.cmip6_deltas_path points at a file that does not exist: {p}",
            remedy_job="weathergen-cmip6",
            remedy_args={"ssp": trend["ssp"], "target_year": trend["target_year"]},
            detail={**trend, "path": str(p)},
        )

    path = models_dir_of(config_path) / deltas_filename(trend["ssp"], trend["target_year"], model)
    if path.is_file():
        return Preflight(True, check, f"deltas present: {path.name}",
                         detail={**trend, "path": str(path)})

    return Preflight(
        False, check,
        (
            f"the climate trend is enabled for {trend['ssp']} @ {trend['target_year']} but "
            f"{path.name} is missing. Upstream would print one line and simulate anyway, "
            f"producing a {trend['target_year']} cube with NO climate adjustment while the "
            f"run metadata still claims the trend was applied. Fetch the deltas first."
        ),
        remedy_job="weathergen-cmip6",
        remedy_args={"ssp": trend["ssp"], "target_year": trend["target_year"], "model": model},
        detail={**trend, "path": str(path)},
    )


# --------------------------------------------------------------------------------------
# Fitted markup wedge
# --------------------------------------------------------------------------------------
# Same class of failure, different file: dispatch_model/reports/markup_model.json is a
# TRACKED input that ships in the release; markup.py:300 loads it for every projected year
# and markup.py:285 falls back to clipped SMC when it is absent, silently. Any relocation
# of reports_dir must therefore seed it - see anchoring.dispatch_reports.

def dispatch_reports_dir(config_path: Path) -> Path:
    """Where dispatch will look for its reports, resolved the way upstream resolves it.

    `Config.reports_dir` is `(self.path.parent / run.reports_dir).resolve()`
    (dispatch_model/config.py:74). It is anchored to the CONFIG FILE, not to the code tree
    — so a run bundle whose overlay lives elsewhere moves reports_dir with it. Hardcoding
    `<code>/dispatch_model/reports` is right only for the shipped config, and `dispatch-run`
    takes `-c {config}` precisely so it can be given something else.
    """
    config_path = Path(config_path)
    raw = _load_yaml(config_path) if config_path.is_file() else {}
    rel = (raw.get("run") or {}).get("reports_dir", "reports")
    p = Path(rel)
    return p if p.is_absolute() else (config_path.parent / p).resolve()


def check_markup_model(reports_dir: Path) -> Preflight:
    """Refuse to project when the fitted markup wedge is not where dispatch will look.

    Takes the RESOLVED directory; callers with a config in hand should get it from
    `dispatch_reports_dir` so an overlay is honoured.
    """
    check = "markup-model-present"
    path = Path(reports_dir) / "markup_model.json"
    if path.is_file():
        return Preflight(True, check, f"markup wedge present: {path}")
    return Preflight(
        False, check,
        (
            f"markup_model.json is missing from {reports_dir}. It ships with the code "
            f"release and is an INPUT to every projected year; without it apply_markup "
            f"falls back to clipped SMC silently, so prices would differ from the "
            f"reference model with no error anywhere."
        ),
        remedy_job=None,
        detail={"path": str(path)},
    )



# --------------------------------------------------------------------------------------
# French history for a backtest
# --------------------------------------------------------------------------------------
# THE FAILURE THIS PREVENTS
#
# `backtest` opens the year with `load_fr_netload` (io/fr_history.py:22), which reads
# `conso_realised` and the `prod_*` columns straight out of `master_hourly`. That table is
# built by `pricemodeling.merge.build_master` from the RTE series
# (`rte_consumption_short_term` -> `conso`), and its docstring is explicit that the FR leg
# works "without any ENTSO-E dependency".
#
# The ENTSO-E fallback in build_master.py:84 covers GENERATION only — every key in
# `ENTSOE_FALLBACK` is a `prod_*` column. There is deliberately no counterpart for
# consumption: "ENTSO-E ecarte la jambe consommation" (build_master.py:81). So an install
# holding a complete ENTSO-E history still cannot back-test: `load_fr_netload` returns an
# empty frame and the run dies much later inside pandas, after the config, the workbook,
# the commodity model and every neighbour stack have already been built.
#
# An ENTSO-E token alone therefore does NOT unblock a backtest. RTE credentials do.

def check_fr_history(database: Path, year: int) -> Preflight:
    """Refuse to back-test a year whose French demand history is not in `master_hourly`.

    Read-only, and cheap: one indexed range count rather than a scan.
    """
    import sqlite3

    check = "fr-history-present"
    database = Path(database)
    if not database.is_file():
        return Preflight(
            False, check,
            f"There is no database at {database} yet. A backtest needs French demand and "
            f"generation history, which is downloaded with your RTE account.",
            remedy_job="extract-rte", remedy_args={"years": year},
            detail={"database": str(database)},
        )
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True,
                                     timeout=2.0)
        connection.execute("PRAGMA query_only = 1")
        with connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='master_hourly'"
            ).fetchone()
            hours, has_column, missing = 0, False, ()
            if table:
                columns = {row[1] for row in
                           connection.execute("PRAGMA table_info(master_hourly)")}
                has_column = "conso_realised" in columns
                missing = tuple(c for c in FR_HISTORY_COLUMNS if c not in columns)
                if has_column:
                    hours = connection.execute(
                        "SELECT COUNT(conso_realised) FROM master_hourly "
                        "WHERE ts_utc >= ? AND ts_utc < ?",
                        (f"{year}-01-01", f"{year + 1}-01-01"),
                    ).fetchone()[0]
        connection.close()
    except sqlite3.DatabaseError as exc:
        return Preflight(False, check, f"The database could not be read: {exc}",
                         detail={"database": str(database)})

    if not table:
        return Preflight(
            False, check,
            "The master table has not been built yet. Ingested data has to be merged into "
            "`master_hourly` before any backtest can read it.",
            remedy_job="build-master", detail={"database": str(database)},
        )
    if not has_column:
        # Measured: with ENTSO-E ingested and no RTE, build_master emits master_hourly with
        # price columns ONLY. The ENTSO-E fallback cannot bootstrap the French columns —
        # build_master.py:120 skips any column not already present — so it repairs RTE data
        # but never substitutes for its absence.
        return Preflight(
            False, check,
            "The master table has no French demand column. It was built from ENTSO-E data "
            "alone, which carries prices and generation but not consumption, so there is "
            "nothing for a backtest to read. French demand comes from RTE.",
            remedy_job="extract-rte", remedy_args={"years": year},
            detail={"database": str(database), "column": "conso_realised"},
        )

    if missing:
        return Preflight(
            False, check,
            f"The master table is missing {len(missing)} of the columns a backtest reads: "
            f"{', '.join(missing)}. These are created by pivoting the RTE generation "
            f"series, so the table's shape follows whichever years were downloaded — "
            f"France reported no offshore wind generation before 2023, for instance, so a "
            f"master built from 2019 alone has no column for it. Downloading a recent year "
            f"as well and rebuilding the master creates the full set.",
            remedy_job="extract-rte",
            remedy_args={"only": "generation_per_type", "start": "2023-01-01",
                         "end": "2024-01-01"},
            detail={"missing": list(missing)},
        )

    #: A leap year has 8784 hours. The tolerance is ABSOLUTE, not a percentage: 1% of a
    #: year is 88 hours, which would quietly accept a missing Feb 29 (24 h) or three
    #: missing days. Twelve hours covers a DST transition plus the odd genuine hole —
    #: the owner's real 2019 is 8,759 of 8,760 — and nothing larger.
    expected = 8784 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 8760
    if expected - hours > MISSING_HOURS_ALLOWED:
        return Preflight(
            False, check,
            f"{year} has {hours:,} hours of French demand in master_hourly, "
            f"{expected - hours:,} short of the {expected:,} the year contains. A backtest "
            f"on a partial year produces numbers that look complete and are not.",
            remedy_job="extract-rte", remedy_args={"years": year},
            detail={"hours": hours, "expected": expected, "missing": expected - hours},
        )
    return Preflight(True, check, f"{year}: {hours:,} hours of French demand present",
                     detail={"hours": hours, "expected": expected})



# --------------------------------------------------------------------------------------
# ENTSO-E stack-sizing inputs
# --------------------------------------------------------------------------------------
# THE FAILURE THIS PREVENTS
#
# `io/entsoe_hist.py:117 load_installed_capacity` swallows a missing table and returns {}:
#
#     except Exception:  # noqa: BLE001  (table may not exist yet)
#         return {}
#
# The caller then sizes every neighbour stack from a p99.9-of-generation proxy. Upstream
# measured the cost on NL 2024 and wrote it in the docstring: "proxy 9.1 GW gas vs 15.6 GW
# real fleet, i.e. half the CCGT fleet invisible and the zone artificially scarce
# (+22 EUR/MWh level bias, zero negative prints)".
#
# So the backtest completes, prints a full set of metrics, and is wrong by tens of euros
# per MWh in the affected zones. Nothing in the output says so. Since no shipped entry
# point fills the table (see ENTSOE_EXTRAS_RUNNER in drivers.inventory), the DEFAULT state
# of a correctly-followed install is the degraded one — which is why this is a refusal
# rather than a warning.

def check_stack_inputs(database: Path, year: int) -> Preflight:
    """Refuse to back-test when installed capacity is missing, since the fallback is silent."""
    import sqlite3

    check = "stack-inputs-present"
    database = Path(database)
    if not database.is_file():
        return Preflight(False, check, f"There is no database at {database} yet.",
                         remedy_job="backfill-entsoe-extras", remedy_args={"year": year})
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True,
                                     timeout=2.0)
        connection.execute("PRAGMA query_only = 1")
        with connection:
            present = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            zones = 0
            if "entsoe_installed_capacity" in present:
                zones = connection.execute(
                    "SELECT COUNT(DISTINCT series_key) FROM entsoe_installed_capacity"
                ).fetchone()[0]
            units = 0
            if "dim_production_unit" in present:
                units = connection.execute(
                    "SELECT COUNT(*) FROM dim_production_unit").fetchone()[0]
            remit = "entsoe_unavailability" in present
        connection.close()
    except sqlite3.DatabaseError as exc:
        return Preflight(False, check, f"The database could not be read: {exc}")

    # dim_production_unit maps each EIC code to a fuel type. io/fr_fleet.py:102 reads it and
    # then keeps only rows whose fuel_type is known, so an EMPTY registry yields an empty
    # French fleet — build_fr_stack then iterates over nothing and the backtest runs with no
    # French dispatchable units at all. Nothing raises; the prices are simply nonsense.
    if not units:
        return Preflight(
            False, check,
            "The production-unit registry is empty, so the model has no French power "
            "stations to dispatch. Reconciling units fills it from the data already "
            "downloaded - it needs no account and takes a moment.",
            remedy_job="reconcile-units",
            detail={"units": units, "zones": zones},
        )
    if not zones:
        return Preflight(
            False, check,
            "Installed generation capacity has not been downloaded. Without it the model "
            "sizes each neighbouring country's power stations from observed output "
            "instead, which upstream measured as making a zone look far smaller than it "
            "is - around 22 EUR/MWh of price bias, with nothing in the results to say so.",
            remedy_job="backfill-entsoe-extras", remedy_args={"year": year},
            detail={"zones": zones},
        )
    # Unlike the two above this one CRASHES rather than degrading: backtest.py:349 calls
    # nuclear_unavailable_mw unconditionally whenever flexibility is on, which is the
    # default, and unavailability.py:169 queries entsoe_unavailability with no guard. A
    # loud failure is better than a quiet one, but it arrives roughly two minutes in, after
    # the year has been preloaded and every neighbour stack built. Catching it up front
    # costs nothing.
    if not remit:
        return Preflight(
            False, check,
            "Power-station outage notifications have not been downloaded. The model uses "
            "them for the true French nuclear availability, and without them a backtest "
            "stops partway through rather than finishing.",
            remedy_job="ingest-remit",
            remedy_args={"start": f"{year}-01-01", "end": f"{year + 1}-01-01"},
            detail={"zones": zones, "units": units, "remit": remit},
        )
    return Preflight(True, check,
                     f"installed capacity for {zones} zones, {units:,} units in the registry",
                     detail={"zones": zones, "units": units, "remit": remit})



# --------------------------------------------------------------------------------------
# Cluster zones for the requested year
# --------------------------------------------------------------------------------------
# THE FAILURE THIS PREVENTS
#
# dispatch_model's config declares thirteen zones: eight real ones plus four virtual
# clusters built from constituent bidding zones (neighbours/blocks.py:114). Those clusters
# were added at different times for different studies — IT_SOUTH came from issue #142,
# whose evidence is all measured on 2024.
#
# A cluster whose constituents have no data for the requested year does not fail. It is
# built from whatever is there, and prices whatever that implies. Measured on a 2019
# backtest, with the full 13-zone config and every ENTSO-E series ingested for that year:
#
#     IT_SOUTH   mean 181.19 EUR/MWh   max 15000.000   <- the LP's value of lost load
#     IT_NORTH   moved from -2.6% to -7.3% once IT_SOUTH was modelled alongside it
#
# Two causes, both structural rather than fixable by downloading more:
#   * IT_CALA did not exist as a separate bidding zone in 2019 — it was carved out of
#     IT_SUD later — so ENTSO-E returns nothing for it and one sixth of the cluster is
#     simply absent;
#   * ENTSO-E publishes Italian installed capacity at COUNTRY level ("IT"), never per
#     bidding zone, so load_installed_capacity finds nothing for any Italian zone and the
#     stack falls back to a generation proxy.
#
# The number that comes out is not a bad estimate; it is 68 hours of unserved energy priced
# at VoLL and a cluster mean four times the market. It looks like a result.

#: neighbours/blocks.py:114. Kept here rather than imported: drivers/ describes upstream, it
#: does not run it, and a check that needs the model importable is a check that cannot run
#: before the environment is built.
CLUSTER_CONSTITUENTS: dict[str, tuple[str, ...]] = {
    "IT_SOUTH": ("IT_CNOR", "IT_CSUD", "IT_SUD", "IT_CALA", "IT_SICI", "IT_SARD"),
    "NL": ("NL",),
    "DK": ("DK_1", "DK_2"),
    "PL_CZ": ("PL", "CZ"),
    "AT_SI": ("AT", "SI"),
}


def check_cluster_zones(database: Path, year: int) -> Preflight:
    """Refuse a year whose virtual cluster zones have no data behind them."""
    import sqlite3

    check = "cluster-zones-present"
    database = Path(database)
    if not database.is_file():
        return Preflight(False, check, f"There is no database at {database} yet.")
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True,
                                     timeout=2.0)
        connection.execute("PRAGMA query_only = 1")
        with connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            # `with connection` is a TRANSACTION scope, not a closing one — closing inside
            # it makes the exit raise "Cannot operate on a closed database". Collect first,
            # close once, decide afterwards.
            has_load = "entsoe_load" in tables
            empty: dict[str, list[str]] = {}
            if has_load:
                for cluster, members in CLUSTER_CONSTITUENTS.items():
                    missing = [
                        z for z in members
                        if not connection.execute(
                            "SELECT 1 FROM entsoe_load WHERE series_key = ? "
                            "AND ts_utc >= ? AND ts_utc < ? LIMIT 1",
                            (z, f"{year}-01-01", f"{year + 1}-01-01")).fetchone()
                    ]
                    if missing:
                        empty[cluster] = missing
        connection.close()
        if not has_load:
            return Preflight(False, check, "No ENTSO-E load has been downloaded yet.",
                             remedy_job="backfill-entsoe", remedy_args={"years": year})
    except sqlite3.DatabaseError as exc:
        return Preflight(False, check, f"The database could not be read: {exc}")

    if empty:
        detail = "; ".join(f"{c} is missing {', '.join(z)}" for c, z in empty.items())
        # NOT a refusal. The other zones are perfectly modellable and France - the point of
        # the exercise - does not need Italy's south. `ok=True` with a populated `detail`
        # means "run, but without these", and drivers.config_overlay does the removing.
        return Preflight(
            True, check,
            f"Some grouped neighbour zones have no {year} data and will be left out of "
            f"this run: {detail}. A group built from part of itself still produces prices, "
            f"and they can be wildly wrong - a 2019 run priced southern Italy at 181 "
            f"EUR/MWh against a market near 50. Some of this is not downloadable: a "
            f"bidding zone that did not exist in {year} has nothing to fetch.",
            remedy_job=None,
            detail={"incomplete": {c: list(z) for c, z in empty.items()}},
        )
    return Preflight(True, check, f"all {len(CLUSTER_CONSTITUENTS)} grouped zones have "
                                  f"{year} data")


def run_all(config_paths: dict[str, Path]) -> list[Preflight]:
    """Convenience for the job engine: run every applicable check it has inputs for."""
    out: list[Preflight] = []
    if "weathergen_config" in config_paths:
        out.append(check_cmip6_deltas(config_paths["weathergen_config"]))
    if "dispatch_reports_dir" in config_paths:
        out.append(check_markup_model(config_paths["dispatch_reports_dir"]))
    if "database" in config_paths and "backtest_year" in config_paths:
        out.append(check_fr_history(config_paths["database"],
                                    int(config_paths["backtest_year"])))
        out.append(check_stack_inputs(config_paths["database"],
                                      int(config_paths["backtest_year"])))
        out.append(check_cluster_zones(config_paths["database"],
                                       int(config_paths["backtest_year"])))
    return out
