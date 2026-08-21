"""Product identity and the text users must accept before installing (decision D4).

THE ONLY PLACE the owner's contact details may appear. `release_tools/path_guard.py` forbids
the owner's name and domain anywhere in this repository, because an address baked into a source
file is usually a leak rather than a decision. Here it is a decision, so exactly one line
carries an explicit allow marker and this module is the single import point for it. Nothing
else in the tree — not even a comment — should spell the address out.

The disclaimer below is written in plain language, not by a lawyer, and has not been reviewed
by one. It states the things that are true of this model and would matter to someone who
misread its output. If Dandelion Studio goes beyond a small circle of known users, have a
lawyer look at it — particularly the liability paragraph, whose enforceability varies by
jurisdiction.
"""
from __future__ import annotations

#: Shown in the title bar, the wizard, the About page and the installer.
PRODUCT_NAME = "Dandelion Studio"

#: Where a stuck user goes. Owner decision, 2026-08-20.
SUPPORT_EMAIL = "pierre@pjeremie.org"      # path-guard: allow

#: One line, for window titles and the installer's header.
TAGLINE = "European electricity price scenarios"

#: Presented on the wizard's first page. Acceptance is required to continue, recorded in the
#: install manifest with a timestamp and the hash of the text that was actually shown — so a
#: later change to this file cannot retroactively claim someone agreed to it.
DISCLAIMER_VERSION = "1.0"

DISCLAIMER = f"""\
{PRODUCT_NAME} — terms of use

WHAT THIS SOFTWARE PRODUCES

{PRODUCT_NAME} computes *scenarios* for European electricity spot prices: what prices would
look like if a particular set of assumptions held. A scenario is not a forecast, a prediction,
or a statement about what will happen. Two reasonable sets of assumptions can produce very
different prices, and the software will compute both without preferring either.

Every result depends on assumptions you choose and on data you obtain yourself. Changing an
assumption changes the answer. The software does not know which assumptions are right.

NOT ADVICE

{PRODUCT_NAME} does not provide investment, financial, trading, legal or tax advice, and its
output must not be relied on as any of those. It is a modelling tool. Decisions about buying,
selling, hedging, contracting or investing are yours, and you are responsible for them. If you
need advice, consult someone qualified and regulated to give it.

DATA YOU OBTAIN YOURSELF

This software ships no market data. It helps you download data from third parties — RTE,
ENTSO-E, Météo-France, Elexon, the Copernicus Climate Data Store, national plant registries and
others — using credentials you register in your own name.

You are responsible for complying with each provider's terms of use, including any limits on
redistribution and on commercial use. Your credentials stay on your machine, in the Windows
Credential Manager; {PRODUCT_NAME} sends them only to the provider they belong to. Nothing
about your data, your assumptions or your results is sent anywhere.

{PRODUCT_NAME} is not affiliated with, endorsed by, or connected to any of those providers.

ACCURACY

The model is a simplification of a complex physical and economic system. It contains
approximations, calibration choices and known limitations, and it may contain errors. Results
have not been validated for any particular purpose, and no output should be treated as accurate
merely because it is precise.

NO WARRANTY

The software is provided "as is", without warranty of any kind, express or implied, including
any warranty of merchantability, fitness for a particular purpose, or non-infringement.

LIMITATION OF LIABILITY

To the fullest extent permitted by applicable law, the authors and copyright holders are not
liable for any claim, damages or other liability — including lost profits, lost opportunity or
business interruption — arising from the software or from the use of, or reliance on, anything
it produces. Some jurisdictions do not allow limits on certain liabilities, so parts of this
paragraph may not apply to you.

SUPPORT

{PRODUCT_NAME} is provided without any commitment to support, maintenance or updates. Questions
and bug reports are welcome at {SUPPORT_EMAIL}, with no guarantee of a reply.

By continuing, you confirm that you have read and accepted these terms.
"""


def disclaimer_sha256() -> str:
    """Hash of the exact text shown to the user, recorded on acceptance.

    Acceptance means little if nobody can say what was accepted; editing this file later must
    not silently rewrite what a past user agreed to.
    """
    import hashlib

    return hashlib.sha256(DISCLAIMER.encode("utf-8")).hexdigest()
