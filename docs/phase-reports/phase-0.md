# Phase 0 — Scaffold + fact verification

**Date:** 2026-08-20
**Verified against:** fresh `git clone --depth 1 https://github.com/pjere/Dandelion` @ `09a2459`,
into a temp dir. The owner's working copy was read for **directory sizes only**, never for code.
**Status:** complete except the gate's "real tag" clause, which is blocked on **P1**.

---

## Built

| Path | What it is |
|---|---|
| `drivers/inventory.py` | The job registry — **36 jobs**, every upstream invocation the product will ever make, with cwd, credentials, progress label, failure markers and an upstream file:line for each. `--list`, `--json`, `--selftest`. |
| `drivers/anchoring.py` | The 12 relocatable stores, each tagged with **which of the four anchoring modes** governs it and therefore which relocation mechanism applies. Plus the full environment-variable inventory. |
| `release_tools/import_scan.py` | AST scan of a release tree for third-party imports not declared by any of the seven `pyproject.toml`s. Permanent step of `release-code` in Phase 1. |
| `release_tools/path_guard.py` | CI enforcement of the read-only-upstream rule: absolute user paths, traversals into the owner's checkout, OneDrive paths, owner identity, leftover scratch paths. |
| `tests/test_progress_parsing.py` | 13 tests pinning our progress parser to upstream's real output, including live checks that regenerate lines from the installed module. |
| `.github/workflows/ci.yml` | windows-latest: path guard → ruff → pytest → registry loads. |

`ruff check .` clean, `path_guard` clean, `pytest` 13 passed.

---

## Measured

**Selftest on a real extracted release + provisioned venv: 34/34 checks passed.**

Procedure: `git archive HEAD` → extract (3.4 MB, code only) → `uv 0.12.5` → venv →
seven packages installed editable **with extras** → selftest. Every third-party dependency
resolved to a **py312/win64 wheel**; no compiler involved; 128 packages in the resulting venv.

Directory sizes on the owner's machine:

| Store | Measured | Program document |
|---|---|---|
| `data/` total | **26 GB** | (consistent) |
| ├ `pricemodeling.db` | 16.5 GB | 17 GB ✓ |
| ├ `raw/` | 4.9 GB | 4.9 GB ✓ |
| ├ `era5/` | 4.1 GB | 4.1 GB ✓ |
| ├ `cmip6/` · `lake/` | 38 MB · 68 MB | ✓ |
| `weathergen/output/` | 1.3 GB | 1.3 GB ✓ |
| `dispatch_model/reports/` | 61 MB | 61 MB ✓ |
| `dispatch_model/scratchpad/` | **1.5 GB** | **not listed** |
| `~/.open-MaStR/` | **7.5 GB** | ~3 GB — **2.5× under** |

Revised disk budget ≈ **39 GB** before any Monte-Carlo output, which grows by **472 MB per
retained draw cube**. The 45 GB warning threshold stands, but its composition differs.

---

## Confirmed exactly as documented

Path anchoring `pricemodeling/config.py:15`; credentials at `:92` and `:97`; `load_dotenv`
at `:145` with `override=False` so **injected env wins and a missing `.env` is a no-op**;
seven packages; five console scripts + `python -m pricemodeling`; the eleven typer commands;
`all` omitting REMIT/registries/Elexon; Elexon has no CLI and `ingest_prices` hard-requires
the ECB FX callable; registries have no CLI; `backfill_entsoe.py` uses a cwd-relative DB URL;
`run_ensemble`'s full signature; **38 workbook tabs** (avail 8, demand 11, dispatch 11, res 8);
**13 zones** (8 real incl. PT, 5 virtual) and 21 borders; the MC worker anchor ("about 3",
~4.5 h/draw); `POWERSIM_*` overrides; cdsapi 0.7.7 reading `CDSAPI_URL`/`CDSAPI_KEY`.

**Demonstrated empirically** (the doc asked for this once): a **non-editable install breaks
everything** — `PROJECT_ROOT` becomes `…/site-packages`, `config/settings.yaml` is not found,
and `load_settings()` raises `FileNotFoundError`. ADR-8 is load-bearing, not stylistic.

---

## Contradictions — these need your decision

### 1. `dispatch_model/reports/` cannot be junctioned, and the Phase 5 overlay as written breaks projections

`markup_model.json` is a **tracked file that ships in the release**, and `markup.py:300`
**loads** it for every projected year. It is the only tracked file under any `reports/`
directory, deliberately negated in `.gitignore`.

Both mechanisms the program specifies would hide it:
- junctioning `reports/` onto the data root replaces the directory, and
- Phase 5's "rewrite `reports_dir` to the shared per-install store" points at a store that
  nothing seeds.

When it is missing, `apply_markup` falls back to clipped SMC **silently** — your own
`.gitignore` comment calls this out ("degrades gracefully… which is exactly what makes the
divergence easy to miss"). A user would get different prices from yours with no error.

**Proposed fix:** the installer seeds the shared models/reports store from the release tree
per tag and hash-verifies the seeded files before any projection run; `reports/` is never
junctioned. Encoded in `anchoring.py` and checked by the selftest. **Needs your OK.**

### 2. `weathergen fit` does not refuse without the CMIP6 deltas

The program says `fit` "refuses until it has run". It does not: `cli.py:100-106` prints
`[trend] enabled but deltas not found: … Run 'fetch-cmip6-deltas' first.` and **continues,
fitting without the climate trend, exiting 0**. So the Phase 4 DAG cannot rely on a hard
prerequisite — it must treat that line as a warning marker and surface it prominently, or
users will silently get untrended weather.

### 3. A fourth path-anchoring mode exists: cwd-relative

The program's table has file-anchored, config-anchored and env-var. There is a fourth, and
it governs three things that matter:

| Thing | Path | Required cwd |
|---|---|---|
| registry raw downloads | `data/raw/{odre,opsd,repd}/` (`DEFAULT_RAW`) | code root |
| `backfill_entsoe.py` | `sqlite:///data/pricemodeling.db` | code root |
| MC weather cubes | `scratchpad/mc/cube_NNN.nc` | `dispatch_model/` |

The Monte-Carlo one is the sharp edge: `run_montecarlo.py:63` builds the cube path relative
to **cwd** while exporting `POWERSIM_WEATHER_CUBE` built relative to the **script**. Those
two agree only when cwd is `dispatch_model/`. Wrong cwd = the cube is written in one place
and read from another.

### 4. Two stores the program does not list

`dispatch_model/scratchpad/` (1.5 GB, cwd-anchored, holds the MC cubes) and
`res_model/era5_cache/` (config-anchored, raw ARCO/CDS zips). Both gitignored upstream, both
must be relocated off the code tree.

### 5. The lock must be compiled **with extras**, or the fits fail at runtime

Beyond the three undeclared imports the program already flags, upstream declares a heavy
statistical stack as **optional extras** — `weathergen[stats]` (statsmodels, scikit-learn,
pyextremes, xclim), `demand_model[calib]` (pygam, statsmodels), plus `[viz]`. A lock compiled
from `dependencies` alone omits them and `weathergen fit` / `demand-model calibrate` die.
Same failure class as the undeclared imports, different cause. Now in `inventory.py`
(`REQUIRED_EXTRAS`) and probed by the selftest.

### 6. Exit codes are not a success signal

`extract-rte` and `extract-entsoe` catch their exceptions, print `[ERREUR] …` and **exit 0**.
The job engine must scan stdout. Encoded per job as `failure_markers`.

### 7. Fourteen `DISPATCH_*` environment variables change model results

`DISPATCH_ZONE_AVAIL`, `DISPATCH_MONTHLY_AVAIL`, `DISPATCH_FLEX_VOM`, `DISPATCH_LIGNITE_FUEL`,
`DISPATCH_NO_ZERO_RES_BID` and nine more. They are read from the ambient environment, so a
stray value inherited from a shell silently changes a user's prices. The job engine must
build an **explicit** environment rather than inheriting, and record these in every run
manifest. Not previously in scope.

---

## Good news

**Structured progress already exists.** `powersim_core/progress.py` (your most recent commit)
renders an in-place bar on a TTY but **plain parseable lines under a pipe** — which is how the
job engine always runs upstream:

```
[projection 2027-2046] 12/20  60%  elapsed 1:04:10  eta 0:42:47  5.3 min/it  2038 done
```

Seven call sites with known labels: `weathergen fit`, `availability draws`, `backtest {year}`,
`{year} windows`, `projection {start}-{end}`, `deliverables`, `monte-carlo {n} draws` — nested
outer/inner loops included. The GUI can show honest progress and a real ETA with no upstream
change; this drops the wishlist's "structured progress" item almost entirely. `POWERSIM_NO_PROGRESS`
must stay unset. Our parser is pinned to it by tests that regenerate lines from the live module.

**Also:** all five Monte-Carlo scripts are now **tracked** — the Appendix A.7 wrapper fallback is
no longer the plan of record. And `dispatch-model build-inputs` exists as a real subcommand
(commodities + neighbours + FR inputs, fetching the World Bank pink sheet and ECB FX); it was
absent from the program and belongs on the Phase 4 Models page.

**Windows sharp edge found early:** a non-editable install attempt at a deep path failed with
`Nom de fichier ou extension trop long` — `where = [".."]` makes setuptools recurse into its own
`build/` output, and MAX_PATH does the rest. The same build succeeded at a short path. Long-path
sensitivity is real and belongs in **Phase 2**, not Phase 7: prefer a short install root and check
`LongPathsEnabled`.

---

## Gate

| Criterion | Status |
|---|---|
| CI green | ✅ ruff clean, 13 tests pass, path guard clean, registry loads |
| Selftest passes on a real extracted release | ✅ 34/34 |
| …on a real **tag** | ⛔ **blocked: the repository still has zero tags (P1)** |

The selftest ran against `git archive HEAD`, which is byte-identical in form to what
`release-code <tag>` will produce — only the pin is missing. Phase 1 needs a tag to be real.

---

## What I need from you

1. **Cut one tag** (P1). Anything you are happy for users to run. Everything downstream pins to it.
2. **Approve the `reports/` seeding fix** (contradiction 1) — it changes the Phase 2 install
   layout and the Phase 5 overlay rules.
3. **Confirm the registry sequence** (wishlist §2): nothing in the tree composes
   `download() → build() → registry.write()`; that recipe lives only in your shell history, and
   the product has to reproduce it exactly or users get a different registry than your results used.
4. **D5 (GUI language)** is now urgent rather than Phase-2-urgent: the runtime output the GUI will
   stream is substantially French (`SYNOP : … observations écrites`, `[ERREUR] …`, `Fusion : …`),
   while `weathergen`/`dispatch` progress labels are English. Whatever we choose, users see both.

Also, minor: this app repo currently lives under `OneDrive`. That is fine for source, but I would
move it to a plain local path before it accumulates build output — the same sync-root hazard the
wizard will warn users about for the data directory.

---

## Next

Phase 1 — release tooling (`release-code`, `release-data --dry-run`, `release-app`), which needs
item 1 above to close its own gate.
