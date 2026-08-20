# Phase 0 — addendum: decisions taken, and the CMIP6 guard

**Date:** 2026-08-20, after owner review of [phase-0.md](phase-0.md).

## Decisions recorded

| Item | Decision |
|---|---|
| `reports/` seeding fix (contradiction 1) | **Approved.** `reports/` is never junctioned; the installer seeds the shared store from the release per tag and hash-verifies it. |
| Registry sequence (wishlist §2) | **Approved** — owner will confirm the exact `download() → build() → registry.write()` composition; `drivers/wrappers/registries.py` will match it. |
| **D5 — GUI language** | **English.** Wizard, Studio, docs. Upstream's French runtime output is shown verbatim in the log pane; error and warning **cards** are written in English by `drivers/inventory.py`, keyed off the markers declared per job. |
| P1 — cut a tag | Still open. Blocks the Phase 1 gate. |

## The CMIP6 guard

Owner ask: *"Make sure CMIP6 deltas are fetched properly in the install to avoid returning 0."*

That is exactly the right worry — the degradation is a **zero climate adjustment on a cube that
claims to be trended** (`Trend.apply` returns the cube unchanged when `deltas` is empty). Three
things now stand in the way.

### 1. The prerequisite is enforced by us, at the right node

`drivers/inventory.py`:

- `weathergen-simulate` gains `requires=("weathergen-fit",)` and
  `preflight=("cmip6-deltas-present",)`;
- `weathergen-fit` keeps its CDS credentials (the first fit pulls ~4 GB of ERA5 lazily) but
  carries **no** trend prerequisite, because it has none;
- `montecarlo` carries the same preflight — every draw regenerates a cube through weathergen;
- the `[trend] enabled but deltas not found` line is registered as a **failure** marker on
  `simulate`, not a warning. After the preflight it must never appear; if it does, the cube is
  untrended and unusable, so the job fails rather than completing "successfully".

### 2. The check knows which file upstream will actually look for

`drivers/preflight.py::check_cmip6_deltas` reproduces `cli.py:100-101` exactly:

```
<models_dir>/cmip6_deltas_{ssp}_{target_year}_{model}.npz
```

with three details that are easy to get wrong:

- **`models_dir` is config-anchored**, relative to the config *file's* directory.
- **`model` defaults to `mpi_esm1_2_lr`**, not the `ec_earth3` the `--model` help still
  advertises (EC-Earth3 has broken roocs subsetting on CDS). The filename embeds it, so the
  stale value looks for a file nothing will ever write.
- **`ssp` and `target_year` are simulate-time inputs**, overridable per run. There is no single
  install-time artifact: each scenario/horizon pair needs its own npz. The check takes the
  effective values, and when the file is missing it returns the fixing job *and its arguments*,
  so the GUI can offer one-click "fetch the deltas for ssp585 @ 2040".

An explicit `trend.cmip6_deltas_path` in the config wins over the derived name, matching
upstream, and is itself verified to exist.

When the trend is **off** the check passes trivially — an untrended run is honest, and it is
what upstream's shipped config does.

### 3. The install fetches the default set

The wizard runs `weathergen-cmip6` for the config default (`ssp245`, 2050) during the model
stage, so the trend toggle works out of the box. It needs CDS credentials and accepted dataset
licences; if the user deferred those, the fetch is deferred with them and the Studio shows the
trend as unavailable until it has run — never as available-but-empty.

**Note for the modelling decision, which is yours, not mine:** the installer does **not** flip
`trend.enabled`. Upstream ships it `false`, and turning on a climate trend changes results, so
that stays an explicit user (or owner-default) choice surfaced in the GUI. Tell me if you want
the shipped default to be trend-on for the 2050 projections and I will change the default
overlay — the deltas will already be there either way.

## Verification

67 tests pass, ruff clean, path guard clean, registry self-consistent.

New in `tests/test_preflight.py`: the derived filename matches upstream's, the stale model name
cannot creep back, a scenario override invalidates the default deltas, an explicit config path
is honoured and config-anchored, the guard is attached to `simulate` and **not** to `fit`, and
the missing-deltas marker is a failure rather than a warning.

`validate_registry()` runs on every `inventory.py` invocation and in CI: a `requires` pointing
at a renamed job, or a `preflight` naming a check nobody implemented, would otherwise fail open
— the job would run unguarded, which is the precise failure mode these fields exist to prevent.
