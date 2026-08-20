"""Where upstream puts things, and how to relocate it.

Upstream resolves paths four different ways. Every store the product must relocate
off the code tree is listed here with the mode that governs it, because the mode
decides the mechanism (junction vs config overlay vs env var vs cwd).

Verified against a fresh clone of pjere/Dandelion @ 09a2459 (2026-08-20).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Anchor(Enum):
    """How a path is resolved by upstream."""

    FILE = "file"        # from the .py file's location (PROJECT_ROOT) - no override exists
    CONFIG = "config"    # from the config.yaml file's own directory; absolute values pass through
    ENV = "env"          # from an environment variable
    CWD = "cwd"          # from the process working directory - the invocation must set cwd
    HOME = "home"        # from the user profile (~) - not relocatable


@dataclass(frozen=True)
class Store:
    """One directory or file upstream reads/writes that must not live in the code tree."""

    key: str
    anchor: Anchor
    upstream_path: str          # where it lands with no intervention, relative to the code root
    evidence: str               # file:line proving the anchoring
    approx_size: str
    mechanism: str              # how the product relocates it
    notes: str = ""
    contains_tracked_files: tuple[str, ...] = field(default_factory=tuple)


#: Every relocatable store, verified. `contains_tracked_files` is the trap: a junction
#: over such a directory hides files that ship with the release and that the code READS.
STORES: tuple[Store, ...] = (
    Store(
        key="pricemodeling_data",
        anchor=Anchor.FILE,
        upstream_path="data/",
        evidence="pricemodeling/config.py:15 (PROJECT_ROOT=parents[1])"
                 " + config/settings.yaml data_dir='data'",
        approx_size="26 GB (db 16.5 - raw 4.9 - era5 4.1 - cmip6 0.04 - lake 0.07)",
        mechanism="NTFS junction code/<tag>/data -> <data-root>/data",
        notes=(
            "No CLI or env override exists. settings.yaml would accept an ABSOLUTE data_dir "
            "(pathlib absolute-join), but that means editing a file inside the release tree, "
            "which the immutability manifest forbids. Junction is the sanctioned mechanism. "
            "ORDERING TRAP: load_settings() calls ensure_dirs() (config.py:151), so ANY upstream "
            "invocation creates data/ and data/raw/. mklink /J fails when the target exists, so "
            "the junction must be created before the first invocation."
        ),
    ),
    Store(
        key="weathergen_output",
        anchor=Anchor.CONFIG,
        upstream_path="weathergen/output/",
        evidence="weathergen/weathergen/config.py:44-48 (path.parent / models_dir)",
        approx_size="1.3 GB (simulation.nc)",
        mechanism="junction, or absolute path in a run-scoped config overlay",
    ),
    Store(
        key="model_models_dirs",
        anchor=Anchor.CONFIG,
        upstream_path="{weathergen,demand_model,res_model,availability_model,dispatch_model}/models/",
        evidence="each package config.py models_dir property (path.parent / run.models_dir)",
        approx_size="varies; holds fitted objects incl. cmip6_deltas_*.npz",
        mechanism="junction, or absolute path in a run-scoped config overlay",
        notes="weathergen fit looks for cmip6_deltas_<ssp>_<year>_<model>.npz HERE (weathergen/cli.py:101).",
    ),
    Store(
        key="model_output_dirs",
        anchor=Anchor.CONFIG,
        upstream_path="{demand_model,res_model,availability_model,dispatch_model}/output/",
        evidence="each package config.py output_dir property",
        approx_size="varies",
        mechanism="run-scoped config overlay -> runs/<run-id>/output",
    ),
    Store(
        key="dispatch_reports",
        anchor=Anchor.CONFIG,
        upstream_path="dispatch_model/reports/",
        evidence="dispatch_model/dispatch_model/config.py:73-75; markup.py:293,300",
        approx_size="61 MB + projection_20y/ + mc/ trees",
        mechanism="DO NOT JUNCTION - seed a shared per-install store from the release,"
                  " then point reports_dir at it",
        notes=(
            "HAZARD: markup_model.json is a TRACKED file that ships in the release and is an INPUT "
            "to every projected year (markup.py:300 load_model). Junctioning reports/ - or rewriting "
            "reports_dir to an empty run dir - hides it, and apply_markup falls back silently to "
            "clipped SMC (markup.py:285). Upstream documents this exact hazard in .gitignore. The "
            "install MUST copy tracked report files into the shared store and verify their hashes "
            "before any projection run."
        ),
        contains_tracked_files=("dispatch_model/reports/markup_model.json",),
    ),
    Store(
        key="availability_reports",
        anchor=Anchor.CONFIG,
        upstream_path="availability_model/reports/",
        evidence="availability_model/availability_model/config.py:39-40",
        approx_size="small",
        mechanism="seed from release like dispatch_reports",
        contains_tracked_files=("availability_model/reports/methodology.md",),
    ),
    Store(
        key="res_era5_cache",
        anchor=Anchor.CONFIG,
        upstream_path="res_model/era5_cache/",
        evidence="res_model/res_model/io/era5.py:24-27 + res_model/config.yaml:55 cache_dir='era5_cache'",
        approx_size="raw ARCO/CDS download zips",
        mechanism="junction or overlay",
        notes="NOT listed in the program document. Gitignored upstream (**/era5_cache/).",
    ),
    Store(
        key="mc_scratchpad",
        anchor=Anchor.CWD,
        upstream_path="dispatch_model/scratchpad/mc/",
        evidence="dispatch_model/scripts/run_montecarlo.py:63 cube = Path('scratchpad/mc')/...",
        approx_size="1.5 GB on the owner machine; 472 MB per retained draw cube",
        mechanism="junction dispatch_model/scratchpad -> <data-root>/scratchpad",
        notes=(
            "NOT listed in the program document. The relative path is resolved against the PROCESS "
            "cwd, while the POWERSIM_WEATHER_CUBE env var the same script exports is built as "
            "(scripts/..).resolve()/scratchpad/mc/... - the two agree ONLY when cwd is "
            "code/<tag>/dispatch_model. That cwd is therefore mandatory for run_montecarlo.py. "
            "With --keep-cubes this grows by 472 MB per draw."
        ),
    ),
    Store(
        key="registry_raw_downloads",
        anchor=Anchor.CWD,
        upstream_path="data/raw/{odre,opsd,repd}/",
        evidence="pricemodeling/registries/{odre,opsd,repd}.py DEFAULT_RAW = Path('data/raw/...')",
        approx_size="small",
        mechanism="run the registry wrappers with cwd=code/<tag> so data/ resolves via the junction",
        notes="DEFAULT_RAW is cwd-relative, not PROJECT_ROOT-relative -"
              " unlike every other pricemodeling path.",
    ),
    Store(
        key="mastr_bulk",
        anchor=Anchor.HOME,
        upstream_path="~/.open-MaStR/",
        evidence="pricemodeling/registries/mastr.py:32 DEFAULT_DB = ~/.open-MaStR/data/sqlite/open-mastr.db",
        approx_size="7.5 GB measured on the owner machine (program document said ~3 GB)",
        mechanism="none - os.path.expanduser, not relocatable",
        notes="Counts toward the disk budget and MUST be offered separately by the uninstaller.",
    ),
    Store(
        key="powersim_lake",
        anchor=Anchor.ENV,
        upstream_path="data/lake/ and data/powersim.duckdb",
        evidence=".env.example; POWERSIM_ROOT / POWERSIM_LAKE / POWERSIM_DUCKDB",
        approx_size="68 MB + duckdb",
        mechanism="env vars (cleanest of all the stores) - also reached via the data junction",
    ),
    Store(
        key="weather_cube",
        anchor=Anchor.ENV,
        upstream_path="per-draw cube",
        evidence="POWERSIM_WEATHER_CUBE, set by run_montecarlo.py per draw",
        approx_size="472 MB per cube",
        mechanism="env var, set per draw by upstream itself",
    ),
)


#: Environment variables upstream reads. The job engine builds an EXPLICIT environment:
#: a stray DISPATCH_* inherited from the user's shell silently changes model results.
CREDENTIAL_ENV = ("RTE_CLIENT_ID", "RTE_CLIENT_SECRET", "ENTSOE_TOKEN", "CDSAPI_URL", "CDSAPI_KEY")

PATH_ENV = ("POWERSIM_ROOT", "POWERSIM_LAKE", "POWERSIM_DUCKDB", "POWERSIM_WEATHER_CUBE")

PROGRESS_ENV = ("POWERSIM_NO_PROGRESS",)   # must stay UNSET: it is what makes progress parseable

#: Behaviour switches that change results. Recorded in every run manifest; cleared unless set on purpose.
DISPATCH_ENV = (
    "DISPATCH_TRACE_SOLVES", "DISPATCH_ZONE_AVAIL", "DISPATCH_UNCLASSIFIED_MUSTRUN",
    "DISPATCH_UNCLASSIFIED_GEN", "DISPATCH_NO_ZERO_RES_BID", "DISPATCH_MONTHLY_AVAIL",
    "DISPATCH_LIGNITE_FUEL", "DISPATCH_JOINT_EXPORT", "DISPATCH_FLEX_VOM", "DISPATCH_DUMP_LP",
    "DISPATCH_CH_ROR", "DISPATCH_CAPTURE_DISPATCH", "DISPATCH_AVAIL_WDRAW", "DISPATCH_AREA_CAPACITY",
)
