# Dandelion App

Installer and graphical studio for **Dandelion**, the European electricity spot-price model
at `github.com/pjere/Dandelion`.

One signed binary, two modes: no install manifest under `%LOCALAPPDATA%\Dandelion\` → the
**Setup Wizard**; manifest present → **Studio**.

## The rule this repository exists to respect

**The research codebase is read-only and stays that way.** Nothing here may require a change
upstream — not a flag, not a config key, not a print statement.

- Code enters the product only as a **pinned release archive** (`git archive <tag>` + a lockfile
  we generate). The owner's working copy is never a source of anything the product executes.
- All upstream knowledge lives in `drivers/`. When a new tag is cut, `drivers/` and the pin are
  the only things that should need to change.
- The product ships **no built databases**. Every user brings their own RTE / ENTSO-E / CDS
  credentials and rebuilds locally. The owner's **fitted models** do ship — they are his own
  work, under 1 MB, and they are what makes a user's numbers match the reference.

`release_tools/path_guard.py` enforces this in CI.

## Layout

```
src/dandelion/     the application (wizard + studio)
drivers/           the ONLY place that knows about upstream
  inventory.py       36 jobs: argv, cwd, credentials, progress label, failure markers
  anchoring.py       the 12 relocatable stores and which mechanism relocates each
  wrappers/          thin wrappers where upstream exposes functions but no CLI
release_tools/     release-code / release-fits / release-app, import scan, path guard
installer/         PyInstaller packaging
tests/
docs/phase-reports/
```

## Working with it

```bash
python drivers/inventory.py --list
```

```bash
python release_tools/path_guard.py --root .
```

Probe a real extracted release against a provisioned venv:

```bash
python drivers/inventory.py --selftest --code-root <release-dir> --python <venv>/Scripts/python.exe
```

Find third-party imports upstream never declared:

```bash
python release_tools/import_scan.py <release-dir>
```

## State

Phase 0 complete — see [docs/phase-reports/phase-0.md](docs/phase-reports/phase-0.md).
Open asks for the owner are listed at the end of that report;
[UPSTREAM_WISHLIST.md](UPSTREAM_WISHLIST.md) ranks the (optional) upstream changes that would
simplify this product.
