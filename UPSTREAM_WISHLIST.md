# Upstream wishlist

Changes to `pjere/Dandelion` that would make this product simpler. **None of them are
required** — the product works around every item here, and that is the point: the research
repo keeps moving without ever having to care about the app. This file exists so that when
the owner is editing a file for their own reasons anyway, they can see what would help.

Ranked by how much work the workaround costs us, most first.

---

### 1. Declare the three lazily-imported dependencies

`cdsapi`, `entsoe-py` and `open-mastr` are imported inside functions and declared in no
`pyproject.toml` and in no `requirements.txt`. Verified by AST scan of the whole tree
(`release_tools/import_scan.py`).

| import | distribution | site |
|---|---|---|
| `cdsapi` | `cdsapi` | `weathergen/era5_cds.py`, `era5_arco.py`, `cmip6_cds.py`, `res_model/io/era5.py` |
| `entsoe` | `entsoe-py` | `pricemodeling/entsoe/series.py:68` |
| `open_mastr` | `open-mastr` | `pricemodeling/registries/mastr.py:73` |

The offline test suite passes without them; real ingestion dies at first use. We pin them
in a `drivers/`-maintained supplement to the lock and probe them in `inventory --selftest`,
so this costs us little now — but it is the item most likely to bite a future release
silently, because nothing upstream fails when a new lazy import is added.

**Ask:** add them to the relevant `dependencies` (or an `extras` group), so
`uv pip compile` sees them.

---

### 2. An orchestration entry point for the plant registries

`pricemodeling/registries/{mastr,odre,opsd,repd}.py` expose `download()` / `build()`, and
`powersim_core.registry.write(df, source)` writes a source's slice to the lake — but
**nothing in the tree composes them**. `registry.write` is called only from tests. So the
sequence that actually produced the registry layer in the shipped lake exists only in the
owner's shell history.

We have to author that composition in `drivers/wrappers/registries.py` and guess at it.
If our guess differs from what the owner ran, the product silently builds a different
registry than the research results were computed on.

**Ask (small):** confirm the exact sequence, so our wrapper matches. **Ask (better):** a
`python -m pricemodeling ingest-registry <source>` command, which would delete the wrapper.

---

### 3. Commands that fail should exit non-zero

`extract-rte` and `extract-entsoe` catch their own exceptions, print `[ERREUR] …` and
**still exit 0** (`pipeline.py:120-123`, `:152-153`). `weathergen fit` prints
`[trend] enabled but deltas not found: … Run 'fetch-cmip6-deltas' first.` and then fits
**without the climate trend**, also exiting 0 (`cli.py:100-106`).

Exit status is therefore not a usable success signal, so the job engine has to scan stdout
for French error markers to decide whether a job worked. That coupling is fragile: a
reworded message becomes a silently-passing failure in the GUI.

**Ask:** `raise typer.Exit(code=1)` after the error echo, and make the missing-deltas case
either fail or be an explicit `--no-trend` choice.

---

### 4. `markup_model.json` deserves to be an input, not a report

`dispatch_model/reports/markup_model.json` is tracked (negated in `.gitignore`) because
`markup.py:300` **loads** it for every projected year, while everything else in `reports/`
is output. When it is absent `apply_markup` falls back to clipped SMC silently — a hazard
the `.gitignore` comment itself documents.

That single file is why we cannot simply relocate `reports/` off the code tree: any
redirection of `reports_dir` hides it. We handle it by seeding and hash-checking the file
into the relocated store, per install and per tag.

**Ask:** move fitted inputs to a directory that is not also an output sink — e.g.
`dispatch_model/models/markup_model.json` alongside the other fitted objects.

---

### 5. A machine-readable status

`python -m pricemodeling status` prints `Base : <path>` and then `SELECT COUNT(*)` for
**every** table. On the 16.5 GB master that is a full scan, so it cannot back a dashboard,
and its output is prose we would have to parse anyway.

We instead query the SQLite file read-only for per-source max timestamps and the
`ingest_log` table. That works and is fast, but it means the product depends on the table
schema rather than on a contract.

**Ask:** `status --json` emitting per-source row counts, min/max timestamps and last
ingest time — cheaply, from `ingest_log` rather than from counts.

---

### 6. Let the data root be configured without editing a tracked file

`PROJECT_ROOT = Path(__file__).resolve().parents[1]` (`config.py:15`) plus
`paths.data_dir: "data"` puts 26 GB inside the code tree, and no flag or environment
variable relocates it — unlike the lake, which has `POWERSIM_ROOT` / `POWERSIM_LAKE` /
`POWERSIM_DUCKDB`. We bridge with an NTFS junction, which works but adds an install step
with its own failure mode, and an ordering constraint (`load_settings()` calls
`ensure_dirs()`, so any invocation before the junction exists creates a real directory and
`mklink /J` then refuses).

**Ask:** honour a `PRICEMODELING_DATA_DIR` (or reuse `POWERSIM_ROOT`) in
`Settings.data_dir`. Two lines, and the junction for `data/` disappears.

---

### 7. Cut tags

The repository has **zero git tags**. Our whole release model is "a tag → a source archive
→ a lockfile", so until a tag exists there is nothing to pin. (The five Monte-Carlo scripts
that were untracked when this program was drafted are now committed — thank you; that part
is resolved.)

**Ask:** one annotated tag, any name, on a commit you are happy for users to run.

---

### 8. Smaller things

- **`.env.example` is stale on CDS.** It says the client reads `~/.cdsapirc`; cdsapi 0.7.7
  checks `CDSAPI_URL` / `CDSAPI_KEY` **first** (`get_url_key_verify`, api.py:48-53). We
  inject env vars and never write a file, which is what we want — but the file misleads.
- **`docs/INSTALL.md` omits real steps**: the Elexon/GB ingest, ECB FX, World Bank
  commodities, and any invocation for the registries. It also implies `all` is a complete
  refresh; `all` chains only meteo → rte → entsoe → reconcile → build-master.
- **`extract-entsoe` is France-only** despite a generic name; the other twelve zones come
  from `scripts/backfill_entsoe.py`. A renamed command, or a `--zones` flag, would remove a
  standing source of user confusion.
- **`run_montecarlo.py` requires `cwd=dispatch_model/`** because `scratchpad/mc/cube_NNN.nc`
  is cwd-relative while the `POWERSIM_WEATHER_CUBE` it exports is script-relative. Resolving
  the cube path against the script would remove the constraint.
