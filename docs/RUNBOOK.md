# Release runbook

Everything the owner does to publish a release. Three independent cycles — code, fits and app —
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
| **`PriceModeling\`** (the research checkout) | Only `git tag` / `git push`. Nothing else, ever — no tool here reads it. |

`release_code.py` does **not** read the checkout next door. It clones the tag from GitHub into
a scratch directory, which is exactly what keeps a release independent of whatever is sitting
in your working copy. So it runs from the app repo and needs no path to the research one.

There is no exception any more: since the product does not ship built databases, nothing in
this repository reads the research checkout at all.

---

## Once, to set up the machine

**Runs in: anywhere.**

### Do not use the Microsoft Store Python

If `python` on your PATH resolves under `WindowsApps\`, it is the Store build. It runs in an
AppContainer with **filesystem virtualization**: writes to `%LOCALAPPDATA%` and `%APPDATA%` are
silently redirected into its own private cache. Create a venv there and it lands in

```
%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.12_...\LocalCache\Local\...
```

while every later command looks for it at the path you asked for and fails with *"Le chemin
d'accès spécifié est introuvable"*. `venv` does warn — `Actual environment location may have
moved due to redirects, links or junctions` — and that warning is the whole story.

Check which one you have:

```bash
where python
```

Use the real installation instead, by full path. One command — creating the environment and
filling it must not be two steps, or the second runs against a directory that does not exist:

```bash
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m venv "%USERPROFILE%\dandelion-tools" && "%USERPROFILE%\dandelion-tools\Scripts\python" -m pip install uv pyyaml zstandard pyinstaller
```

The venv goes under `%USERPROFILE%`, not `%LOCALAPPDATA%`, so it is outside both the
virtualized area and any OneDrive-synced folder.

Check it took — this should print a version, not an error:

```bash
"%USERPROFILE%\dandelion-tools\Scripts\python" -m uv --version
```

Every `release_tools` command below assumes that interpreter. Plain `python` fails with
`No module named uv` — the system Python has none of these.

> This is not just a papercut for the owner: it is why the product provisions its own CPython
> through `uv` rather than using whatever Python a user happens to have. A Store Python would
> redirect the whole install out from under the wizard.

`uv` provisions the scratch environments, `zstandard` packs the fits and `pyinstaller`
builds the exe. All three are build-time only — none reaches a user machine. Run every command below
with `%USERPROFILE%\dandelion-tools\Scripts\python`; the exact versions used land in each
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
"%USERPROFILE%\dandelion-tools\Scripts\python" release_tools\release_code.py --tag v0.1.0
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

**Decision D3, 2026-08-20: a public releases-only repository.** `pjere/Dandelion` stays
private; a separate public repo — `pjere/dandelion-releases` — carries nothing but release
assets. The installer runs on strangers' machines and can therefore hold no credential: any
token baked into a distributed binary is a published token. Anonymous fetch is the only
workable shape.

Create it once (public, empty, no code), then per release create a GitHub release tagged
`v0.1.0` there and upload:

| asset | from |
|---|---|
| `dandelion-code-v0.1.0.zip` | `dist/v0.1.0/` |
| `constraints.lock` | `dist/v0.1.0/` |
| `code_manifest.json` | `dist/v0.1.0/` |
| `fits.NNN.tar.zst` + `fits_manifest.json` | the fits cycle below |

Nothing private leaks: the archive is exactly what a user installs and runs anyway. What stays
private is the repository's history, issues and unreleased work.

---

## 2. Data — no snapshot, but the fits do ship

**Decision, 2026-08-20: the product ships no built databases.** Every user brings their own
RTE / ENTSO-E / CDS credentials and rebuilds locally. That removes a whole release cycle and
with it the licensing question (D1b) and the hosting of ~14 GB (D1a) — you cannot have a
redistribution problem with data you do not redistribute.

**The fitted models are different** and do ship: they are your own work, not a redistribution
of anyone's data, and they are what makes a user's projections match the reference rather than
merely resemble it. They are also small — **under 1 MB** for everything except the weathergen
generator's array sidecar.

**Runs in: `Dandelion-app\`**

```bash
"%USERPROFILE%\dandelion-tools\Scripts\python" release_toolselease_fits.py --source <your-Dandelion-checkout> --tag v0.1.0 --python <release-venv>\Scripts\python.exe --dry-run
```

Drop `--dry-run` to package. Two things it insists on:

- **`--tag`.** A fit is not portable across code versions: the serialized dataclasses name
  their own classes, so a structural change upstream makes an old fit unloadable. Fits are
  published per code tag.
- **`--python`** pointing at a release venv, so every fit is **loaded through upstream's own
  deserializer before it is packaged**. A fit that does not load fails on the user's machine,
  hours into their first run, not here.

The tool declares what a complete fit set is per package rather than sweeping `*/models`,
because `save_params` writes a `.npz` sidecar *only when the payload holds arrays* and deletes
it when it does not. A JSON carrying `__ndarray__` references with no sidecar beside it is a
broken fit, and the tool says so instead of shipping it. Backups (`*.bak`, `backup*/`) and the
unreferenced `_gauss_cache.npz` are never packaged.

`dispatch_model` is deliberately absent: its one fitted artifact, `markup_model.json`, is
tracked and already ships inside the code release.

---

## 3. App release

**Runs in: `Dandelion-app\`**

```bash
"%USERPROFILE%\dandelion-tools\Scripts\python" release_tools\release_app.py --version 0.1.0 --url https://.../Dandelion.exe
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

- `code_manifest.json` is the record of what you shipped. Keep it.
- A release built from a local path is marked `"rehearsal": true` and must not be published.
- Run `"%USERPROFILE%\dandelion-tools\Scripts\python" release_tools\path_guard.py --root .` if you have edited the tooling — it fails
  the build on any path that reaches into a particular machine.
