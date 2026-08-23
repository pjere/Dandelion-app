"""What the wizard remembers between runs.

Getting a Dandelion Studio installation working is not a five-minute task: the ENTSO-E token
arrives by email days after you ask for it, and a full rebuild runs overnight. So the wizard
is built to be abandoned and resumed rather than completed in one sitting — closing it must
never mean starting again.

State lives in plain JSON next to the installation. Secrets never appear here; they go to
Windows Credential Manager, and this file records only *whether* a credential was stored and
whether its test passed.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATE_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Acceptance:
    """Proof of what was agreed to, not merely that something was."""

    accepted: bool = False
    at: str = ""
    disclaimer_version: str = ""
    #: Hash of the exact text shown. Editing the terms later cannot retroactively claim
    #: someone agreed to the new wording.
    disclaimer_sha256: str = ""


@dataclass
class WizardState:
    version: int = STATE_VERSION
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    terms: Acceptance = field(default_factory=Acceptance)

    app_root: str = ""
    data_root: str = ""
    locations_confirmed: bool = False

    code_tag: str = ""
    runtime_ready: bool = False

    #: credential key -> "untested" | "passed" | "failed" | "skipped"
    credentials: dict[str, str] = field(default_factory=dict)

    fits_choice: str = ""          # "download" | "fit-myself" | "later"
    fits_ready: bool = False

    data_choice: str = ""          # "rebuild" | "later"

    finished: bool = False

    # ------------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Path) -> WizardState:
        """Read saved state, tolerating a file written by a different version."""
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if raw.get("version") != STATE_VERSION:
            # A future version's file is not ours to interpret; start clean rather than
            # half-restore an installation.
            return cls()
        terms = Acceptance(**raw.pop("terms", {}) or {})
        known = {f for f in cls.__dataclass_fields__ if f != "terms"}
        return cls(terms=terms, **{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        self.updated_at = _now()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)                                # atomic: never a half-written state

    # ------------------------------------------------------------------ progress
    @property
    def next_step(self) -> str:
        """Where re-running the wizard should drop the user."""
        if not self.terms.accepted:
            return "welcome"
        if not self.locations_confirmed:
            return "locations"
        if not self.runtime_ready:
            return "runtime"
        if not self.credentials:
            return "credentials"
        if not self.fits_choice:
            return "models"
        if not self.data_choice:
            return "data"
        return "finish"

    def credential_state(self, key: str) -> str:
        return self.credentials.get(key, "untested")

    @property
    def can_finish(self) -> bool:
        """The minimum for a usable installation.

        Deliberately permissive about credentials: ENTSO-E can take days to arrive, and a user
        who has installed the runtime and chosen where data goes has a real installation even
        if it cannot fetch anything yet. Studio tells them what is missing.
        """
        return self.terms.accepted and self.locations_confirmed and self.runtime_ready
