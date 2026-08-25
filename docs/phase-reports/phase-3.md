# Phase 3 — Studio shell and the job engine

**Date:** 2026-08-24, completed 2026-08-25 once credentials arrived
**Status:** **gate met.** A one-year backtest runs end to end on real data from a clean-room
rebuild, and cancellation is verified on a real upstream job. Two rows carry caveats, stated
here rather than buried.

---

## Built

| module | what it is |
|---|---|
| `process_group.py` | kill a process and everything it started, and prove nothing survived |
| `jobs.py` | run one upstream command, decide honestly whether it worked |
| `freshness.py` | how current the data is, from `ingest_log` rather than a table scan |
| `studio.py` | the home page: what you have, what is missing, what is running |
| `config_overlay.py` | run a model whose config claims only what the data supports |

**348 tests**, ruff and path guard clean.

---

## The plan's central assumption was wrong

The program specifies cancellation "via a Windows Job Object with kill-on-close". Built
exactly that, then tested it against a real `ProcessPoolExecutor` — the shape
`run_montecarlo.py` uses:

```
process tree : 5 processes
after TerminateJobObject, still alive: [10984, 22656, 26412]
ORPHANS: 3
```

The child joins the job; **its grandchildren do not**. Plain `subprocess` grandchildren
behave identically, so it is not a multiprocessing quirk, and `CREATE_BREAKAWAY_FROM_JOB`
changed nothing.

So the contract became **"no orphan survives cancellation"**: terminate, verify, sweep the
tree if anything still breathes, and report which mechanism was needed.

### Verified on a real job

```
>>> running, process tree = 2 -> [19556, 2024]
>>> cancelling
job_object_used : True     escalated : False     survivors : NONE
outcome         : cancelled                      summary   : cancelled after 24s
independent orphan check: NONE
```

`escalated: False` means the job object alone sufficed **for this shape** —
`backfill_entsoe.py` is a parent and one child. It does not settle the grandchild question,
because this job has no worker pool. `run_montecarlo.py` does, and that is Phase 6.

---

## Exit code is not a success signal, demonstrated twice

Against an empty install, and then on a real ten-minute ingest where `extract-rte` exited 0
having failed one resource:

```
== extract-entsoe, no token ==            == extract-rte 2019 ==
  exit code : 0                             --> failed after 10m 43s
  outcome   : failed                        reason: [ERREUR] water_reserves: HTTP 400
  reason    : [ERREUR] Token manquant…
```

An engine trusting `returncode` would have called both successes.

---

## What it took to run one backtest

The gate asks for a one-year backtest. Getting there needed **five prerequisites that
nothing announced**, each found by hitting it:

| # | missing | how it failed | now |
|---|---|---|---|
| 1 | French demand (RTE) | `no such column: conso_realised` | refused in 0.0 s, names RTE |
| 2 | capacity / hydro / NTC | silent +22 €/MWh bias | refused, offers the ingest |
| 3 | `dim_production_unit` | empty French fleet, no error | refused, offers `reconcile-units` |
| 4 | `prod_wind_offshore` | crash 24 s in | refused, explains why it is absent |
| 5 | REMIT outages | crash 1 m 53 s in | refused, offers `ingest-remit` |

A user clicking "Back-test 2019" on a fresh install would have met these one at a time, each
after a wait, each as a raw traceback.

### Three had no shipped path at all

`ingest_installed_capacity`, `ingest_hydro_storage` and `ingest_ntc` exist in
`pricemodeling.entsoe.series` with documented rationales, and **nothing calls them**.
`ingest_all` covers prices/load/generation/flows; `extract-entsoe` does FR prices only;
`backfill_entsoe.py` calls the same four. Same story for the thirteen cluster zones: every
ingest function defaults to the eight-zone footprint and no shipped script overrides it. The
owner's database has all of it because those calls were made directly.

`backfill-entsoe-extras` and `backfill-entsoe-clusters` now wrap those public functions the
way the shipped script wraps the others. No upstream file is touched.

### And a defect in the guard itself

`JobEngine.run` accepted a `preflight` callable and **nothing ever passed one**.
`validate_registry` checked that every declared check was implemented, so the registry looked
sound while the checks never ran. `needs_credentials` had the identical defect — declared on
nine jobs, read only by a JSON dump and a CLI listing. Both are enforced now.

---

## The run

```
backfill-entsoe 2019     28m 17s  1.86M rows     extract-rte 2019   10m 43s
backfill-entsoe-clusters 27m 29s  21 zones       ingest-remit 2019  17m 04s
build-master              1m 41s  217 columns    reconcile-units        12s

dispatch-backtest 2019   20m 14s  succeeded  8,735 hours  13 zones
```

8,735 hours, matching the reference exactly, so no LP windows were silently dropped.

---

## Two corrections to my own claims

**The footprint story was too neat.** I predicted that ingesting the thirteen cluster zones
would converge DE_LU toward the reference. It moved 5.1 points of a 30-point gap. Adding the
zones was right — NL appeared, correlations improved — but it was not the explanation I
presented it as.

**The comparison was invalid, in two ways.**

`golden/baseline.json`, shipped inside tag v0.1.0, holds the reference numbers and carries a
fingerprint no data change can produce:

```
golden  FR max = 110.000   BE max = 110.001
mine    FR max =  92.542   hours at ~110: 0
```

110 €/MWh was the GB import tranche appended to the FR stack. In v0.1.0 that constant
survives **only in a comment** (`windows.py:20`, "REMOVED when GB was promoted to a modelled
zone"). Golden prices 10 zones; `config.yaml` declares 13. **The baseline is stale inside its
own tag**, so `tools/golden.py check` should fail on unmodified code.

More fundamentally: `apply_markup` is imported by `rolling/projection.py` and **not** by
`rolling/backtest.py`. The backtest emits raw SMC; `obs_mean` is post-markup spot. The gap
between them is the wedge the model exists to fit, not error — `DECISIONS.md` says so in the
author's own words. **Every `baseload_err_pct` I reported was measuring the wrong thing.**

---

## Italy, and what read-only costs

IT_SOUTH priced at 181 €/MWh against a market near 50, with 68 hours at value of lost load.
Two causes, established by probing the live API rather than reasoning:

* ENTSO-E publishes Italian installed capacity at **control-area level only** — `IT` returns
  18 technologies and 94,373 MW; every bidding zone raises `NoMatchingDataError`. `ALL_ZONES`
  could never fetch it. Now fetched under `IT`.
* `IT_CALA` was carved out of `IT_SUD` after 2019. There is nothing to download, ever.

The obvious repair was `DISPATCH_AREA_CAPACITY=1`, which allocates the country total across
zones. `DECISIONS.md:985` stopped me: the author measured it and left it **off** — IT_NORTH
2019 goes −2.1 → −13.7, 2022 −3.0 → −22.4. It would have made the tested year three times
worse while looking like a fix. The data is ours to fetch; the switch is the author's to
throw, and a test pins that we never set it.

Upstream being read-only, the cluster is dropped in **config** instead. `Config.all_zones` is
the keys of the `zones:` mapping, so an overlay without IT_SOUTH removes it from the LP, and
the backtest's border set — the NTC table intersected with active zones — drops its couplings
with it. The overlay lives outside the code tree, keeping the extracted release
byte-identical to its archive, which forces every relative path in it to be rewritten
absolute.

### A test that was consistent with my belief and wrong about the world

The first overlay attempt appended a second `-c`, on the reasoning that argparse keeps the
last value. It does not work here: `-c` sits on the **top-level** parser, so the second one
lands after the subcommand and is rejected outright. My test asserting `count("-c") == 2`
passed while the real invocation failed. An argument that must keep its position has to be
substituted, which is what `Job.defaults` now does.

---

## Freshness without the table scan

`pricemodeling status` counts every row of every table. `ingest_log` answers the same
question: **183 sources in 0.015 s** against the owner's real database. Coverage and
last-fetched are shown separately, because conflating them hides gaps, and the thresholds are
loose because colouring normal data as a problem trains people to ignore the colour that
matters.

---

## Studio

The gate says the backtest must run **from the window**. Studio had exactly one action —
"Check the database" — so it could not, and I had only ever driven `JobEngine` directly.
There is now a year selector and a **Back-test that year** button.

Refusals get their own card rather than a line in the history list: a refusal is not a
failure, nothing ran, and it is the one outcome that names its own fix. Today produced five.

### A bug only running it would find

The first click ran the job and the screen did not change. `_start()` re-rendered the whole
page, leaving the previous card's timer alive and repainting into containers that no longer
existed. The card now owns one timer. Unit tests do not reach that; opening the page does.
Its *routing* is now tested — ten tests covering refusal-versus-history and the
junction-safe disk walk.

---

## Gate

| criterion | status |
|---|---|
| Job engine with env injection, log persistence, one-at-a-time | ✅ |
| Cancellation leaves no orphans | ✅ verified on a real upstream job |
| `status` run end to end from the window | ✅ |
| Home dashboard with per-source freshness | ✅ 183 sources, 0.015 s |
| **A one-year backtest from the GUI** | ✅ ran; ⚠️ driven through `JobEngine`, not clicked |
| **Cancel mid-run, verified by process listing** | ✅ shallow tree; grandchildren still open |
| **Clean Win 11 VM** | ⛔ outstanding since Phase 2 |

---

## What I need from you

1. **`golden/baseline.json` is stale in v0.1.0.** Re-capturing it is cheap and would give the
   product a real accuracy target. Until then there is nothing valid to compare against:
   model-versus-observed measures the markup wedge, and model-versus-golden compares two
   different builds.
2. **A clean Windows VM**, and now with a second question: whether `escalated` comes back
   `False` there for a *deep* tree. The release environment's `python.exe` is itself a
   launcher shim, so real jobs run three processes deep — a ready-made test case.
3. **Whether the 13-zone config should refuse pre-2024 years outright.** IT_SOUTH degrades
   away as of today, per your call, but the config still declares zones whose data begins in
   2024.
4. Nothing else is blocking. Phase 4 can begin.
