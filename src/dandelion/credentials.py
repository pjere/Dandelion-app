"""The user's API credentials: stored in Windows Credential Manager, tested for real.

The product ships no data, so every user brings their own accounts. That makes this screen
the critical path of the whole installation — and the place where a vague "saved!" would be
most damaging, because the consequence of a wrong token is not an error here but a failed
ingest hours later.

So each credential is **tested against the provider that issued it**, inside the release
environment, using the same libraries the model itself uses. And each test reports what it
actually verified: `status()` on the Copernicus API proves the service is reachable and the
URL is right, and says so, rather than implying the key was checked when it was not.

Secrets go to Windows Credential Manager through `keyring`. They are never written to a file,
never placed in a config, never logged, and never included in an exported bundle. Upstream
reads them from environment variables (`os.getenv`), and `load_dotenv` runs with
`override=False`, so injecting them into a subprocess wins over any `.env` that might exist.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SERVICE = "Dandelion Studio"


@dataclass(frozen=True)
class Field:
    env: str
    label: str
    secret: bool = True
    default: str = ""


@dataclass(frozen=True)
class Credential:
    key: str
    label: str
    why: str
    fields: tuple[Field, ...]
    signup_url: str
    steps: tuple[str, ...]
    required_for: str
    #: What the product can still do without it.
    without: str


CREDENTIALS: tuple[Credential, ...] = (
    Credential(
        key="rte",
        label="RTE",
        why="French generation, load and outage data, at plant level.",
        fields=(Field("RTE_CLIENT_ID", "Client ID", secret=False),
                Field("RTE_CLIENT_SECRET", "Client secret")),
        signup_url="https://data.rte-france.com",
        steps=(
            "Create a free account at data.rte-france.com.",
            "Create an 'application' in your account.",
            "Subscribe that application to each API listed in config/rte_catalog.yaml — "
            "this is the step people miss, and a subscription-less application returns "
            "empty data rather than an error.",
            "Copy the application's client ID and client secret here.",
        ),
        required_for="French plant-level data",
        without="Everything else still works; French generation detail will be missing.",
    ),
    Credential(
        key="entsoe",
        label="ENTSO-E Transparency",
        why="Prices, load, generation and cross-border flows for all thirteen zones.",
        fields=(Field("ENTSOE_TOKEN", "API token"),),
        signup_url="https://transparency.entsoe.eu",
        steps=(
            "Create a free account at transparency.entsoe.eu.",
            "Email transparency@entsoe.eu asking for API access, from the address you "
            "registered with.",
            "Wait for their reply — this is not instant and can take several days. You can "
            "close this installer and come back; it will remember where you were.",
            "Paste the token they send you here.",
        ),
        required_for="the core price and load history",
        without="The model cannot be built. This is the one credential nothing works without.",
    ),
    Credential(
        key="cds",
        label="Copernicus Climate Data Store",
        why="ERA5 reanalysis and CMIP6 climate deltas.",
        fields=(Field("CDSAPI_URL", "API URL", secret=False,
                      default="https://cds.climate.copernicus.eu/api"),
                Field("CDSAPI_KEY", "API key")),
        signup_url="https://cds.climate.copernicus.eu",
        steps=(
            "Create a free account at cds.climate.copernicus.eu.",
            "Accept the licence for the ERA5 and CMIP6 datasets on the website — downloads "
            "fail with a licence error until you do.",
            "Copy the API key from your profile page here.",
        ),
        required_for="re-fitting the weather generator yourself",
        without=(
            "Not needed if you download the fitted models, which is the default: nothing "
            "then pulls ERA5. Only required if you want to re-fit from scratch."
        ),
    ),
)


def by_key(key: str) -> Credential:
    for credential in CREDENTIALS:
        if credential.key == key:
            return credential
    raise KeyError(f"no such credential: {key!r}")


# --------------------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------------------

def _keyring():
    import keyring

    return keyring


def save(env: str, value: str) -> None:
    """Store one value in Windows Credential Manager."""
    _keyring().set_password(SERVICE, env, value)


def load(env: str) -> str | None:
    try:
        return _keyring().get_password(SERVICE, env)
    except Exception:                                    # noqa: BLE001 - a locked keyring
        return None


def forget(env: str) -> None:
    try:
        _keyring().delete_password(SERVICE, env)
    except Exception:                                    # noqa: BLE001 - absent is fine
        pass


def stored_environment() -> dict[str, str]:
    """Every stored credential, as the environment variables upstream reads.

    This is the ONLY place secrets enter a job's environment. Nothing writes them to disk.
    """
    out: dict[str, str] = {}
    for credential in CREDENTIALS:
        for field_ in credential.fields:
            value = load(field_.env)
            if value:
                out[field_.env] = value
    return out


def status() -> dict[str, bool]:
    """Which credentials have every field filled in."""
    stored = stored_environment()
    return {c.key: all(f.env in stored for f in c.fields) for c in CREDENTIALS}


# --------------------------------------------------------------------------------------
# testing
# --------------------------------------------------------------------------------------

@dataclass
class TestResult:
    ok: bool
    summary: str
    #: Exactly what was proven, so the UI never implies more than it checked.
    verified: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    detail: str = ""


#: Run inside the release environment. Each prints one JSON object and exits.
_PROBES: dict[str, str] = {
    "rte": r"""
import json, sys
try:
    from pricemodeling.config import load_settings
    from pricemodeling.rte.auth import TokenManager
    cid, secret = load_settings().rte_credentials
    token = TokenManager(cid, secret).token()
    print(json.dumps({"ok": True, "summary": f"Signed in to RTE (token length {len(token)}).",
                      "verified": ["The client ID and secret are valid.",
                                   "RTE issued an access token."],
                      "unverified": ["Whether your application is subscribed to each API "
                                     "in rte_catalog.yaml. An unsubscribed application "
                                     "returns empty data rather than an error."]}))
except Exception as exc:
    print(json.dumps({"ok": False, "summary": "RTE refused these credentials.",
                      "detail": f"{type(exc).__name__}: {exc}"[:400]}))
""",
    "entsoe": r"""
import json, os, sys
try:
    import pandas as pd
    from entsoe import EntsoePandasClient
    client = EntsoePandasClient(api_key=os.environ["ENTSOE_TOKEN"])
    end = pd.Timestamp.utcnow().tz_convert("Europe/Brussels").normalize()
    start = end - pd.Timedelta(days=1)
    series = client.query_day_ahead_prices("FR", start=start, end=end)
    print(json.dumps({"ok": True,
                      "summary": f"ENTSO-E returned {len(series)} French price hours.",
                      "verified": ["The token is valid.",
                                   "Day-ahead prices can be downloaded."]}))
except Exception as exc:
    message = f"{type(exc).__name__}: {exc}"[:400]
    hint = ""
    if "401" in message or "Unauthorized" in message:
        hint = " The token was rejected - check it was copied whole."
    print(json.dumps({"ok": False, "summary": "ENTSO-E refused this token." + hint,
                      "detail": message}))
""",
    "cds": r"""
import json, os
try:
    import cdsapi
    client = cdsapi.Client(url=os.environ.get("CDSAPI_URL"), key=os.environ.get("CDSAPI_KEY"),
                           quiet=True, verify=True)
    info = client.status()
    verified = ["The Copernicus service is reachable.", "The API URL is correct."]
    unverified = ["Whether the key itself is accepted - Copernicus does not expose a cheap "
                  "way to check that, so it is proven the first time data is downloaded.",
                  "Whether you have accepted the ERA5 and CMIP6 dataset licences."]
    print(json.dumps({"ok": True, "summary": "Reached the Copernicus Climate Data Store.",
                      "verified": verified, "unverified": unverified,
                      "detail": str(info)[:200]}))
except Exception as exc:
    print(json.dumps({"ok": False, "summary": "Could not reach the Copernicus API.",
                      "detail": f"{type(exc).__name__}: {exc}"[:400]}))
""",
}


def job_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """A clean environment for an upstream job, with credentials injected.

    Deliberately explicit rather than inherited: fourteen `DISPATCH_*` variables change model
    results, and one left over in a user's shell would silently alter their prices.
    """
    env = dict(base if base is not None else os.environ)
    for name in list(env):
        if name.startswith("DISPATCH_"):
            del env[name]
    env.update(stored_environment())
    env["PYTHONUTF8"] = "1"
    env.pop("POWERSIM_NO_PROGRESS", None)               # progress lines are how the GUI reports
    return env


def test_credential(key: str, python: Path, code_dir: Path,
                    values: dict[str, str] | None = None,
                    timeout: int = 120) -> TestResult:
    """Ask the provider whether these credentials work.

    `values` tests values not yet saved, so the wizard can validate before storing.
    """
    credential = by_key(key)
    env = job_environment()
    if values:
        env.update({k: v for k, v in values.items() if v})

    missing = [f.label for f in credential.fields if not env.get(f.env)]
    if missing:
        return TestResult(False, f"Fill in {', '.join(missing)} first.")

    try:
        proc = subprocess.run(
            [str(python), "-X", "utf8", "-c", _PROBES[key]],
            cwd=str(code_dir), env=env, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return TestResult(False, f"{credential.label} did not answer within {timeout} seconds.",
                          detail="The service may be down, or a proxy may be blocking it.")
    except OSError as exc:
        return TestResult(False, "Could not run the check.", detail=str(exc))

    line = next((ln for ln in reversed((proc.stdout or "").splitlines()) if ln.startswith("{")),
                None)
    if not line:
        return TestResult(
            False, f"The {credential.label} check did not report a result.",
            detail=(proc.stderr or proc.stdout or "").strip()[-400:] or f"exit {proc.returncode}",
        )
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return TestResult(False, "Unreadable result from the check.", detail=line[:300])

    return TestResult(
        ok=bool(payload.get("ok")),
        summary=payload.get("summary", ""),
        verified=list(payload.get("verified", [])),
        unverified=list(payload.get("unverified", [])),
        detail=str(payload.get("detail", "")),
    )


def scrub(text: str) -> str:
    """Remove any stored secret from text about to be logged or exported."""
    cleaned = text
    for credential in CREDENTIALS:
        for field_ in credential.fields:
            if not field_.secret:
                continue
            value = load(field_.env)
            if value and len(value) >= 8:
                cleaned = cleaned.replace(value, f"<{field_.env} redacted>")
    return cleaned


if __name__ == "__main__":                               # pragma: no cover - manual probe
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for name, present in status().items():
        print(f"{name:8s} {'stored' if present else 'not stored'}")
