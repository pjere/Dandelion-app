# Phase 4 — Data refresh and model refits

**Date:** 2026-08-25
**Status:** built and verified in a browser against the live installation. Both pages work
on real data. The gate is met with one caveat: nothing here has been exercised on a clean
Windows VM, which is now outstanding from Phase 2 for the third phase running.

---

## Built

| module | lines | what it is |
|---|---|---|
| `journal.py` | 164 | what has been run, surviving the window closing |
| `staleness.py` | 186 | which fitted models the data has moved on from |
| `expectations.py` | 81 | how long a job takes, and whether it will say anything |
| `pages.py` | 221 | the Data and Models pages |

**421 tests**, ruff and path guard clean, six commits.

---

## The same bug, a third time

`requires` turned out to be declared and enforced **nowhere** — after `preflight` in Phase 3
and `needs_credentials` an hour later. `validate_registry` checked each edge pointed at a
real job; nothing ever checked it had run. The edges exist precisely because upstream does
not enforce them, so leaving them unenforced reproduced the failure they document.

Only one of eleven model jobs declared its edges. The rest are now grounded in actual
`.load()` calls found in the release tree, not inferred from names:

```
demand   projection/engine.py:125-126   calibrated.json, residual.json
res      projection/engine.py:64-65     calibrated_res.json, residual_res.json
avail    projection/engine.py:143       calibrated_availability.json
```

Demand and RES additionally need a simulated cube — both docstrings say so. Availability
draws outages from its fitted process and needs none, so it does not get a spurious edge. A
test walks the graph for cycles, since a cycle would leave two jobs permanently unrunnable
with each refusal pointing at the other.

**Three fields, three phases, one pattern.** Worth a standing check when adding any field to
`Job`: something must read it at run time, or it is a comment.

---

## The journal

Enforcement needs durable state, and there was none — logs sat on disk with timestamped
names, but no outcome was recorded anywhere. Append-only JSONL, one object per line: a
truncated final line after a hard kill costs one record rather than the file, and it stays
readable when someone needs to send it.

Two decisions worth stating. **Everything written passes through `credentials.scrub`** —
params reach the journal and a job could in principle be handed a secret, which is exactly
why the logs get that treatment. And **a run under a different code tag does not satisfy a
prerequisite**: output from another build of the model is not evidence this one has been
fitted. Refusals are journalled too, because what a user hit last time is what they will ask
about.

---

## Staleness is two dates, not a file listing

A model file's timestamp says when it was written, not whether it is still right. The
question is whether anything it was fitted on has changed since — the fit's date from the
journal, its inputs' from `ingest_log`. The dependency map is grounded in what each package
reads: `demand_model`'s own config declares its two RTE tables; `availability_model` reads
`rte_generation_per_unit`, `rte_water_reserves` and `dim_production_unit`.

**An input that cannot be dated reads as `unknown`, never `current`.** `build-master` merges
rather than ingests, so it has no `ingest_log` row at all. Saying "stale" wrongly costs a
refit; saying "current" wrongly leaves someone projecting off a model that never saw the
last two years, with nothing on screen to say so. The asymmetry decides it.

### A wrong answer caught by running it

The first version reported all four models as **"has never been fitted here"** against the
live install. Literally true, and misleading: the release ships your fitted models. Offering
a refit there would send users into multi-hour calibrations to reproduce what they already
have, and on a partial local history probably reproduce it worse.

```
fits_shipped=True    weathergen-fit    shipped   came with the model release
fits_shipped=False   weathergen-fit    never     has never been fitted here
```

`shipped` is its own state, and does not count as needing a refit. Whether your data differs
enough to justify one is a judgement the product cannot make for you.

---

## Silence is the failure mode nobody designs for

`ingest-remit` ran seventeen minutes and printed one line, at the end. Under a pipe that is
indistinguishable from a hang, and the reasonable response to a hang is to kill it — losing
someone else's API quota and leaving a half-filled table.

Jobs now declare whether they are quiet and roughly how long they take. **Every duration is
measured from this project's own runs**, not guessed:

```
backfill-entsoe 28m   clusters 27m   ingest-remit 17m (silent)
extract-rte     10m   backtest 16m   build-master  1m (silent)
```

A test pins those numbers so nobody later "improves" them into invention — the sentence
built from a made-up number is simply false.

**A typical never becomes a percentage.** A job at 90% of its usual duration is not 90%
done, and a bar claiming that becomes a lie the moment it runs long. Running long is
reported in words instead, with no second estimate dressed up as information. Where upstream
emits real progress lines, this module says nothing at all.

### The counter, and why it was needed

Without it a quiet job never repaints: `apply_all` returns `False` with no output and
`engine.progress` stays `None`, so `changed` never becomes `True` and `paint()` is never
called. The card would have sat on "Working…" for the whole run. Verified live by starting
`build-master` from the Data page:

```
running for 6s of about 2 minutes. This one prints nothing until it is done.
```

The sentence changes once a second, which is what throttles the repaint. No timer tricks.

---

## The pages

**Data** groups the ingest jobs by the account each needs, because "I have an RTE login and
not an ENTSO-E one" is the shape a user's situation takes — not by which upstream package
implements them. Every job is its own button with its measured cost beside it.

**No update-everything button**, by your instruction and by measurement: that chain was about
ninety minutes of somebody else's API quota on this machine, and one click hiding it gets
pressed at five o'clock and abandoned. A test asserts the only button labels are Run and
Refit.

A test also caught `extract-entsoe` reachable from **nowhere**. It is now an explicit
"not offered" entry with its reason — it fetches FR prices for one zone where
`backfill-entsoe` fetches all eight — so an omission has to be deliberate.

### Tabs took four attempts

Three tries at Quasar's `ui.tab_panels` all failed, and each time **the tab strip highlighted
correctly** while `elementFromPoint` proved the old panel was still being painted:

```
bind_value on tabs only        panels parked
bind_value on both ends        the two bindings fight over one attribute
documented tabs-drives-panels  linkage did not take
```

Had I trusted the highlight — the obvious signal — I would have shipped a window whose tabs
do not work, three times over. The fix is three plain containers whose visibility is set
explicitly: less clever, entirely debuggable, and it avoids re-rendering from a handler,
which is what left a stale timer repainting detached containers in Phase 3.

The browser also turned up a stray `0` floating in the middle of the indeterminate progress
bar — NiceGUI renders `linear_progress`'s raw value.

---

## Gate

| criterion | status |
|---|---|
| Data page listing every ingest job, reachable and separate | ✅ |
| Freshness per source, shown against what refreshes it | ✅ 147 sources |
| Credentials enforced before a job starts, not after | ✅ |
| Model refits page showing what the data has overtaken | ✅ |
| Dependency edges enforced | ✅ grounded in real `.load()` calls |
| Honest progress for silent jobs | ✅ verified live |
| **Clean Win 11 VM** | ⛔ outstanding since Phase 2 |

End to end, in a browser, on the live install: clicked **Run** on the Data page →
`build-master` ran → **88.6 s, succeeded** → recorded in the journal.

---

## What I need from you

1. **A clean Windows VM.** Third phase asking. Beyond the Phase 2 install question there are
   now two more: whether `escalated` comes back `False` for a *deep* process tree (the
   release environment's `python.exe` is itself a launcher shim, so real jobs run three deep),
   and whether the pages render on a machine that has never had a dev toolchain.
2. **`golden/baseline.json` is still stale in v0.1.0** — carried over from Phase 3. Until it
   is re-captured the product has no valid accuracy target.
3. **Whether `MODEL_INPUTS` matches your intent.** I grounded it in what each package reads,
   but you know which inputs actually matter to a fit. If refitting demand should also
   depend on weather observations, say so and it is one line.
4. Nothing else is blocking. Phase 5 can begin.
