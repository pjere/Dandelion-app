# Phase 0 — addendum: decisions taken, and the CMIP6 guard

**Date:** 2026-08-20, after owner review of [phase-0.md](phase-0.md).

## Decisions recorded

| Item | Decision |
|---|---|
| `reports/` seeding fix (contradiction 1) | **Approved.** `reports/` is never junctioned; the installer seeds the shared store from the release per tag and hash-verifies it. |
| Registry sequence (wishlist §2) | **Approved** — owner will confirm the exact `download() → build() → registry.write()` composition; `drivers/wrappers/registries.py` will match it. |
| **D5 — GUI language** | **English.** Wizard, Studio, docs. Upstream's French runtime output is shown verbatim in the log pane; error and warning **cards** are written in English by `drivers/inventory.py`, keyed off the markers declared per job. |
| Climate trend | **ON by default**, SSP2-4.5, ramped 2020→2050. Set by a declared, hashed config patch (the Monte-Carlo path accepts no override). |
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

## Trend ON by default — and the one file the product must edit

**Owner decision, 2026-08-20: the climate trend ships enabled.** A twenty-year price path to
2050 computed on present-day climate would understate cooling demand and the wind/hydro shifts
throughout the horizon. `Trend.apply` ramps the deltas from `baseline_year` (2020) to
`target_year` (2050), so each simulated year gets its own fraction rather than a step change.

### Why this needs a config edit rather than a flag

`weathergen simulate` accepts `--trend`. The Monte-Carlo path does not go through the CLI:
`mc_weather.py:34` loads `<code_root>/weathergen/config.yaml` **by hardcoded path** and calls
`_build_trend(cfg, Namespace(ssp=None, target_year=None, trend=None))` — all three overrides
None, so the file's value is final. No flag, no environment variable, no `-c`.

Using the flag would therefore give a trended single simulation and an **untrended Monte-Carlo**
from the same install — precisely the silent divergence this product exists to prevent. The
config file is the only lever that reaches both.

### How the edit stays honest

`drivers/code_patches.py` makes this the single declared exception to code-tree immutability:

- **Line-surgical.** Verified against the real `weathergen/config.yaml`: exactly **one line**
  changes in 127, comment alignment preserved, every other byte identical, and the parsed YAML
  differs in exactly that one key. The decoy `enabled:` under `data.era5` (nine lines earlier)
  is untouched — the patcher is block-scoped, and a test asserts it.
- **Self-explaining.** The patched line reads
  `enabled: true                  # [dandelion] trend ON for 2050 projections; upstream default: false`
- **Declared and hashed.** The install manifest records the file, the block, the key, the old
  and new values, and the sha256 **before and after**. The integrity check accepts only the
  pristine hash or the declared patched hash, so any other edit to the tree is still caught.
- **Refuses on surprise.** If the key holds anything other than a value this product recognises
  — because upstream changed its shipped default, or someone hand-edited — the patch is skipped
  with a `REFUSED` note rather than overwriting.
- **Reversible.** The GUI toggle writes the same patch with the other value, so "off" is a
  declared state too, not drift. (The first cut of the guard made this a one-way door: the trend
  could be turned on but never off. There is now a regression test that toggles four times.)

### Knock-on effects

1. **CDS credentials are no longer optional** for anyone who wants projections. The deltas are
   now on the default path, and the preflight refuses to simulate without them. The wizard must
   present CDS as required, with an explicit "run untrended instead" escape that writes
   `trend_patch(False)` — so a user who cannot get a CDS account still gets a working, honestly
   labelled product.
2. **The install fetches `cmip6_deltas_ssp245_2050_mpi_esm1_2_lr.npz`** during the model stage.
3. **The GUI states the trend on the projection page and in results**, because it changes
   numbers: `Climate trend: ON — SSP245, ramped 2020 to 2050`.
4. **Run manifests record** trend enabled/ssp/target_year plus the deltas file hash.
5. No new dependency: QDM is implemented in pure numpy (`trend.py` D6.1); `xclim` is only a
   documented future drop-in.

### Scenario: SSP2-4.5, and it stays that way

**Owner decision, 2026-08-20: keep `ssp245` as the shipped headline scenario.** The GUI still
exposes the scenario picker, and each scenario/horizon pair fetches its own deltas on demand.

That value needs no patch — it is already what upstream ships. But *inheriting* a default is not
the same as *choosing* one, and this whole phase has been an exercise in finding places where the
two look identical until they diverge. If a future release edited `trend.ssp`, the product's
headline scenario would change with it, silently, and every projection would quietly answer a
different question.

So `code_patches.EXPECTED_VALUES` declares the three trend values the product deliberately
inherits — `ssp: "ssp245"`, `target_year: 2050`, `baseline_year: 2020` — and `check_expectations()`
verifies them at install. A mismatch is **reported for review, never overwritten**: upstream is
allowed to change its mind, but not without us noticing. Checked green against `09a2459`.

## Verification

90 tests pass, ruff clean, path guard clean, registry self-consistent.

New in `tests/test_code_patches.py`: one line changes and no other, the decoy stays put, the
patch is idempotent, it refuses an unrecognised value, verification catches post-install
tampering, and the toggle survives four round trips.

New in `tests/test_preflight.py`: the derived filename matches upstream's, the stale model name
cannot creep back, a scenario override invalidates the default deltas, an explicit config path
is honoured and config-anchored, the guard is attached to `simulate` and **not** to `fit`, and
the missing-deltas marker is a failure rather than a warning.

`validate_registry()` runs on every `inventory.py` invocation and in CI: a `requires` pointing
at a renamed job, or a `preflight` naming a check nobody implemented, would otherwise fail open
— the job would run unguarded, which is the precise failure mode these fields exist to prevent.
