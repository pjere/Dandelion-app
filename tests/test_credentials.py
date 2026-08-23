"""Credential storage, the job environment, and honesty about what a test proved.

The product ships no data, so these three accounts are the critical path of every
installation. The failure this file guards against is a wrong credential accepted quietly:
the consequence is not an error on this screen but a failed ingest hours later.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion import credentials as cr  # noqa: E402


class FakeKeyring:
    """Stands in for Windows Credential Manager so tests never touch the real vault."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        del self.store[(service, name)]


@pytest.fixture
def vault(monkeypatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(cr, "_keyring", lambda: fake)
    return fake


# ------------------------------------------------------------------------ the catalogue

def test_the_three_accounts_are_declared():
    assert {c.key for c in cr.CREDENTIALS} == {"rte", "entsoe", "cds"}


def test_every_credential_explains_itself_and_how_to_get_one():
    for credential in cr.CREDENTIALS:
        assert credential.why and credential.signup_url.startswith("https://")
        assert len(credential.steps) >= 3
        assert credential.without, credential.key


def test_the_entsoe_walkthrough_warns_about_the_email_wait():
    """It is granted by a human replying to email; users must know before they wait."""
    steps = " ".join(cr.by_key("entsoe").steps).lower()
    assert "email" in steps and ("days" in steps or "not instant" in steps)


def test_the_rte_walkthrough_warns_about_api_subscription():
    """An unsubscribed RTE application returns empty data rather than an error."""
    assert "subscribe" in " ".join(cr.by_key("rte").steps).lower()


def test_cds_is_described_as_optional_when_fits_are_downloaded():
    assert "fitted models" in cr.by_key("cds").without


def test_unknown_credential_is_loud():
    with pytest.raises(KeyError):
        cr.by_key("nope")


# ---------------------------------------------------------------------------- storage

def test_round_trip_and_forget(vault):
    cr.save("ENTSOE_TOKEN", "abc123")
    assert cr.load("ENTSOE_TOKEN") == "abc123"
    cr.forget("ENTSOE_TOKEN")
    assert cr.load("ENTSOE_TOKEN") is None


def test_forgetting_something_absent_is_not_an_error(vault):
    cr.forget("ENTSOE_TOKEN")


def test_a_locked_vault_reads_as_absent_rather_than_crashing(monkeypatch):
    class Broken:
        def get_password(self, *_a):
            raise RuntimeError("vault locked")

    monkeypatch.setattr(cr, "_keyring", lambda: Broken())
    assert cr.load("ENTSOE_TOKEN") is None


def test_status_requires_every_field_of_a_credential(vault):
    cr.save("RTE_CLIENT_ID", "id-only")
    assert cr.status()["rte"] is False
    cr.save("RTE_CLIENT_SECRET", "secret")
    assert cr.status()["rte"] is True


def test_stored_environment_uses_the_names_upstream_reads(vault):
    cr.save("ENTSOE_TOKEN", "t")
    cr.save("CDSAPI_KEY", "k")
    assert cr.stored_environment() == {"ENTSOE_TOKEN": "t", "CDSAPI_KEY": "k"}


# -------------------------------------------------------------------- job environment

def test_job_environment_injects_credentials(vault):
    cr.save("ENTSOE_TOKEN", "tok")
    assert cr.job_environment({})["ENTSOE_TOKEN"] == "tok"


def test_job_environment_strips_dispatch_switches(vault):
    """Fourteen DISPATCH_* variables change model results; one inherited from a shell would
    alter a user's prices with nothing to show for it."""
    env = cr.job_environment({"DISPATCH_FLEX_VOM": "99", "DISPATCH_TRACE_SOLVES": "1",
                              "PATH": "keep"})
    assert "DISPATCH_FLEX_VOM" not in env and "DISPATCH_TRACE_SOLVES" not in env
    assert env["PATH"] == "keep"


def test_job_environment_forces_utf8(vault):
    assert cr.job_environment({})["PYTHONUTF8"] == "1"


def test_job_environment_never_silences_progress(vault):
    """POWERSIM_NO_PROGRESS would remove the only signal the GUI has for a four-hour run."""
    assert "POWERSIM_NO_PROGRESS" not in cr.job_environment({"POWERSIM_NO_PROGRESS": "1"})


# ---------------------------------------------------------------------------- scrubbing

def test_scrub_redacts_stored_secrets(vault):
    cr.save("ENTSOE_TOKEN", "super-secret-token-value")
    cleaned = cr.scrub("failed with token super-secret-token-value in the URL")
    assert "super-secret-token-value" not in cleaned
    assert "ENTSOE_TOKEN redacted" in cleaned


def test_scrub_leaves_non_secret_fields_alone(vault):
    """The RTE client ID is an identifier, not a secret; redacting it hides useful
    diagnostics for no benefit."""
    cr.save("RTE_CLIENT_ID", "an-identifier-value")
    assert "an-identifier-value" in cr.scrub("client an-identifier-value failed")


def test_scrub_ignores_very_short_secrets(vault):
    """Redacting a two-character value would blank out unrelated text."""
    cr.save("ENTSOE_TOKEN", "ab")
    assert cr.scrub("a fabulous absolute value") == "a fabulous absolute value"


# ------------------------------------------------------------------------------ testing

def test_testing_without_values_asks_for_them_rather_than_calling_out(vault, tmp_path):
    result = cr.test_credential("entsoe", tmp_path / "python.exe", tmp_path)
    assert not result.ok and "Fill in" in result.summary


def test_a_probe_exists_for_every_credential():
    assert set(cr._PROBES) == {c.key for c in cr.CREDENTIALS}


def test_the_cds_probe_states_what_it_cannot_verify():
    """status() is unauthenticated: it proves reachability, not that the key is accepted."""
    probe = cr._PROBES["cds"]
    assert "unverified" in probe
    assert "cheap way to check" in probe or "proven the first time" in probe


def test_the_rte_probe_flags_the_subscription_gap():
    assert "subscribed" in cr._PROBES["rte"]
