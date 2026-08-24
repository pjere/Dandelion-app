# Phase 3 — Studio shell and the job engine

**Date:** 2026-08-24
**Status:** built and exercised against a real installation. **The gate is not met**: it wants a
one-year backtest run end to end from the window and a cancel verified mid-run, and both need
data the machine does not have — the ENTSO-E token has not arrived, so the database is empty.

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

## Gate

| criterion | status |
|---|---|
| Job engine with env injection, log persistence, one-at-a-time | ✅ |
| Cancellation leaves no orphans | ✅ verified on real process trees |
| `status` run end to end from the window | ✅ |
| Home dashboard with per-source freshness | ✅ 183 sources, 0.015 s |
| **A one-year backtest from the GUI** | ⛔ needs a built database |
| **Cancel mid-run, verified by process listing** | ⚠️ proven at the `ProcessGroup` level; not yet through a long GUI job |
| **Clean Win 11 VM** | ⛔ still outstanding from Phase 2 |

The two blocked rows are the same missing thing: data. A backtest needs history, and history
needs the ENTSO-E token. The cancellation row is partly covered — the mechanism is tested
against real `ProcessPoolExecutor` trees — but not yet through a job long enough to interrupt
from the window, which the backtest would provide.

---

## What I need from you

1. **The ENTSO-E token when it arrives.** It unblocks the backtest, which in turn unblocks the
   cancellation drill. Nothing else is waiting on it.
2. **A clean Windows VM**, still, and now with a second question attached: whether
   `escalated` comes back `False` there. That single value tells us whether the job object
   behaves as the plan assumed on a normal machine.
3. Nothing else is blocking. Phase 4 (data refresh and model refits) can begin — its pages can
   be built and their job definitions exercised against an empty database, the same way Phase 3
   was.
