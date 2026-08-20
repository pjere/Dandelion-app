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
                       model: str = CMIP6_DEFAULT_MODEL) -> Preflight:
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
    if trend["explicit_path"]:
        p = Path(trend["explicit_path"])
        if not p.is_absolute():
            p = (config_path.parent / p).resolve()
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

def check_markup_model(reports_dir: Path) -> Preflight:
    """Refuse to project when the fitted markup wedge is not where dispatch will look."""
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


def run_all(config_paths: dict[str, Path]) -> list[Preflight]:
    """Convenience for the job engine: run every applicable check it has inputs for."""
    out: list[Preflight] = []
    if "weathergen_config" in config_paths:
        out.append(check_cmip6_deltas(config_paths["weathergen_config"]))
    if "dispatch_reports_dir" in config_paths:
        out.append(check_markup_model(config_paths["dispatch_reports_dir"]))
    return out
