# Phase 1 — Release tooling

**Date:** 2026-08-20
**Status:** **complete — gate met.** The owner cut and pushed `v0.1.0`, then ran the full code
release from the runbook unaided and got `release v0.1.0 qualified — 29/29`.

---

## The tag — resolved

`v0.1.0` now exists and is pushed: `refs/tags/v0.1.0` → `09a2459`.

Before it existed I rehearsed the cycle against a throwaway tag in my own scratch clone. That
turned out to be worth more than a stand-in: the owner's real run, fetching from
`https://github.com/pjere/Dandelion.git`, produced **byte-identical artifacts** to the
rehearsal built from a local clone —

```
archive sha256  f0e05d66b7bfa9fa2f53bd7f01a2579f3ba6848d166a0df2ff3f859e59d918f6
lock    sha256  b91227fd70b27a739d8f37284e82d31592d2539097b1296fe96ccdee1442c53e
```

— which is the reproducibility claim demonstrated rather than asserted: two independent runs,
two different sources, two different tag objects, same commit, same bytes.

The owner's manifest is kept at `docs/evidence/code_manifest-v0.1.0-owner-run.json`, with
absolute user paths redacted — the path guard refused the raw file, correctly: uv echoes build
locations into the qualification details, so a manifest carries the machine layout of whoever
built it. Worth remembering for Phase 7, where run bundles get exported: they need path
scrubbing alongside secret scrubbing.

---

## Built

| tool | what it does |
|---|---|
| `release_tools/release_code.py` | tag → archive + lock + manifest, and refuses to publish unless a scratch install built the installer's way actually works |
| `release_tools/release_fits.py` | packages the owner's fitted models, and refuses to ship a fit that will not load |
| `release_tools/release_app.py` | builds the exe, runs it, measures it, stamps `latest.json` |
| `docs/RUNBOOK.md` | the owner-facing procedure, including what each failing gate means |
| `src/dandelion/__main__.py` | minimal binary shell — `freeze_support()` first, WebView2 probe, `--self-check` |

128 tests pass, ruff clean, path guard clean.

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

### Data snapshot — measured, then made moot

The dry run measured **37.4 GB raw → ~14.2 GB packed** across ten stores, with the database
compressing 8× (16.5 GB → 2.2 GB) and ERA5 not compressing at all (0.92).

**Owner decision, 2026-08-20: the product ships no built databases.** Users bring their own
credentials and rebuild locally, so D1b (licensing) and D1a (hosting) both dissolve — there is
no redistribution to license or host. `release_data.py` and its worksheet were removed
accordingly (recoverable from git history at `d5acb9d`).

The measurement is kept here because it is the cost of the alternative, and because two of its
numbers still matter to Phase 2: ERA5 is 4 GB that every user must now pull from CDS on their
first fit, and MaStR is 7.4 GB every user downloads themselves.

The secondary effect is worth more than the tool: `release_data.py` was the **single sanctioned
exception** to "never read the owner's working copy". With it gone, no tool in the repository
reaches outside it, and `path_guard.py` now enforces that with no exemptions at all.

### Model fits — measured and packaged

**Owner decision, 2026-08-20: ship the fitted models.** They are the owner's own work, not a
redistribution of anyone's data, and they are what makes a user's numbers match the reference
rather than merely resemble it.

```
weathergen         fitted.json 153.5 KB + fitted.json.npz 2.6 GB
                   wind100.json + .npz, cmip6_deltas_ssp245_2050_mpi_esm1_2_lr.npz
demand_model       calibrated.json + .npz, residual.json + .npz     (782 KB)
res_model          calibrated_res / residual_res / wind_transfers   ( 19 KB)
availability_model calibrated_availability.json                     ( 13 KB)

TOTAL 2.6 GB raw -> 1.3 GB packed, 3 chunks, all 8 fits load
```

Everything except the weathergen generator's array sidecar is under 1 MB; that one file is
**2.6 GB and doubled during this session's afternoon**. Compression halves it.

The payoff is bigger than the saved calibration time. The only two things that pull ERA5 from
CDS are `weathergen fit` and `res-model calibrate`; ship both fits and neither ever runs, and
the CMIP6 deltas ship too. **The CDS account plausibly drops off the critical path entirely** —
one of three credentials, plus a ~4 GB download and hours of fitting. Phase 2 should confirm
that end to end before promising it.

`dispatch_model` is deliberately excluded: `markup_model.json` is tracked and already ships
inside the code release, and two copies would have no tie-break rule.

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

**And the second version of the gate was unsound.** It detected source builds by parsing
`Building <pkg>` out of uv's install output. That works exactly once per machine: uv caches
the wheel it builds, so the *second* run installs from cache, prints no `Building` line, and
the check passes without having checked anything. The owner's run shows it — `built_from_sdist`
is empty even though `pymeeus` has no wheel, because my earlier runs had already warmed the
cache on that machine.

A gate that passes because it ran before is worse than no gate. It is now a **resolution**
check — `uv pip install --only-binary :all: --dry-run` with a `--no-binary` exemption per
allowlist entry — which asks the index, not the cache, and therefore gives the same answer
cold or warm. Verified both directions: it resolves with the exemption and fails naming
`pymeeus==0.5.12` without it. The install-output parse is kept as provenance, not as the gate.

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

### 3b. The fits packager caught a refit in flight, then a flaw in itself

On its first run against the owner's checkout it refused to package, reporting
`weathergen/models/fitted.json references arrays but fitted.json.npz is missing`. That looked
like a lost file; it was a **refit in progress** — the JSON written, the 2.6 GB sidecar not yet.
Forty minutes later the pair was complete and loaded cleanly. Exactly the guard working: a
packager that swept the directory would have shipped an unloadable fit, and the user would have
discovered it hours into their first run.

It also exposed a flaw in my own chunker. It caps *input* bytes per chunk, which normally keeps
the compressed output well under GitHub's 2 GB asset limit — but a **single artifact larger
than the chunk size cannot be split**, so its published size rides entirely on how well that
one file compresses. The weathergen sidecar is 2.6 GB and happened to halve, landing at 1.3 GB.
Had it compressed poorly it would have exceeded the limit and been rejected at upload, after the
whole cycle had run. The output size is now checked explicitly, and an unsplittable artifact is
called out before packaging rather than discovered after.

### 4. Smaller things

- `shutil.rmtree` cannot delete a git object store on Windows (read-only packfiles), leaving a
  partial tree so the next clone fails with *"destination path already exists"* — which reads
  like a missing tag and sends you debugging the wrong thing.
- The tools crashed on a cp1252 console while *reporting* a failure, because uv's diagnostics
  are full of box-drawing characters. Both now force UTF-8, the same guard upstream uses.
- `weathergen`'s suite takes 2m13s — most of the release's wall-clock.
- `demand_model` emits 170,558 warnings. Harmless, but it makes the log hard to read.

### 5. Two runbook papercuts, one of which matters beyond the runbook

Getting the owner through the first real release cost three round trips, all documentation
rather than code:

- **Which directory.** `release_tools/` lives in the app repo, but the tag commands run in the
  research checkout, and the runbook said neither. Every command block is now labelled.
- **The Microsoft Store Python.** The owner's `python` resolves under `WindowsApps\`, which
  runs in an AppContainer that silently redirects writes to `%LOCALAPPDATA%` into a private
  cache. `python -m venv %LOCALAPPDATA%\dandelion-tools` therefore "succeeded" into
  `…\Packages\PythonSoftwareFoundation.Python.3.12_…\LocalCache\Local\`, while every later
  command looked at the real path and failed with *"Le chemin d'accès spécifié est
  introuvable"*.

The second one is not just a papercut. It is precisely the failure mode a user would hit, and
it is **evidence for the architecture decision to bundle `uv` and provision our own CPython**
rather than use whatever Python is on the machine. A Store Python would redirect the entire
install out from under the wizard, and the wizard would report success. Phase 2 assumed this;
it is now demonstrated.

---

## Gate

| criterion | status |
|---|---|
| `release-code` produces a qualified, reproducible release | ✅ 29 checks, stable hashes |
| `release-data --dry-run` reports the compressed snapshot size | ✅ ~14.2 GB — then superseded: no snapshot ships |
| `release-app` builds, measures and stamps | ✅ both packagings measured |
| Runbook exists and covers every failure mode | ✅ `docs/RUNBOOK.md` |
| **Owner performs one full cycle unaided** | ✅ `29/29`, `rehearsal: false`, no notes |

---

## What I need from you

1. ~~Push the tag and run the cycle.~~ **Done.**
2. ~~D1b licensing / D1a hosting.~~ **Resolved: no data ships.**
3. ~~The registry sequence.~~ **Reconstructed from the lake and confirmed** — see
   `UPSTREAM_WISHLIST.md` §2. Excluding MaStR solar and pumped storage is deliberate.
4. **Create `pjere/dandelion-releases`** (public, empty) — decision D3. Nothing can be
   published until it exists.
5. **D4 text**: product name, disclaimer wording, support address. You said you would supply
   these; Phase 2's first wizard page needs them.

D3 (how the installer fetches releases from a private repo) and D2 (code signing) are not
blocking yet but both land in Phase 2.
