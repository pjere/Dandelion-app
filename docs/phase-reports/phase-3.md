# Phase 3 — Studio shell and the job engine

**Date:** 2026-08-24
**Status:** built and exercised against a real installation. Updated 2026-08-24 after the
ENTSO-E token arrived. Cancellation is now verified on a real upstream job. **The backtest row
is still not met**, for a reason this report originally got wrong: it needs RTE credentials,
not the ENTSO-E token. See the correction below.

---

## Built

| module | what it is |
|---|---|
| `process_group.py` | kill a process and everything it started, and prove nothing survived |
| `jobs.py` | run one upstream command, decide honestly whether it worked |
| `freshness.py` | how current the data is, from `ingest_log` rather than a table scan |
| `studio.py` | the home page: what you have, what is missing, what is running |

4,509 lines of application code against 2,542 lines of tests. **288 tests**, ruff and path
guard clean, three commits.

---

## The plan's central assumption was wrong

The program specifies cancellation "via a Windows Job Object with kill-on-close". I built
exactly that, then tested it against a real `ProcessPoolExecutor` — the shape
`run_montecarlo.py` actually uses:

```
process tree    : 5 processes
after TerminateJobObject, still alive: [10984, 22656, 26412]
ORPHANS: 3
```

Three workers survived. Rather than patch around it, I measured why:

```
is THIS process already in a job? True
assign() returned  : True
child in OUR job   : True
grandchild ... in OUR job: False      <- the actual failure
```

The child joins the job; **its grandchildren do not**. Isolating further: plain `subprocess`
grandchildren behave identically, so it is not a multiprocessing quirk. The outer job here has
`BREAKAWAY_OK` set (`LimitFlags 0x00003000`), and explicitly requesting
`CREATE_BREAKAWAY_FROM_JOB` changed nothing.

**What this machine cannot settle** is whether that is peculiar to running inside another job —
a terminal or IDE that already sandboxes its children — or whether it would also affect a user
launching from Explorer. This shell *is* inside a job, so the clean case is unobservable here.

### So the contract changed

Not "the job object works" but **"no orphan survives cancellation"**: terminate the job, then
verify, then sweep the process tree if anything is still breathing, and report which mechanism
was needed.

```
ProcessPoolExecutor    tree= 5  job_object=True  escalated=True  survivors=0  CLEAN=True
nested subprocess      tree= 8  job_object=True  escalated=True  survivors=0  CLEAN=True
```

`TerminationResult.escalated` is the useful part: on a clean VM, if it comes back `False`, job
containment works on a normal machine and the tree sweep is pure insurance. The open question
becomes a value somebody reads rather than an argument.

---

## Exit code is not a success signal, demonstrated

The Phase 0 finding, now working behaviour. Run against the real installation with no token
stored:

```
== extract-entsoe, with no token ==
  exit code      : 0        <- upstream reports success
  engine outcome : failed   <- what the user is told
  reason         : [ERREUR] ENTSO-E: Token ENTSO-E manquant…
```

An engine trusting `returncode` would have reported a failed ingest as a success, and the user
would have discovered otherwise days later when a projection came back short.

Every job declares its own failure markers. `weathergen simulate`'s missing-deltas line is
among them — it is a **failure**, not a warning, because it produces a present-day cube
labelled as the target year. A test asserts it appears in `failure_markers` and not in
`warning_markers`.

Other things the engine encodes: progress reported only where a real
`powersim_core.progress` line exists (a counter line yields no percentage rather than an
invented one); logs scrubbed of stored credentials as they are written, because a log is the
first thing a stuck user forwards; console scripts resolved inside the release environment
rather than through PATH; one job at a time, because the stages share one database.

---

## Freshness without the table scan

`pricemodeling status` runs `SELECT COUNT(*)` over every table — a full scan of a 16.5 GB
master, and prose to parse afterwards. `ingest_log` already holds the answer.

Measured against the owner's real database:

```
described 183 sources, 98.0M rows in 0.015s

Weather observations  Météo-France SYNOP  33.3M rows  to 2026-01     fetched 1 months ago  [ageing]  (5 missing)
French detail         RTE                 31.2M rows  to 2027-01-01  fetched 1 months ago  [ageing]
Prices                ENTSO-E day-ahead    1.1M rows  to 2026-01-01  fetched 19 days ago   [fresh]
Generation            ENTSO-E             23.2M rows  to 2026-01-01  fetched 19 days ago   [fresh]
GB market             Elexon               975k rows  to 2026-07-30  fetched 17 days ago   [fresh]
```

Fast enough to redraw freely, and it surfaced a real signal on its first run: five missing
SYNOP chunks.

Two deliberate choices. Coverage (`to …`) is parsed from the chunk key and shown **separately**
from when the data was last fetched, because those are different questions and conflating them
hides gaps. And the staleness thresholds are loose — three weeks reads as fresh — because this
model runs on years of history, and colouring normal data as a problem trains people to ignore
the colour that matters.

Everything opens the database read-only with `query_only`, since a job may be writing and a
dashboard has no business taking a write lock on 16.5 GB of someone's work.

---

## Studio

Against the live installation:

```
Dandelion Studio · Model release v0.1.0
Before you can run a projection
  The ENTSO-E token is not confirmed - without it there is no price or load history…
Data     The database exists but is empty. That is what a fresh install looks like.
Accounts RTE / ENTSO-E / Copernicus - not set up
Disk     Data 76 KB · Program 940 MB · German registry 7.4 GB · 69.7 GB free
```

Then **Check the database** ran a real job through the engine and reported
`status: finished in 1s`.

### A bug only running it would find

The first click ran the job — a log file proved it — and the screen did not change. The cause
was mine: `_start()` re-rendered the whole page, which left the *previous* activity card's
timer alive and repainting into containers that no longer existed. Every click added another.

The activity card now owns one timer and exposes its own repaint, so a click updates that card
alone. Unit tests do not reach this class of defect; opening the page does.

---

## Correction, 2026-08-24: the token did not unblock the backtest

This report said the ENTSO-E token "unblocks the backtest, which in turn unblocks the
cancellation drill." Half of that was right.

The token arrived. The cancellation drill ran on a real upstream job and passed. The
backtest did not, and could not have — I had not checked what it reads.

`backtest` opens the year with `load_fr_netload`, which reads `conso_realised` and the
`prod_*` columns from `master_hourly`. That table is built from **RTE**, and the module
docstring says the FR leg works "without any ENTSO-E dependency". The ENTSO-E fallback in
`build_master.py:84` is generation-only — every key is a `prod_*` column — because the two
sources disagree on consumption by construction (`build_master.py:81`).

Measured rather than argued. 2019 ingested in full, then the master rebuilt:

```
backfill-entsoe 2019   succeeded in 28m 17s
  prices    70,063     load   131,353     gen  1,361,139     flows  297,838

build-master           succeeded in 1m 15s
  master_hourly columns: ts_utc, ts_local, utc_offset_h,
                         price_da_be … price_da_pt        <- prices, and nothing else
```

No `conso_realised`, and no `prod_*` either: `build_master.py:120` skips any column not
already in the frame, so the ENTSO-E fallback **repairs** RTE columns and cannot bootstrap
them. With no RTE at all it is a no-op.

**An ENTSO-E token does not unblock a backtest. RTE credentials do.**

### What was built in response

`dispatch-backtest` had no preflight, so it would have constructed the config, the
workbook, the commodity model and every neighbour stack before dying inside pandas.

```
outcome : refused | did not start
reason  : The master table has no French demand column. It was built from ENTSO-E data
          alone, which carries prices and generation but not consumption, so there is
          nothing for a backtest to read. French demand comes from RTE.
```

0.0 s, and it names the account that fills the gap. `check_fr_history` distinguishes four
states that have four different fixes: no database, no master table, a master built without
RTE, and a year with real gaps in it.

The tolerance for gaps is **absolute, not proportional**. My first version allowed 1%, which
is 88 hours — enough to accept a missing 29 February as a complete year. A test now pins
that.

A second gap this exposed: `JobEngine.run` took a `preflight` callable, and nothing ever
passed one. `validate_registry` checked that every declared check was implemented, so the
registry looked sound while the checks never ran — the exact fail-open the field exists to
prevent. `declared_preflight` now wires them in by default.

301 tests.

---

## Cancellation, on a real upstream job

```
>>> running, process tree = 2 -> [19556, 2024]
>>> cancelling
job_object_used : True
escalated       : False
survivors       : NONE
outcome         : cancelled     summary : cancelled after 24s
independent orphan check: NONE
```

The gate row is met: cancelled mid-run, no orphans, verified by a separate process listing,
and a coherent job state.

`escalated: False` is worth reading carefully. It means the job object alone sufficed **for
this shape** — `backfill_entsoe.py` is a parent and one child. It does not settle the
grandchild question above, because this job has no worker pool. `run_montecarlo.py` does,
and that is Phase 6.


---

## Gate

| criterion | status |
|---|---|
| Job engine with env injection, log persistence, one-at-a-time | ✅ |
| Cancellation leaves no orphans | ✅ verified on real process trees |
| `status` run end to end from the window | ✅ |
| Home dashboard with per-source freshness | ✅ 183 sources, 0.015 s |
| **A one-year backtest from the GUI** | ⛔ needs RTE credentials, not the ENTSO-E token |
| **Cancel mid-run, verified by process listing** | ✅ on a real 28-minute upstream job |
| **Clean Win 11 VM** | ⛔ still outstanding from Phase 2 |

The two blocked rows are the same missing thing: data. A backtest needs history, and history
needs the ENTSO-E token. The cancellation row is partly covered — the mechanism is tested
against real `ProcessPoolExecutor` trees — but not yet through a job long enough to interrupt
from the window, which the backtest would provide.

---

## What I need from you

1. ~~The ENTSO-E token.~~ Arrived, stored in Credential Manager, tested against the live API
   (97 French price hours), and used to ingest 2019 in full. It unblocked the cancellation
   drill. It did **not** unblock the backtest — see the correction above.
2. **RTE credentials** — the OAuth client id and secret from RTE's data portal. This is the
   real backtest blocker. With them: `extract-rte 2019`, `build-master`, then the backtest.
3. **A clean Windows VM**, still, and now with a second question attached: whether
   `escalated` comes back `False` there. That single value tells us whether the job object
   behaves as the plan assumed on a normal machine.
4. Nothing else is blocking. Phase 4 (data refresh and model refits) can begin — its pages can
   be built and their job definitions exercised against an empty database, the same way Phase 3
   was.
