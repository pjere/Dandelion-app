"""The install record: what it must contain, and what it must never contain."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion.manifest import (  # noqa: E402
    MANIFEST_VERSION,
    FittedModels,
    InstallManifest,
    build,
    contains_no_secrets,
)
from dandelion.wizard_state import WizardState  # noqa: E402


def a_state() -> WizardState:
    state = WizardState()
    state.terms.accepted = True
    state.terms.at = "2026-08-24T09:00:00+00:00"
    state.terms.disclaimer_version = "1.0"
    state.terms.disclaimer_sha256 = "deadbeef"
    state.credentials = {"entsoe": "passed", "rte": "skipped"}
    return state


def test_absent_manifest_means_run_the_wizard(tmp_path):
    assert InstallManifest.load(tmp_path / "nope.json") is None


def test_round_trip(tmp_path):
    path = tmp_path / "install_manifest.json"
    build(app_version="0.0.1", app_root=tmp_path, data_root=tmp_path / "d",
          tag="v0.1.0", wizard_state=a_state()).save(path)

    loaded = InstallManifest.load(path)
    assert loaded is not None
    assert loaded.code.tag == "v0.1.0"
    assert loaded.terms.sha256 == "deadbeef"
    assert loaded.credentials["entsoe"] == "passed"


def test_a_corrupt_manifest_reads_as_absent(tmp_path):
    path = tmp_path / "install_manifest.json"
    path.write_text("{oops", encoding="utf-8")
    assert InstallManifest.load(path) is None


def test_a_manifest_from_another_version_is_not_trusted(tmp_path):
    path = tmp_path / "install_manifest.json"
    path.write_text(json.dumps({"version": MANIFEST_VERSION + 5, "app_root": "x"}),
                    encoding="utf-8")
    assert InstallManifest.load(path) is None


def test_saving_is_atomic(tmp_path):
    path = tmp_path / "install_manifest.json"
    InstallManifest(app_root=str(tmp_path)).save(path)
    assert [p.name for p in tmp_path.iterdir()] == ["install_manifest.json"]


def test_code_provenance_is_carried_from_the_release_manifest(tmp_path):
    record = build(app_version="0.0.1", app_root=tmp_path, data_root=tmp_path,
                   tag="v0.1.0", code_manifest={
                       "commit": "09a24590758957567331d65b02a33ec205179645",
                       "archive_sha256": "f0e05d66", "lock_sha256": "b91227fd",
                       "packages": ["powersim_core"]})
    assert record.code.commit.startswith("09a2459")
    assert record.code.archive_sha256 == "f0e05d66"


def test_usable_requires_a_code_tag(tmp_path):
    assert not InstallManifest(app_root=str(tmp_path)).usable
    assert build(app_version="0", app_root=tmp_path, data_root=tmp_path, tag="v0.1.0").usable


def test_gaps_name_entsoe_when_it_is_not_confirmed(tmp_path):
    record = build(app_version="0", app_root=tmp_path, data_root=tmp_path, tag="v0.1.0")
    assert any("ENTSO-E" in gap for gap in record.missing_for_a_full_run())


def test_gaps_are_empty_once_everything_is_in_place(tmp_path):
    state = a_state()
    record = build(app_version="0", app_root=tmp_path, data_root=tmp_path, tag="v0.1.0",
                   wizard_state=state, fits=FittedModels(source="downloaded", tag="v0.1.0"))
    assert record.missing_for_a_full_run() == []


def test_fitting_locally_counts_as_having_a_plan(tmp_path):
    state = a_state()
    record = build(app_version="0", app_root=tmp_path, data_root=tmp_path, tag="v0.1.0",
                   wizard_state=state, fits=FittedModels(source="fit-locally"))
    assert not any("fitted models" in gap for gap in record.missing_for_a_full_run())


# --------------------------------------------------------------------------- secrets

def test_credential_outcomes_are_recorded_but_not_values(tmp_path):
    path = tmp_path / "install_manifest.json"
    build(app_version="0", app_root=tmp_path, data_root=tmp_path, tag="v0.1.0",
          wizard_state=a_state()).save(path)
    text = path.read_text(encoding="utf-8")
    assert '"entsoe": "passed"' in text
    for forbidden in ("token", "secret", "password"):
        assert forbidden not in text.lower(), forbidden


def test_a_secret_in_the_record_is_detected(tmp_path):
    record = build(app_version="0", app_root=tmp_path, data_root=tmp_path, tag="v0.1.0")
    record.notes.append("failed using token abcdefghijklmnop")
    assert not contains_no_secrets(record, ["abcdefghijklmnop"])


def test_a_clean_record_passes_the_secret_check(tmp_path):
    record = build(app_version="0", app_root=tmp_path, data_root=tmp_path, tag="v0.1.0",
                   wizard_state=a_state())
    assert contains_no_secrets(record, ["abcdefghijklmnop"])


def test_short_values_do_not_trigger_false_positives(tmp_path):
    """A two-character 'secret' appears in ordinary text; matching it would be noise."""
    record = build(app_version="0", app_root=tmp_path, data_root=tmp_path, tag="v0.1.0")
    assert contains_no_secrets(record, ["v0"])


def test_junctions_are_recorded_so_the_uninstaller_removes_links_not_data(tmp_path):
    record = build(app_version="0", app_root=tmp_path, data_root=tmp_path, tag="v0.1.0",
                   junctions={"code/v0.1.0/data": "D:/dandelion/data"})
    assert record.junctions["code/v0.1.0/data"] == "D:/dandelion/data"
