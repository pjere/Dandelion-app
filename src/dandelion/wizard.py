"""The Setup Wizard.

Structured so that closing it is never destructive: every page writes its outcome to
`wizard_state.json` before moving on, and re-running the executable resumes at the first
incomplete step. That is not polish — the ENTSO-E token arrives by email days after you ask
for it, so an installation genuinely spans days.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nicegui import app, ui  # noqa: E402

from dandelion import branding  # noqa: E402
from dandelion.paths import Install, check_data_location, default_install  # noqa: E402
from dandelion.ui import choose_window_mode, free_port  # noqa: E402
from dandelion.wizard_state import WizardState  # noqa: E402

STATE_FILE = "wizard_state.json"

PAGES = ("welcome", "locations", "runtime", "credentials", "models", "data", "finish")
PAGE_TITLES = {
    "welcome": "Welcome",
    "locations": "Where things go",
    "runtime": "Install the model",
    "credentials": "Your data accounts",
    "models": "Fitted models",
    "data": "Build the database",
    "finish": "Done",
}


class Wizard:
    def __init__(self, install: Install):
        self.install = install
        self.state_path = install.app_root / STATE_FILE
        self.state = WizardState.load(self.state_path)
        self.current = self.state.next_step
        self.body: ui.column | None = None

    # ------------------------------------------------------------------ plumbing
    def persist(self) -> None:
        self.state.save(self.state_path)

    def go(self, page: str) -> None:
        self.current = page
        self.render()

    def advance(self) -> None:
        index = PAGES.index(self.current)
        self.go(PAGES[min(index + 1, len(PAGES) - 1)])

    # ------------------------------------------------------------------ chrome
    def render(self) -> None:
        assert self.body is not None
        self.body.clear()
        with self.body:
            self._steps()
            getattr(self, f"page_{self.current}")()

    def _steps(self) -> None:
        done_to = PAGES.index(self.state.next_step)
        with ui.row().classes("w-full gap-1 items-center q-mb-md"):
            for index, page in enumerate(PAGES):
                if index < done_to:
                    colour, icon = "text-positive", "check_circle"
                elif page == self.current:
                    colour, icon = "text-primary", "radio_button_checked"
                else:
                    colour, icon = "text-grey-5", "radio_button_unchecked"
                with ui.row().classes("items-center gap-1"):
                    ui.icon(icon).classes(colour)
                    ui.label(PAGE_TITLES[page]).classes(f"{colour} text-caption")
                if index < len(PAGES) - 1:
                    ui.separator().props("vertical").classes("q-mx-xs")

    # ------------------------------------------------------------------ pages
    def page_welcome(self) -> None:
        ui.label(f"{branding.PRODUCT_NAME}").classes("text-h4")
        ui.label(branding.TAGLINE).classes("text-subtitle1 text-grey-7 q-mb-md")

        ui.markdown(
            "This installs the Dandelion model on your machine: a Python environment, the "
            "model code, and the fitted parameters. You will need your own accounts with the "
            "data providers — **the software ships no market data**."
        ).classes("q-mb-sm")

        ui.markdown(
            "**Setting up takes a while, and that is expected.** The ENTSO-E token is issued "
            "by email and can take days to arrive. You can close this window at any point and "
            "reopen it; it remembers where you were."
        ).classes("q-mb-md")

        with ui.scroll_area().classes("w-full h-64 border rounded q-pa-sm bg-grey-1"):
            ui.markdown(f"```\n{branding.DISCLAIMER}\n```")

        accepted = ui.checkbox("I have read and accept these terms",
                              value=self.state.terms.accepted)

        with ui.row().classes("q-mt-md"):
            def accept() -> None:
                if not accepted.value:
                    ui.notify("Please accept the terms to continue.", type="warning")
                    return
                self.state.terms.accepted = True
                self.state.terms.disclaimer_version = branding.DISCLAIMER_VERSION
                self.state.terms.disclaimer_sha256 = branding.disclaimer_sha256()
                from datetime import UTC, datetime

                self.state.terms.at = datetime.now(UTC).isoformat(timespec="seconds")
                self.persist()
                self.advance()

            ui.button("Continue", on_click=accept).props("color=primary")
            ui.button("Quit", on_click=app.shutdown).props("flat")

    def page_locations(self) -> None:
        ui.label("Where things go").classes("text-h5 q-mb-sm")
        ui.markdown(
            "The program itself is small. The **data** is not: a full rebuild reaches about "
            "26 GB, and you may want it on a different drive."
        ).classes("q-mb-md")

        ui.label("Program files").classes("text-subtitle2")
        ui.label(str(self.install.app_root)).classes("text-body2 text-grey-8 q-mb-md")

        data_input = ui.input("Data folder",
                              value=self.state.data_root or str(self.install.data_root)) \
            .classes("w-full")
        verdict_area = ui.column().classes("w-full q-mt-sm")

        def check() -> bool:
            verdict_area.clear()
            verdict = check_data_location(Path(data_input.value))
            with verdict_area:
                for problem in verdict.blocking:
                    with ui.row().classes("items-start no-wrap"):
                        ui.icon("error").classes("text-negative")
                        ui.label(problem).classes("text-body2")
                for warning in verdict.warnings:
                    with ui.row().classes("items-start no-wrap"):
                        ui.icon("warning").classes("text-warning")
                        ui.label(warning).classes("text-body2")
                if verdict.ok and not verdict.warnings:
                    with ui.row().classes("items-center"):
                        ui.icon("check_circle").classes("text-positive")
                        free = f"{verdict.free_gb:.0f} GB free" if verdict.free_gb else ""
                        ui.label(f"Looks good. {free}").classes("text-body2")
            return verdict.ok

        data_input.on("blur", lambda _: check())
        check()

        with ui.row().classes("q-mt-md"):
            def confirm() -> None:
                if not check():
                    ui.notify("Choose a different folder to continue.", type="negative")
                    return
                self.state.data_root = data_input.value
                self.state.app_root = str(self.install.app_root)
                self.state.locations_confirmed = True
                self.persist()
                self.advance()

            ui.button("Continue", on_click=confirm).props("color=primary")
            ui.button("Back", on_click=lambda: self.go("welcome")).props("flat")

    def _placeholder(self, title: str, note: str) -> None:
        ui.label(title).classes("text-h5 q-mb-sm")
        ui.markdown(note).classes("q-mb-md")
        with ui.row():
            ui.button("Continue", on_click=self.advance).props("color=primary")
            ui.button("Back", on_click=lambda: self.go(
                PAGES[max(0, PAGES.index(self.current) - 1)])).props("flat")

    def page_runtime(self) -> None:
        self._placeholder("Install the model",
                          "Downloads the model code and builds its Python environment.")

    def page_credentials(self) -> None:
        self._placeholder("Your data accounts",
                          "RTE, ENTSO-E and Copernicus, each with a **Test** button.")

    def page_models(self) -> None:
        self._placeholder("Fitted models",
                          "Download the reference fits (1.3 GB) or fit them yourself.")

    def page_data(self) -> None:
        self._placeholder("Build the database",
                          "Runs the ingest with your credentials. This part runs overnight.")

    def page_finish(self) -> None:
        ui.label("Ready").classes("text-h5 q-mb-sm")
        ui.markdown("The installation is recorded. Studio opens from the same shortcut.")
        ui.button("Close", on_click=app.shutdown).props("color=primary")


def run(install: Install | None = None, *, force_mode: str | None = None,
        headless_check: bool = False) -> int:
    """Open the wizard. `headless_check` builds the page and exits, for packaging tests."""
    install = install or default_install()
    install.app_root.mkdir(parents=True, exist_ok=True)
    wizard = Wizard(install)

    @ui.page("/")
    def index() -> None:
        ui.add_head_html("<style>body{font-family:Segoe UI,system-ui,sans-serif}</style>")
        with ui.column().classes("q-pa-lg w-full max-w-4xl mx-auto") as body:
            wizard.body = body
        wizard.render()

    if headless_check:
        return 0

    mode = choose_window_mode(force_mode)
    ui.run(
        native=mode.native,
        reload=False,
        show=not mode.native,
        port=free_port(),
        title=f"{branding.PRODUCT_NAME} — Setup",
        window_size=(1040, 780) if mode.native else None,
        favicon="🌱",
    )
    return 0
