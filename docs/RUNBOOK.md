# Release runbook

Everything the owner does to publish a release. Three independent cycles — code, data, app —
that can be run separately and in any order.

Nothing here touches the research repository beyond reading it. No command in this document
writes to `pjere/Dandelion`.

---

## Two directories — which command runs where

This is the single easiest thing to get wrong, so every command block below is labelled with
the directory it runs in.

| | |
|---|---|
| **`Dandelion-app\`** (this repo) | Every `release_tools\...` command. This is where the tooling lives. |
| **`PriceModeling\`** (the research checkout) | Only `git tag` / `git push`. Nothing else, ever. |

`release_code.py` does **not** read the checkout next door. It clones the tag from GitHub into
a scratch directory, which is exactly what keeps a release independent of whatever is sitting
in your working copy. So it runs from the app repo and needs no path to the research one.

The one exception is `release_data.py`, which takes the checkout as an explicit `--source`
argument — it is the sanctioned data packager, and even it only reads.

---

## Once, to set up the machine

**Runs in: anywhere.** One command — creating the environment and filling it must not be two
steps, or the second runs against a directory that does not exist yet.

```bash
python -m venv "%LOCALAPPDATA%\dandelion-tools" && "%LOCALAPPDATA%\dandelion-tools\Scripts\python" -m pip install uv pyyaml zstandard pyinstaller
```

Check it took:

```bash
"%LOCALAPPDATA%\dandelion-tools\Scripts\python" -m uv --version
```

Every `release_tools` command below assumes that interpreter. Plain `python` fails with
`No module named uv` — the system Python has none of these. If cmd reports *"Le chemin d'accès
spécifié est introuvable"*, the venv is missing: re-run the command above.

`uv` provisions the scratch environments, `zstandard` packs snapshots, `pyinstaller` builds
the exe. All three are build-time only — none reaches a user machine. Run every command below
with `%LOCALAPPDATA%\dandelion-tools\Scripts\python`; the exact versions used land in each
manifest.

---

## 1. Code release

### Cut the tag

**Runs in: `PriceModeling\` (the research checkout).**

```bash
git tag -a v0.1.0 -m "release v0.1.0" && git push origin v0.1.0
```

Any commit you are happy for a non-developer to run. **A tag is required** — the whole release
model is "a tag → an archive → a lock", and `main` moves.

### Build and qualify it

**Runs in: `Dandelion-app\`** — not the research checkout. The tool fetches the tag
from GitHub itself.

```bash
python release_tools/release_code.py --tag v0.1.0
```

Takes roughly 6–8 minutes, most of it the upstream test suites. It clones the tag into a
scratch directory, archives it, resolves the lock, builds a throwaway environment **using the
exact procedure the installer will use**, and only then declares the release qualified.

Output lands in `dist/v0.1.0/`:

| file | what it is |
|---|---|
| `dandelion-code-v0.1.0.zip` | the source tree, straight from `git archive` |
| `constraints.lock` | 118 pinned packages, resolved for windows / cpython 3.12 |
| `code_manifest.json` | hashes, sizes, tool versions, and every qualification result |

Both the archive and the lock hash reproducibly: re-running the same tag gives the same bytes.

### If it says NOT QUALIFIED

The tool never publishes a failing build, and never calls one "qualified". Read the failing
step; each maps to a specific action.

| failing step | what it means | what to do |
|---|---|---|
| `no unreviewed source builds` | a dependency has no wheel and would need building on a user machine | check whether it is pure Python. If it is, add it to `SDIST_ALLOWLIST` in `release_code.py` **with the reason**. If it needs a compiler, pin around it — users have no build tools. |
| `no NEW undeclared third-party import` | upstream added a lazy `import` of something declared nowhere | pin it in `DRIVERS_SUPPLEMENT`, add it to `UPSTREAM_WISHLIST.md`, re-run |
| `inherited config values unchanged` | upstream changed a value the product deliberately inherits (e.g. `trend.ssp`) | decide whether to follow. Nothing is overwritten for you. |
| `editable install <pkg>` | ADR-8 install order or a package broke | reproduce by hand in the scratch venv |
| `pytest <pkg>` naming UNEXPECTED tests | a genuine regression in the tag | fix upstream, cut a new tag |
| `pytest dispatch_model` listing tests that "skip on a clean tree" | the work tree was re-used and holds a stale empty database | re-run with a fresh `--work` directory |

`--skip-tests` gives a fast rehearsal. It is not a release: the manifest records that the
suites were skipped.

### Publish

Upload all three files as assets of the GitHub release for the tag. **Where** is decision D3 —
`pjere/Dandelion` is private and no PAT may ever be baked into the installer, so this needs a
public releases-only repository, or hosting alongside the data snapshot.

---

## 2. Data snapshot

### First, the licensing worksheet (D1b)

`release_tools/data_stores.yaml` lists every store, the sources inside it, and the licence to
check. Redistributing a database built from someone else's API is a per-source question, so
the packager will not touch a store until you have answered it.

Work through each store, set `ship: yes` or `ship: no`, then set `signed_off: true` with your
name and the date. A `no` is not a failure — that store becomes rebuild-only and the wizard
tells the user so.

The most restrictive source in a store governs the whole store: `pricemodeling.db` is one
SQLite file containing every ingested source and cannot be separated.

### Measure before deciding

**Runs in: `Dandelion-app\`**, pointing at the research checkout.

```bash
python release_tools/release_data.py --source <your-Dandelion-checkout> --dry-run
```

Writes nothing. Reports every store's size, an estimated compressed size, and — importantly —
what fraction of each store was actually sampled to produce that estimate. Read a ratio with
its coverage: 0.1 % coverage on a 16 GB database is a decent guess, not a measurement.

Measured on the owner's machine, 2026-08-20: **37.4 GB raw → roughly 14 GB packed** across all
stores. That is the number that decides hosting (D1a).

### Build it

**Runs in: `Dandelion-app\`**

```bash
python release_tools/release_data.py --source <your-Dandelion-checkout>
```

Packs each `ship: yes` store into zstd-compressed tar chunks of at most 1.4 GB — under
GitHub's 2 GB asset cap — with a sha256 per chunk in `data_manifest.json`.

Two guards run on every file. Anything `git ls-files` knows about is skipped, because a
tracked file is code and comes from the code release; anything that *looks* like source is
skipped as an independent backstop. `dispatch_model/reports/markup_model.json` is the case
that matters — it lives among data outputs but ships with the code.

### Verify what you are about to publish

**Runs in: `Dandelion-app\`**

```bash
python release_tools/release_data.py --verify dist\snapshot-2026-08-20
```

Re-hashes every chunk against the manifest. Run it after uploading too, against a
re-downloaded copy — that is the same check the installer performs.

---

## 3. App release

**Runs in: `Dandelion-app\`**

```bash
python release_tools/release_app.py --version 0.1.0 --url https://.../Dandelion.exe
```

Builds the executable, then **runs it**: version, cold start, warm start, and the binary's own
`--self-check`. A build that cannot run is not a release. Results and a sha256 go into
`latest.json`, which installed copies poll for updates.

`--onedir` builds a directory instead of a single file. Measured on the owner's machine with
the Phase 1 shell:

| | onefile | onedir |
|---|---|---|
| exe | 8.7 MB | 2.0 MB (20 MB payload) |
| first launch | 6.6 s | 6.6 s |
| subsequent launches | **0.95 s** | **0.09 s** |

First launch costs the same either way — that is the antivirus scanning a new binary, not the
packaging. The steady-state difference is real: onefile unpacks itself to `%TEMP%` on *every*
launch. Phase 2 decides on clean-VM evidence; today the numbers favour onedir.

The binary is **unsigned** until D2, so SmartScreen will warn every user. `latest.json` records
`"signed": false` so this cannot be forgotten.

---

## After any release

- `code_manifest.json` and `data_manifest.json` are the record of what you shipped. Keep them.
- A release built from a local path is marked `"rehearsal": true` and must not be published.
- Run `python release_tools/path_guard.py --root .` if you have edited the tooling — it fails
  the build on any path that reaches into a particular machine.
