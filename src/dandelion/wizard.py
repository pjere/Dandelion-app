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

from dandelion import branding, credentials  # noqa: E402
from dandelion.background import BackgroundTask, TaskView  # noqa: E402
from dandelion.credentials import CREDENTIALS  # noqa: E402
from dandelion.paths import Install, check_data_location, default_install  # noqa: E402
from dandelion.ui import choose_window_mode, free_port  # noqa: E402
from dandelion.wizard_state import WizardState  # noqa: E402

STATE_FILE = "wizard_state.json"

#: The release this build of the installer knows how to fetch. Pinned rather than "latest":
#: an installer and a code release are qualified together, and silently picking up a newer
#: tag would install a combination nobody tested.
DEFAULT_TAG = "v0.1.0"

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
        self.task: BackgroundTask | None = None
        self.view = TaskView()
        self.cred_tasks: dict[str, BackgroundTask] = {}

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
        ui.label("Install the model").classes("text-h5 q-mb-sm")
        ui.markdown(
            f"This downloads model release **{DEFAULT_TAG}**, installs a private copy of "
            "Python, and builds the environment the model runs in. Nothing already on your "
            "machine is changed, and nothing needs administrator rights."
        ).classes("q-mb-sm")
        ui.markdown(
            "It takes **about eight minutes** and downloads roughly 1 GB. You can leave it."
        ).classes("text-body2 text-grey-7 q-mb-md")

        if self.state.runtime_ready and self.task is None:
            with ui.card().classes("w-full bg-green-1 q-mb-md"):
                with ui.row().classes("items-center"):
                    ui.icon("check_circle").classes("text-positive text-h5")
                    label = f"{self.state.code_tag} is installed and verified."
                    ui.label(label).classes("text-body1")
            with ui.row():
                ui.button("Continue", on_click=self.advance).props("color=primary")
                ui.button("Reinstall", on_click=self._start_provision).props("flat")
                ui.button("Back", on_click=lambda: self.go("locations")).props("flat")
            return

        progress_area = ui.column().classes("w-full q-mb-md")
        buttons = ui.row().classes("q-mt-md")

        def paint() -> None:
            progress_area.clear()
            with progress_area:
                if self.view.current and not self.view.done:
                    with ui.row().classes("items-center w-full"):
                        ui.spinner(size="sm")
                        ui.label(self.view.current).classes("text-body2")
                    if self.view.fraction is not None:
                        ui.linear_progress(value=self.view.fraction).classes("w-full")
                    else:
                        # No honest percentage exists for this step, so none is shown.
                        ui.linear_progress().props("indeterminate").classes("w-full")
                for done in self.view.steps[-14:]:
                    ok = done.ok is not False
                    with ui.row().classes("items-center no-wrap"):
                        icon = "check_circle" if ok else "error"
                        colour = "text-positive" if ok else "text-negative"
                        ui.icon(icon).classes(colour)
                        ui.label(done.message).classes("text-body2")
                if self.view.error:
                    with ui.card().classes("w-full bg-red-1"):
                        ui.label("Installation stopped").classes("text-subtitle2 text-negative")
                        ui.label(self.view.error).classes("text-body2 whitespace-pre-wrap")

            buttons.clear()
            with buttons:
                if self.task is not None and self.task.running:
                    ui.label("Working...").classes("text-grey-7")
                elif self.view.error:
                    ui.button("Try again", on_click=self._start_provision).props("color=primary")
                    ui.button("Back", on_click=lambda: self.go("locations")).props("flat")
                elif self.state.runtime_ready:
                    ui.button("Continue", on_click=self.advance).props("color=primary")
                else:
                    ui.button("Install now", on_click=self._start_provision).props("color=primary")
                    ui.button("Back", on_click=lambda: self.go("locations")).props("flat")

        def poll() -> None:
            if self.task is None:
                return
            if self.view.apply_all(self.task.drain()):
                if self.task.finished and self.task.succeeded and not self.state.runtime_ready:
                    self.state.runtime_ready = True
                    self.state.code_tag = DEFAULT_TAG
                    self.persist()
                paint()

        ui.timer(0.3, poll)
        paint()

    def _configured_install(self) -> Install:
        return Install(app_root=self.install.app_root,
                       data_root=Path(self.state.data_root or self.install.data_root))

    def _start_provision(self) -> None:
        from dandelion.provision import provision

        self.view = TaskView()
        task = BackgroundTask("Installation")
        self.task = task
        install = self._configured_install()

        def work():
            return provision(
                install, DEFAULT_TAG,
                listener=lambda step: task.step(step.title, step.detail or "", step.ok),
                on_download=lambda pr: task.progress(f"Downloading {pr.name}", pr.fraction),
            )

        task.start(work)
        self.render()

    def page_credentials(self) -> None:
        ui.label("Your data accounts").classes("text-h5 q-mb-sm")
        ui.markdown(
            "Dandelion Studio ships no market data. It downloads what it needs using **your** "
            "accounts, so the data arrives under your own terms of use with each provider."
        ).classes("q-mb-sm")
        ui.markdown(
            "Credentials are stored in **Windows Credential Manager**, never in a file, and "
            "are sent only to the provider they belong to."
        ).classes("text-body2 text-grey-7 q-mb-md")

        if not self.state.runtime_ready:
            with ui.card().classes("w-full bg-amber-1 q-mb-md"):
                ui.label("You can enter credentials now, but testing them needs the model "
                         "installed first - the checks run inside it.").classes("text-body2")

        for credential in CREDENTIALS:
            self._credential_card(credential)

        def carry_on() -> None:
            for cred in CREDENTIALS:
                self.state.credentials.setdefault(cred.key, "untested")
            self.persist()
            self.advance()

        with ui.row().classes("q-mt-md"):
            ui.button("Continue", on_click=carry_on).props("color=primary")
            ui.button("Back", on_click=lambda: self.go("runtime")).props("flat")

    def _credential_card(self, credential) -> None:
        state = self.state.credential_state(credential.key)
        stored = credentials.status().get(credential.key, False)

        with ui.card().classes("w-full q-mb-md"):
            with ui.row().classes("items-center w-full justify-between"):
                with ui.row().classes("items-center"):
                    icon, colour = {
                        "passed": ("check_circle", "text-positive"),
                        "failed": ("error", "text-negative"),
                        "skipped": ("remove_circle_outline", "text-grey"),
                    }.get(state, ("radio_button_unchecked", "text-grey-5"))
                    ui.icon(icon).classes(f"{colour} text-h6")
                    ui.label(credential.label).classes("text-subtitle1")
                ui.label("stored" if stored else "not stored").classes("text-caption text-grey")

            ui.label(credential.why).classes("text-body2 text-grey-8")
            ui.label(credential.without).classes("text-caption text-grey-7 q-mb-sm")

            with ui.expansion("How to get one").classes("w-full"):
                for index, step in enumerate(credential.steps, 1):
                    ui.label(f"{index}. {step}").classes("text-body2 q-mb-xs")
                ui.link(credential.signup_url, credential.signup_url, new_tab=True)

            boxes: dict[str, object] = {}
            for field_ in credential.fields:
                existing = credentials.load(field_.env) or field_.default
                boxes[field_.env] = ui.input(
                    field_.label, value=existing,
                    password=field_.secret,
                    password_toggle_button=field_.secret,
                ).classes("w-full")

            result_area = ui.column().classes("w-full")

            def save_values(_boxes=boxes) -> dict:
                values = {env: (box.value or "").strip() for env, box in _boxes.items()}
                for env, value in values.items():
                    if value:
                        credentials.save(env, value)
                return values

            def run_test(cred=credential, area=result_area, saver=save_values) -> None:
                values = saver()
                if not self.state.runtime_ready:
                    ui.notify("Install the model first - the check runs inside it.",
                              type="warning")
                    return

                install = self._configured_install()
                tag = self.state.code_tag or DEFAULT_TAG
                task = BackgroundTask(f"Testing {cred.label}")
                self.cred_tasks[cred.key] = task

                area.clear()
                with area, ui.row().classes("items-center"):
                    ui.spinner(size="sm")
                    ui.label(f"Asking {cred.label}...").classes("text-body2")

                task.start(credentials.test_credential, cred.key,
                           install.python(tag), install.code_dir(tag), values)

                def check() -> None:
                    if not task.finished:
                        return
                    timer.deactivate()
                    outcome = task.result if task.succeeded else None
                    area.clear()
                    with area:
                        if outcome is None:
                            ui.label("The check could not run.").classes("text-negative")
                            ui.label(str(task.error)).classes("text-caption")
                            self.state.credentials[cred.key] = "failed"
                        else:
                            colour = "text-positive" if outcome.ok else "text-negative"
                            with ui.row().classes("items-center"):
                                ui.icon("check_circle" if outcome.ok else "error").classes(colour)
                                ui.label(outcome.summary).classes("text-body2")
                            for line in outcome.verified:
                                ui.label(f"OK - {line}").classes("text-caption text-positive")
                            # What was NOT proven matters more than the tick: the Copernicus
                            # check reaches the service but cannot validate the key itself.
                            for line in outcome.unverified:
                                ui.label(f"not checked - {line}").classes("text-caption text-grey-7")
                            if outcome.detail and not outcome.ok:
                                ui.label(outcome.detail).classes("text-caption text-grey-7")
                            self.state.credentials[cred.key] = (
                                "passed" if outcome.ok else "failed")
                    self.persist()

                timer = ui.timer(0.3, check)

            def skip(cred=credential) -> None:
                self.state.credentials[cred.key] = "skipped"
                self.persist()
                ui.notify(f"{cred.label} skipped. {cred.without}", type="info")

            def just_save(saver=save_values) -> None:
                saver()
                ui.notify("Saved to Windows Credential Manager.", type="positive")

            with ui.row().classes("q-mt-sm"):
                ui.button("Save and test", on_click=run_test).props("color=primary outline")
                ui.button("Save", on_click=just_save).props("flat")
                ui.button("Skip for now", on_click=skip).props("flat")

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
        headless_check: bool = False, show: bool | None = None,
        port: int | None = None) -> int:
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
        show=(not mode.native) if show is None else show,
        port=port or free_port(),
        title=f"{branding.PRODUCT_NAME} — Setup",
        window_size=(1040, 780) if mode.native else None,
        favicon="🌱",
    )
    return 0
