# Phase 1 — Release tooling

**Date:** 2026-08-20
**Status:** built and exercised end to end. The gate has one open item: **the tag does not exist.**

---

## The tag

`v0.1.0` is not in the repository. Checked both ways:

```
git ls-remote --tags origin   ->  0 tags
git tag -l  (your working copy) ->  empty
```

Nothing was pushed, and nothing exists locally either, so I could not build a real release. To
avoid stalling the phase I created a throwaway tag named `v0.1.0` **in my own scratch clone**
at `main` (`09a2459`) and ran the full cycle against that. Your repository was not touched.

Every artifact from that run is stamped `"rehearsal": true` in its manifest, with the source
path recorded, and carries the note *"must not be published"*. If your real `v0.1.0` lands on
`09a2459`, the rehearsal is exactly what a real run will produce; if it lands elsewhere, re-run
the tool and everything regenerates.

To unblock:

```bash
git tag -a v0.1.0 -m "release v0.1.0" && git push origin v0.1.0
```

---

## Built

| tool | what it does |
|---|---|
| `release_tools/release_code.py` | tag → archive + lock + manifest, and refuses to publish unless a scratch install built the installer's way actually works |
| `release_tools/release_data.py` | the sanctioned read-only packager of the git-ignored data stores, gated on the licensing worksheet |
| `release_tools/data_stores.yaml` | the D1b worksheet: 10 stores, their sources, the licence to check, `ship:` per store |
| `release_tools/release_app.py` | builds the exe, runs it, measures it, stamps `latest.json` |
| `docs/RUNBOOK.md` | the owner-facing procedure, including what each failing gate means |
| `src/dandelion/__main__.py` | minimal binary shell — `freeze_support()` first, WebView2 probe, `--self-check` |

118 tests pass (28 new), ruff clean, path guard clean.

---

## Measured

### Code release — qualified

29 checks. 46 requirements resolve to **118 pinned packages**. The archive and lock hash
identically across runs, so a release is reproducible from the tag alone.

```
dandelion-code-v0.1.0.zip   1.2 MB   sha256 f0e05d66b7bfa9fa…
constraints.lock            118 packages   sha256 b91227fd70b27a73…
```

All seven packages install editable in ADR-8 order; five console scripts resolve;
`PROJECT_ROOT` anchors to the release tree; the three undeclared imports are importable.

### Data snapshot — the D1a number

```
store                  ship     files           raw       ~packed  ratio  sampled
pricemodeling_db    pending         1       16.5 GB        2.2 GB   0.13    0.1%
raw_extracts        pending     1,644        4.9 GB      178.6 MB   0.04    2.0%
era5                pending       194        4.0 GB        3.7 GB   0.92    4.7%
cmip6               pending        24       37.1 MB       16.3 MB   0.44  100.0%
lake                pending        31       67.4 MB       60.5 MB   0.90   87.5%
weathergen_output   pending         3        1.3 GB      938.1 MB   0.72    1.8%
model_fits          pending        17        3.2 GB        2.4 GB   0.74    0.8%
dispatch_reports    pending        91       60.2 MB       41.3 MB   0.69   47.2%
mastr_bulk          pending         3        7.4 GB        4.8 GB   0.64    0.2%

TOTAL                              37.4 GB  ~14.2 GB compressed
```

**Roughly 14 GB packed** for everything — the input to D1a. Two things stand out:

- **The database compresses 8×** (16.5 GB → 2.2 GB). The single most valuable store is also
  the cheapest to ship.
- **ERA5 does not compress at all** (0.92). 4 GB of NetCDF is 3.7 GB packed either way. If
  hosting is tight, this is the store to drop — at the cost of a multi-hour CDS pull on the
  user's first fit.

The `sampled` column is deliberate: a ratio from 0.1 % of a store is a decent guess, not a
measurement, and the tool says so rather than presenting all nine numbers as equally solid.

`dispatch_reports` shows `excluded: 1 tracked` — that is `markup_model.json` being correctly
kept out of the data snapshot because it ships with the code.

### App binary

| | onefile | onedir |
|---|---|---|
| exe | 8.7 MB | 2.0 MB (20 MB payload) |
| first launch | 6.61 s | 6.56 s |
| **subsequent launches** | **0.95 s** | **0.09 s** |

First launch costs the same both ways, which rules out self-extraction as its cause — it is
the antivirus scanning a newly written binary. The steady-state gap is the real finding: a
onefile exe unpacks to `%TEMP%` on *every* launch, and pays ~0.9 s for it. With NiceGUI and
pywebview bundled that only grows. **Phase 2 should start from onedir**, and the tool now
measures both so the decision stays evidence-based.

---

## Found while building

### 1. Not every dependency has a wheel

The Phase 0 expectation — "every locked dep ships a py312 win64 wheel, expected true" — is
**false**. `pymeeus` is sdist-only on PyPI, reaching us through
`demand_model → workalendar → convertdate` for the French holiday calendar.

The first version of the gate used `--only-binary :all:` and the release failed outright. That
was the wrong gate: the requirement is *no compiler on the user's machine*, and a pure-Python
sdist builds fine without one. It is now an allowlist — any source build not reviewed and
justified in `SDIST_ALLOWLIST` fails the release, and `pymeeus` carries its reason.

### 2. The upstream test suites are not offline

Twelve `dispatch_model` tests read the built database. But their behaviour depends on a detail
worth knowing:

- **no database file at all** → they skip cleanly; exactly **one** test fails;
- **an empty database file present** → eleven of them fail with `no such table`.

An empty file is what a *re-used* work tree contains, so the same tag could qualify or fail
depending on whether the scratch directory was fresh. Only the one genuinely-failing test is
allowlisted; the other eleven stay armed, and if they ever fail the tool recognises the
signature and says *"the work tree was re-used: re-run with a fresh `--work`"* rather than
blaming the code.

### 3. Two bugs my own tests caught

- **The worksheet's exclusions did nothing.** `fnmatch("a/scratchpad/cube.nc",
  "a/scratchpad")` is `False`, so every directory-shaped exclusion silently matched nothing.
  Fixed, with a test.
- **`--allow-failures` printed "qualified".** A run with a failing step still ended with
  `release v0.1.0 qualified`. That is the one sentence this tool must never say wrongly. It now
  reports NOT QUALIFIED and writes the artifacts marked unpublishable.

### 4. Smaller things

- `shutil.rmtree` cannot delete a git object store on Windows (read-only packfiles), leaving a
  partial tree so the next clone fails with *"destination path already exists"* — which reads
  like a missing tag and sends you debugging the wrong thing.
- The tools crashed on a cp1252 console while *reporting* a failure, because uv's diagnostics
  are full of box-drawing characters. Both now force UTF-8, the same guard upstream uses.
- `weathergen`'s suite takes 2m13s — most of the release's wall-clock.
- `demand_model` emits 170,558 warnings. Harmless, but it makes the log hard to read.

---

## Gate

| criterion | status |
|---|---|
| `release-code` produces a qualified, reproducible release | ✅ 29 checks, stable hashes |
| `release-data --dry-run` reports the compressed snapshot size | ✅ ~14.2 GB — feeds D1a |
| `release-app` builds, measures and stamps | ✅ both packagings measured |
| Runbook exists and covers every failure mode | ✅ `docs/RUNBOOK.md` |
| **Owner performs one full cycle unaided** | ⛔ needs the tag first |

---

## What I need from you

1. **Push the tag.** Then run `python release_tools/release_code.py --tag v0.1.0` from the
   runbook — the gate is you doing it without me.
2. **D1b, the licensing worksheet.** `release_tools/data_stores.yaml` is filled in with each
   store's sources and the licence to check; the `ship:` decisions and the sign-off are yours.
   Until then the packager only does `--dry-run`.
3. **D1a, hosting.** ~14 GB total, or ~2.3 GB for the database plus fitted models alone if you
   want a minimal fast-start snapshot. Worth deciding what the snapshot *is* before deciding
   where it lives.
4. **The registry sequence** (still open from Phase 0) — nothing in the tree composes
   `download() → build() → registry.write()`.

D3 (how the installer fetches releases from a private repo) and D2 (code signing) are not
blocking yet but both land in Phase 2.
