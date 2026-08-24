# Phase 2 — The binary shell and the Setup Wizard

**Date:** 2026-08-24
**Status:** feature-complete. **The gate is not met**: it requires a clean Windows VM with no
Python and no administrator rights, which was not available. Everything below was measured on
the owner's machine, which has Python, uv and a warm package cache — so the numbers are real
but the *absence of prerequisites* is untested.

---

## Built

| module | what it is |
|---|---|
| `paths.py` | the install layout, and the guards that run before 26 GB lands somewhere bad |
| `provision.py` | the install engine: uv, venv, code fetch, junctions, seeding, verification |
| `download.py` | resumable, checksum-verified downloads |
| `background.py` | slow work off the interface thread, via an event queue |
| `wizard.py` | seven pages, resumable at every step |
| `wizard_state.py` | what the wizard remembers between runs |
| `credentials.py` | Windows Credential Manager, and probes that report what they verified |
| `fits.py` | the fitted-model download, with a refusal to unpack anything unexpected |
| `manifest.py` | the install record — the file that turns the Wizard into Studio |
| `uninstall.py` / `uninstall_cli.py` | removal that keeps what cost a night to build |
| `ui.py` | native window where WebView2 exists, browser tab where it does not |

3,384 lines of application code against 2,112 lines of tests. **243 tests**, ruff and path
guard clean, five commits.

---

## Measured

### A complete installation, from nothing

```
PROVISIONED in 463s — 21 steps, 0 failed
```

uv-provisioned CPython 3.12 → venv → extracted release → junctions → seeded store →
118 pinned packages → seven packages editable in ADR-8 order → verification.

Then real upstream commands inside it:

```
python -m pricemodeling status
Base : ...\DandelionTest\data-root\data\pricemodeling.db
```

That line is the Phase 0 thesis working: an **unmodified** research codebase, which resolves
paths from its own file location, creating its database in a directory that is not inside it.

### Footprint

| | |
|---|---|
| environment (118 packages) | 989 MB |
| CPython | 70 MB |
| extracted code | 3.5 MB |
| **software total** | **1.1 GB** |

The 45 GB budget holds comfortably. The uv download cache — 998 MB apparent — can be cleared
afterwards: verified by deleting it and re-running every console script and import. It reclaims
far less than its size, because uv **hardlinks** into the environment and most of those bytes
were already shared.

### The packaged binary

| | onefile | onedir |
|---|---|---|
| exe | 13.2 MB | 13.2 MB (70 MB payload) |
| warm start | 0.95 s | 0.45 s |

The decisive test was not that the frozen binary *runs* but that it **builds the interface**:

```
Dandelion.exe --headless-check  →  interface built OK
```

NiceGUI ships its interface as package data, loaded only at runtime, so PyInstaller's import
analysis cannot see it. Without `--collect-all nicegui` the binary starts perfectly and then
serves a blank page on the user's machine. Same reasoning for pywebview's Windows backend
(loaded by name) and keyring's backend (found through entry points).

NiceGUI 3.16 + pywebview 6.2.1 + keyring 25.7 all survive freezing. **The PySide6 escape hatch
is not needed.**

### Credentials

`keyring` resolves to `WinVaultKeyring` — the real Windows Credential Manager. Round-trip
verified with a throwaway entry.

---

## Found while building

### 1. The uninstaller would have deleted the user's work

The software removal targeted **the whole app root**, which contains `runs/` and, by default,
the data root. It would have deleted both regardless of what the user chose. Two tests failed
the moment they were written.

`software_paths()` now enumerates only the program's own directories, data and runs are handled
first, and the app folder is removed at the end *only if it is empty*. A third test then failed
for the right reason — it asserted the app root disappears, but it must survive while it holds
kept runs. That was the test being wrong.

This is the single most valuable thing this phase produced. Everything else costs time; this
one cost a night of someone's downloading.

### 2. `os.path.islink()` is False for a junction

Verified before designing around it, because getting it wrong destroys data:

```
junction made      : True
islink()           : False        <-- the trap
is_junction        : True
rmtree the parent  : completed
TARGET FILE SURVIVED: True
```

Two things follow. `shutil.rmtree` is safe — it does not follow junctions. But anything testing
`islink()` treats a junction as an ordinary directory. `is_link()` checks `isjunction()` first,
and a test pins the discrepancy so a future refactor cannot quietly reintroduce it.

### 3. The install record claimed a scrub it never performed

The finish page appended a note saying *"secret detected and removed before writing"* and then
copied the dictionary unchanged — removing nothing while claiming it had. Ruff flagged it as a
pointless comprehension; the real defect was the lie. It now **refuses to write** the manifest,
because a secret reaching that structure is a bug in our own code, not something to paper over.

### 4. A 404 was retried five times with backoff

Clicking "Download the fitted models" against an unpublished release left the spinner going for
about a minute. A 404 will not become a 200 by asking harder. Retries are now limited to
genuinely transient failures — 408, 429, 5xx and connection-level:

```
failed in 0.1s (was ~60s before the fix)
```

Found by running the wizard rather than by reading the code.

### 5. Smaller things

- The installer reported "364 packages" because `uv pip compile` writes indented `# via …`
  provenance comments the line filter did not strip. Now 118, with a test.
- The Microsoft Store Python redirects `%LOCALAPPDATA%` writes into a private container. It cost
  three round trips during Phase 1 and is *exactly* the failure a user would hit — which is the
  evidence behind provisioning our own CPython rather than using whatever is on the machine.

---

## Decisions this phase settled

**Fits extract into `code/<tag>/`, not the shared store.** Archive members are named
`<package>/models/<file>` and `models_dir` resolves relative to each package's config file, so
unpacking there needs no overlay and no junction. It is also semantically right: a fit only
loads in the release that produced it, so it lives and dies with that tag.

**`reports/` is still never junctioned** — seeded from the release instead, because
`markup_model.json` ships inside it and the model reads it every projected year.

**The wizard can finish without working credentials.** ENTSO-E is issued by a human replying to
email; blocking on it would strand people mid-install. The manifest records what is missing and
Studio surfaces it.

**Uninstall defaults to keeping data and runs**, and removing the databases requires typing
`REMOVE`. `--quiet` removes software only — unattended is when destroying someone's work is
least excusable.

---

## Gate

| criterion | status |
|---|---|
| Wizard reaches a recorded installation | ✅ all seven pages, resumable |
| Provisioning produces a working environment | ✅ 463 s, 21 steps, real upstream commands run |
| Native-vs-browser decided on evidence | ✅ native where WebView2 exists, browser fallback otherwise |
| Uninstall leaves only what it promised | ✅ verified against the real 1.1 GB install |
| **Clean Win 11 VM, no Python, no admin** | ⛔ **not available — outstanding** |
| **Kill mid-download, then resume** | ⚠️ resume logic tested by unit test; not exercised for real |

The two open rows are the same missing thing: a machine without the prerequisites. On this
machine uv, a Python and a warm package cache all exist, so the paths that matter most to a
stranger — *what happens when none of that is there* — have never run.

---

## What I need from you

1. **A clean Windows VM**, or a decision to ship the Phase 2 gate as accepted-with-risk. Until
   then, "installs on a machine with no Python" is a design intent, not a measurement.
2. **Publish the v0.1.0 release assets** to `pjere/Dandelion-app`. The installer currently
   cannot fetch anything — the 404 above is the honest evidence of that. Until it is published,
   the download paths are exercised only against a local archive.
3. Nothing else is blocking. Phase 3 (Studio shell and job engine) can begin on top of this.
