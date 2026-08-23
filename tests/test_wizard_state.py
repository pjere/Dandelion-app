"""What the wizard remembers between runs.

An installation genuinely spans days — the ENTSO-E token arrives by email, the rebuild runs
overnight — so abandoning the wizard and coming back must be ordinary, not a restart.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion.wizard_state import STATE_VERSION, WizardState  # noqa: E402


def test_a_missing_file_starts_a_fresh_run(tmp_path):
    state = WizardState.load(tmp_path / "absent.json")
    assert not state.terms.accepted and state.next_step == "welcome"


def test_state_survives_a_round_trip(tmp_path):
    path = tmp_path / "wizard_state.json"
    state = WizardState()
    state.terms.accepted = True
    state.terms.disclaimer_sha256 = "abc"
    state.data_root = "D:/dandelion"
    state.locations_confirmed = True
    state.save(path)

    reloaded = WizardState.load(path)
    assert reloaded.terms.accepted and reloaded.terms.disclaimer_sha256 == "abc"
    assert reloaded.data_root == "D:/dandelion" and reloaded.locations_confirmed


def test_saving_leaves_no_temporary_file_behind(tmp_path):
    """The write is atomic so a crash mid-save cannot produce half a state."""
    path = tmp_path / "wizard_state.json"
    WizardState().save(path)
    assert [p.name for p in tmp_path.iterdir()] == ["wizard_state.json"]


def test_a_corrupt_file_starts_fresh_rather_than_crashing(tmp_path):
    path = tmp_path / "wizard_state.json"
    path.write_text("{not json", encoding="utf-8")
    assert WizardState.load(path).next_step == "welcome"


def test_a_state_from_another_version_is_not_half_restored(tmp_path):
    """Half-restoring an installation is worse than starting the wizard again."""
    path = tmp_path / "wizard_state.json"
    path.write_text(json.dumps({"version": STATE_VERSION + 99, "runtime_ready": True}),
                    encoding="utf-8")
    assert WizardState.load(path).runtime_ready is False


def test_unknown_fields_are_ignored(tmp_path):
    path = tmp_path / "wizard_state.json"
    path.write_text(json.dumps({"version": STATE_VERSION, "data_root": "D:/x",
                                "invented_field": 1}), encoding="utf-8")
    assert WizardState.load(path).data_root == "D:/x"


def test_resume_lands_on_the_first_incomplete_step():
    state = WizardState()
    assert state.next_step == "welcome"
    state.terms.accepted = True
    assert state.next_step == "locations"
    state.locations_confirmed = True
    assert state.next_step == "runtime"
    state.runtime_ready = True
    assert state.next_step == "credentials"
    state.credentials = {"entsoe": "passed"}
    assert state.next_step == "models"
    state.fits_choice = "download"
    assert state.next_step == "data"
    state.data_choice = "later"
    assert state.next_step == "finish"


def test_an_installation_can_finish_without_working_credentials():
    """ENTSO-E can take days. A user with a provisioned runtime has a real installation;
    Studio tells them what is still missing."""
    state = WizardState(terms=WizardState().terms)
    state.terms.accepted = True
    state.locations_confirmed = True
    state.runtime_ready = True
    assert state.can_finish


def test_an_installation_cannot_finish_without_a_runtime():
    state = WizardState()
    state.terms.accepted = True
    state.locations_confirmed = True
    assert not state.can_finish


def test_credential_state_defaults_to_untested():
    assert WizardState().credential_state("rte") == "untested"


def test_no_secret_is_ever_written_to_the_state_file(tmp_path):
    """Secrets belong in Windows Credential Manager. The state records only outcomes."""
    path = tmp_path / "wizard_state.json"
    state = WizardState()
    state.credentials = {"entsoe": "passed"}
    state.save(path)
    text = path.read_text(encoding="utf-8")
    for forbidden in ("token", "secret", "password", "key"):
        assert forbidden not in text.lower(), forbidden
