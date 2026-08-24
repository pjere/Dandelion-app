"""The fitted-model download: what it refuses, and what it verifies."""
from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest
import zstandard

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion import fits as fits_mod  # noqa: E402
from dandelion.download import sha256_file  # noqa: E402


def make_chunk(path: Path, members: dict[str, bytes]) -> Path:
    """A zstd-compressed tar shaped like release_fits.py produces."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    path.write_bytes(zstandard.ZstdCompressor().compress(buffer.getvalue()))
    return path


def test_a_fit_extracts_where_models_dir_resolves(tmp_path):
    """Members are <package>/models/<file>, and models_dir is anchored to the code tree -
    so unpacking there needs no configuration change at all."""
    chunk = make_chunk(tmp_path / "fits.000.tar.zst",
                       {"weathergen/models/fitted.json": b'{"x": 1}'})
    code = tmp_path / "code"
    names = fits_mod.extract_fits([chunk], code)
    assert names == ["weathergen/models/fitted.json"]
    assert (code / "weathergen" / "models" / "fitted.json").read_bytes() == b'{"x": 1}'


def test_several_packages_unpack_together(tmp_path):
    chunk = make_chunk(tmp_path / "c.tar.zst", {
        "weathergen/models/fitted.json": b"a",
        "demand_model/models/calibrated.json": b"b",
    })
    code = tmp_path / "code"
    assert len(fits_mod.extract_fits([chunk], code)) == 2
    assert (code / "demand_model" / "models" / "calibrated.json").is_file()


@pytest.mark.parametrize("bad", [
    "../escape.json",
    "weathergen/notmodels/fitted.json",
    "fitted.json",
])
def test_an_unexpected_path_is_refused_rather_than_unpacked(tmp_path, bad):
    """An archive is downloaded from the internet; it does not get to choose where it lands."""
    chunk = make_chunk(tmp_path / "c.tar.zst", {bad: b"x"})
    with pytest.raises(fits_mod.FitsError):
        fits_mod.extract_fits([chunk], tmp_path / "code")


def test_nothing_is_written_outside_the_code_tree(tmp_path):
    chunk = make_chunk(tmp_path / "c.tar.zst", {"../../evil.json": b"x"})
    code = tmp_path / "code"
    with pytest.raises(fits_mod.FitsError):
        fits_mod.extract_fits([chunk], code)
    assert not (tmp_path / "evil.json").exists()


# ------------------------------------------------------------------------ verification

def test_verify_reports_a_missing_artifact(tmp_path):
    plan = fits_mod.FitsPlan(tag="v0.1.0", artifacts=[
        {"package": "weathergen", "name": "fitted.json", "sha256": "abc"}])
    assert any("missing" in p for p in fits_mod.verify_installed(plan, tmp_path))


def test_verify_reports_a_checksum_mismatch(tmp_path):
    target = tmp_path / "weathergen" / "models"
    target.mkdir(parents=True)
    (target / "fitted.json").write_bytes(b"content")
    plan = fits_mod.FitsPlan(tag="v0.1.0", artifacts=[
        {"package": "weathergen", "name": "fitted.json", "sha256": "not-the-hash"}])
    assert any("checksum" in p for p in fits_mod.verify_installed(plan, tmp_path))


def test_verify_passes_on_a_correct_install(tmp_path):
    target = tmp_path / "weathergen" / "models"
    target.mkdir(parents=True)
    written = target / "fitted.json"
    written.write_bytes(b"content")
    plan = fits_mod.FitsPlan(tag="v0.1.0", artifacts=[
        {"package": "weathergen", "name": "fitted.json", "sha256": sha256_file(written)}])
    assert fits_mod.verify_installed(plan, tmp_path) == []
    assert fits_mod.already_installed(plan, tmp_path)


def test_an_empty_plan_is_not_treated_as_installed(tmp_path):
    """Otherwise a manifest that listed nothing would look like success."""
    assert not fits_mod.already_installed(fits_mod.FitsPlan(tag="v0.1.0"), tmp_path)


def test_plan_reports_the_download_size():
    plan = fits_mod.FitsPlan(tag="v0.1.0", chunks=[{"bytes": 1000}, {"bytes": 2000}])
    assert plan.download_bytes == 3000


def test_the_cost_of_fitting_yourself_is_stated_plainly():
    text = fits_mod.FIT_YOURSELF_COST.lower()
    assert "copernicus" in text and "hours" in text and "differ" in text


# ------------------------------------------------------------------- retry policy

def test_a_missing_file_is_not_retried():
    """404 will not become 200 by asking five more times; the user just waits."""
    import urllib.error

    from dandelion.download import _is_retryable

    missing = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    assert not _is_retryable(missing)


def test_rate_limits_and_server_errors_are_retried():
    import urllib.error

    from dandelion.download import _is_retryable

    for code in (429, 500, 503):
        assert _is_retryable(urllib.error.HTTPError("u", code, "x", {}, None))


def test_connection_failures_are_retried():
    from dandelion.download import _is_retryable

    assert _is_retryable(ConnectionResetError("dropped"))


def test_access_denied_is_not_retried():
    """A private repository will not become public on the second attempt."""
    import urllib.error

    from dandelion.download import _is_retryable

    assert not _is_retryable(urllib.error.HTTPError("u", 403, "Forbidden", {}, None))
