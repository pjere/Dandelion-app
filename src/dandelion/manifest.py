"""The record of what was installed, and what it was built from.

Its presence is what makes the executable open Studio instead of the Setup Wizard, so writing
it is the last act of a successful install and never a hopeful one.

It answers, months later, the question that actually gets asked: *why does my number differ
from yours?* So it records the code tag and its commit, the archive and lock checksums, the
fitted models and their hashes, and the exact terms text that was accepted — enough to
reconstruct which combination produced a given result.

It contains **no secrets**: which credentials were stored and whether they passed a test, never
the values. It also carries no absolute path beyond the two install roots, because manifests
get attached to support emails.
"""
from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

MANIFEST_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class CodeRelease:
    tag: str = ""
    commit: str = ""
    archive_sha256: str = ""
    lock_sha256: str = ""
    packages: list[str] = field(default_factory=list)


@dataclass
class FittedModels:
    #: "downloaded" | "fit-locally" | "none"
    source: str = "none"
    tag: str = ""
    artifacts: list[dict] = field(default_factory=list)
    downloaded_bytes: int = 0


@dataclass
class Terms:
    accepted_at: str = ""
    version: str = ""
    sha256: str = ""


@dataclass
class InstallManifest:
    version: int = MANIFEST_VERSION
    app_version: str = ""
    installed_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    app_root: str = ""
    data_root: str = ""

    code: CodeRelease = field(default_factory=CodeRelease)
    fits: FittedModels = field(default_factory=FittedModels)
    terms: Terms = field(default_factory=Terms)

    #: credential key -> "passed" | "failed" | "skipped" | "untested". Never a value.
    credentials: dict[str, str] = field(default_factory=dict)

    #: link -> target, so the uninstaller removes links rather than following them into data.
    junctions: dict[str, str] = field(default_factory=dict)
    #: relative path -> sha256 of the artifacts seeded out of the release.
    seeded: dict[str, str] = field(default_factory=dict)

    python: str = ""
    uv: str = ""
    platform: str = field(default_factory=lambda: f"{platform.system()} {platform.release()}")

    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Path) -> InstallManifest | None:
        """Returns None when there is no usable manifest — which means "run the wizard"."""
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if raw.get("version") != MANIFEST_VERSION:
            return None
        nested = {
            "code": CodeRelease(**(raw.pop("code", None) or {})),
            "fits": FittedModels(**(raw.pop("fits", None) or {})),
            "terms": Terms(**(raw.pop("terms", None) or {})),
        }
        known = set(cls.__dataclass_fields__) - set(nested)
        return cls(**nested, **{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        self.updated_at = _now()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)                                # never a half-written manifest

    # ------------------------------------------------------------------ questions
    @property
    def usable(self) -> bool:
        """Whether Studio can open against this installation."""
        return bool(self.code.tag and self.app_root)

    def missing_for_a_full_run(self) -> list[str]:
        """What the user still has to do. Studio shows this on its home page."""
        gaps: list[str] = []
        if self.credentials.get("entsoe") != "passed":
            gaps.append("The ENTSO-E token is not confirmed - without it there is no price "
                        "or load history to build from.")
        if self.fits.source == "none":
            gaps.append("No fitted models yet: download them, or run the calibration "
                        "yourself (several hours, and needs a Copernicus account).")
        return gaps


def build(*, app_version: str, app_root: Path, data_root: Path, tag: str,
          code_manifest: dict | None = None, wizard_state=None,
          fits: FittedModels | None = None, junctions: dict[str, str] | None = None,
          seeded: dict[str, str] | None = None, uv_version: str = "") -> InstallManifest:
    """Assemble the manifest from what the install actually produced."""
    code = CodeRelease(tag=tag)
    if code_manifest:
        code.commit = code_manifest.get("commit", "")
        code.archive_sha256 = code_manifest.get("archive_sha256", "")
        code.lock_sha256 = code_manifest.get("lock_sha256", "")
        code.packages = list(code_manifest.get("packages", []))

    manifest = InstallManifest(
        app_version=app_version,
        app_root=str(app_root),
        data_root=str(data_root),
        code=code,
        fits=fits or FittedModels(),
        junctions=dict(junctions or {}),
        seeded=dict(seeded or {}),
        python=sys.version.split()[0],
        uv=uv_version,
    )
    if wizard_state is not None:
        manifest.terms = Terms(
            accepted_at=wizard_state.terms.at,
            version=wizard_state.terms.disclaimer_version,
            sha256=wizard_state.terms.disclaimer_sha256,
        )
        manifest.credentials = dict(wizard_state.credentials)
    return manifest


def contains_no_secrets(manifest: InstallManifest, secrets: list[str]) -> bool:
    """Belt and braces before a manifest is written or attached to a support email."""
    text = json.dumps(asdict(manifest))
    return not any(secret and len(secret) >= 8 and secret in text for secret in secrets)
