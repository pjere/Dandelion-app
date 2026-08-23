"""The product identity and the text users accept before installing (D4).

The disclaimer is the one piece of user-facing text with legal weight, so the parts that carry
that weight are pinned here. A future edit that drops one of them fails the suite rather than
shipping quietly.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dandelion import branding  # noqa: E402


def test_product_identity():
    assert branding.PRODUCT_NAME == "Dandelion Studio"
    assert "@" in branding.SUPPORT_EMAIL


def test_the_disclaimer_says_scenarios_are_not_forecasts():
    text = branding.DISCLAIMER.lower()
    assert "not a forecast" in text
    assert "scenario" in text


def test_the_disclaimer_refuses_the_advice_reading():
    text = branding.DISCLAIMER.lower()
    for phrase in ("investment", "financial", "advice"):
        assert phrase in text, phrase
    assert "does not provide investment" in text


def test_the_disclaimer_covers_third_party_data_terms():
    """Users pull data under their own credentials, so provider terms are their obligation."""
    text = branding.DISCLAIMER.lower()
    assert "terms of use" in text
    assert "redistribution" in text
    for provider in ("rte", "entso-e", "copernicus"):
        assert provider in text, provider


def test_the_disclaimer_states_no_warranty_and_limits_liability():
    text = branding.DISCLAIMER.lower()
    assert '"as is"' in text
    assert "without warranty" in text
    assert "liability" in text


def test_the_disclaimer_states_nothing_is_sent_anywhere():
    assert "sent anywhere" in branding.DISCLAIMER.lower()


def test_the_disclaimer_carries_the_product_name_and_support_address():
    assert branding.PRODUCT_NAME in branding.DISCLAIMER
    assert branding.SUPPORT_EMAIL in branding.DISCLAIMER


def test_acceptance_hash_is_stable_and_content_bound():
    """Acceptance means nothing if nobody can say what was accepted."""
    first = branding.disclaimer_sha256()
    assert first == branding.disclaimer_sha256()
    assert len(first) == 64

    original = branding.DISCLAIMER
    try:
        branding.DISCLAIMER = original + "\nAn added clause.\n"
        assert branding.disclaimer_sha256() != first
    finally:
        branding.DISCLAIMER = original
    assert branding.disclaimer_sha256() == first


def test_disclaimer_version_is_declared():
    assert branding.DISCLAIMER_VERSION


# ---------------------------------------------------------------------- licence (GPL-3.0)

def test_licence_is_declared():
    assert branding.LICENSE_SPDX == "GPL-3.0-or-later"
    assert "General Public License" in branding.LICENSE_NAME


def test_licence_file_is_the_full_gpl3_text():
    """A truncated or paraphrased licence grants nothing. Check it structurally."""
    import re

    text = (Path(__file__).resolve().parents[1] / "LICENSE").read_text(encoding="utf-8")
    assert text.splitlines()[0].strip() == "GNU GENERAL PUBLIC LICENSE"
    assert "Version 3, 29 June 2007" in text
    assert "TERMS AND CONDITIONS" in text
    assert "Preamble" in text
    assert "How to Apply These Terms to Your New Programs" in text
    sections = {int(m) for m in re.findall(r"^\s{2}(\d{1,2})\. ", text, re.MULTILINE)}
    assert sections == set(range(18)), f"missing sections: {set(range(18)) - sections}"


def test_the_disclaimer_states_the_licence_and_does_not_override_it():
    text = branding.DISCLAIMER
    assert branding.LICENSE_SPDX in text
    assert "free software" in text.lower()
    # the terms of use must not appear to take away what the licence grants
    assert "where the two differ, the licence governs" in text.lower()


def test_the_disclaimer_carries_a_copyright_notice():
    assert f"Copyright (C) {branding.COPYRIGHT_YEAR}" in branding.DISCLAIMER
    assert branding.COPYRIGHT_HOLDER in branding.DISCLAIMER


def test_the_disclaimer_separates_app_licence_from_model_and_data():
    """GPL covers this wrapper only; the model and the data have their own terms."""
    text = branding.DISCLAIMER.lower()
    assert "the model this software drives" in text
    assert "covered separately" in text
