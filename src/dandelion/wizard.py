"""The Setup Wizard.

Structured so that closing it is never destructive: every page writes its outcome to
`wizard_state.json` before moving on, and re-running the executable resumes at the first
incomplete step. That is not polish — the ENTSO-E token arrives by email days after you ask
for it, so an installation genuinely spans days.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nicegui import app, ui  # noqa: E402

from dandelion import branding, credentials  # noqa: E402
from dandelion import fits as fits_mod
from dandelion.background import BackgroundTask, TaskView  # noqa: E402
from dandelion.credentials import CREDENTIALS  # noqa: E402
from dandelion.download import sha256_file  # noqa: E402
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
        ui.label("Fitted models").classes("text-h5 q-mb-sm")
        ui.markdown(
            "The model needs calibrated parameters before it can project anything. You can "
            "download the reference ones, or compute your own."
        ).classes("q-mb-sm")
        ui.markdown(
            "**Downloading them is recommended.** Your results then match the reference model "
            "exactly, and nothing needs to pull ERA5 - which is why the Copernicus account "
            "becomes optional."
        ).classes("text-body2 text-grey-7 q-mb-md")

        if self.state.fits_ready and self.task is None:
            with ui.card().classes("w-full bg-green-1 q-mb-md"):
                with ui.row().classes("items-center"):
                    ui.icon("check_circle").classes("text-positive text-h5")
                    ui.label("The fitted models are installed and verified.") \
                        .classes("text-body1")
            with ui.row():
                ui.button("Continue", on_click=self.advance).props("color=primary")
                ui.button("Back", on_click=lambda: self.go("credentials")).props("flat")
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
                        ui.linear_progress().props("indeterminate").classes("w-full")
                for done in self.view.steps[-10:]:
                    ok = done.ok is not False
                    with ui.row().classes("items-center no-wrap"):
                        ui.icon("check_circle" if ok else "error").classes(
                            "text-positive" if ok else "text-negative")
                        ui.label(done.message).classes("text-body2")
                if self.view.error:
                    with ui.card().classes("w-full bg-red-1"):
                        ui.label("Download stopped").classes("text-subtitle2 text-negative")
                        ui.label(self.view.error).classes("text-body2 whitespace-pre-wrap")
                        ui.label("Nothing was lost - starting again resumes from where it "
                                 "stopped.").classes("text-caption text-grey-7")

            buttons.clear()
            with buttons:
                if self.task is not None and self.task.running:
                    ui.label("Downloading...").classes("text-grey-7")
                elif self.state.fits_ready:
                    ui.button("Continue", on_click=self.advance).props("color=primary")
                else:
                    label = "Try again" if self.view.error else "Download the fitted models"
                    ui.button(label, on_click=self._start_fits).props("color=primary")
                    ui.button("I will fit them myself", on_click=choose_own).props("flat")
                    ui.button("Back", on_click=lambda: self.go("credentials")).props("flat")

        def choose_own() -> None:
            self.state.fits_choice = "fit-myself"
            self.persist()
            ui.notify(fits_mod.FIT_YOURSELF_COST, type="info", multi_line=True,
                      classes="w-96")
            self.advance()

        def poll() -> None:
            if self.task is None:
                return
            if self.view.apply_all(self.task.drain()):
                if self.task.finished and self.task.succeeded and not self.state.fits_ready:
                    self.state.fits_ready = True
                    self.state.fits_choice = "download"
                    self.persist()
                paint()

        ui.timer(0.3, poll)
        paint()

    def _start_fits(self) -> None:
        from dandelion.provision import RELEASE_BASE

        self.view = TaskView()
        task = BackgroundTask("Fitted models")
        self.task = task
        install = self._configured_install()
        tag = self.state.code_tag or DEFAULT_TAG

        def work():
            task.step("Reading the published listing")
            plan = fits_mod.fetch_plan(RELEASE_BASE, tag, install.app_dir)
            size = fits_mod.human(plan.download_bytes)
            task.step(f"{len(plan.chunks)} file(s) to fetch, {size}")

            if fits_mod.already_installed(plan, install.code_dir(tag)):
                task.step("Already present and verified", ok=True)
                return plan

            def progress(name, fraction, index, total) -> None:
                task.progress(f"Downloading {name} ({index} of {total})", fraction)

            chunks = fits_mod.download_fits(plan, RELEASE_BASE, install.app_dir, progress)
            task.step("Download complete and checksums verified", ok=True)

            task.progress("Unpacking", None)
            names = fits_mod.extract_fits(chunks, install.code_dir(tag))
            task.step(f"Unpacked {len(names)} fitted files", ok=True)

            problems = fits_mod.verify_installed(plan, install.code_dir(tag))
            if problems:
                raise fits_mod.FitsError("; ".join(problems))
            task.step("Verified against the published checksums", ok=True)

            for chunk in chunks:                          # the archives are no longer needed
                chunk.unlink(missing_ok=True)
            return plan

        task.start(work)
        self.render()

    #: What a full rebuild involves, in the order it runs. Deliberately not upstream's `all`
    #: command, which omits REMIT, the plant registries and the GB/Elexon ingest.
    BUILD_PLAN = (
        ("Weather observations", "Meteo-France SYNOP", None, "about an hour"),
        ("French generation and load", "RTE", "rte", "several hours"),
        ("Prices, load and flows", "ENTSO-E, all thirteen zones", "entsoe", "several hours"),
        ("Outage notifications", "ENTSO-E REMIT", "entsoe", "under an hour"),
        ("GB market data", "Elexon (no account needed)", None, "under an hour"),
        ("Plant registries", "MaStR, ODRE, OPSD, REPD", None, "about an hour, ~7 GB"),
        ("Reconcile and build the master table", "the expensive one", None, "an hour or more"),
    )

    def page_data(self) -> None:
        ui.label("Build the database").classes("text-h5 q-mb-sm")
        ui.markdown(
            "Dandelion Studio ships no market data. This step downloads it using your "
            "accounts, and it is the long part: **expect it to run overnight** and to use "
            "roughly 26 GB."
        ).classes("q-mb-sm")
        ui.markdown(
            "You do not have to do it now. Studio runs the same sequence with progress, logs "
            "and a cancel button, and it can be stopped and resumed - which is the better "
            "place for something this long."
        ).classes("text-body2 text-grey-7 q-mb-md")

        with ui.card().classes("w-full q-mb-md"):
            ui.label("What it downloads").classes("text-subtitle2 q-mb-xs")
            for label, source, needs, duration in self.BUILD_PLAN:
                blocked = needs is not None and \
                    self.state.credential_state(needs) not in ("passed",)
                with ui.row().classes("items-center no-wrap w-full"):
                    if blocked:
                        ui.icon("lock").classes("text-warning")
                    else:
                        ui.icon("cloud_download").classes("text-grey-6")
                    ui.label(label).classes("text-body2")
                    ui.label(f"- {source}").classes("text-caption text-grey-7")
                    ui.space()
                    ui.label(duration).classes("text-caption text-grey-6")
                if blocked:
                    ui.label(f"    needs the {needs.upper()} credential, which is not "
                             f"confirmed yet").classes("text-caption text-warning")

        db_area = ui.column().classes("w-full q-mb-md")

        def initialise() -> None:
            """A quick, real first step: create the empty database through the junction.

            Seconds rather than hours, but it proves the paths, the link and the environment
            all work together - which is worth knowing before committing to a night of it.
            """
            install = self._configured_install()
            tag = self.state.code_tag or DEFAULT_TAG
            task = BackgroundTask("Initialising the database")
            self.cred_tasks["initdb"] = task

            db_area.clear()
            with db_area, ui.row().classes("items-center"):
                ui.spinner(size="sm")
                ui.label("Creating the database...").classes("text-body2")

            def work():
                import subprocess

                proc = subprocess.run(
                    [str(install.python(tag)), "-X", "utf8", "-m", "pricemodeling", "init-db"],
                    cwd=str(install.code_dir(tag)), env=credentials.job_environment(),
                    capture_output=True, text=True, timeout=600,
                    encoding="utf-8", errors="replace",
                )
                if proc.returncode != 0:
                    raise RuntimeError((proc.stderr or proc.stdout).strip()[-400:])
                return (proc.stdout or "").strip().splitlines()[-1:]

            task.start(work)

            def check() -> None:
                if not task.finished:
                    return
                timer.deactivate()
                db_area.clear()
                with db_area:
                    if task.succeeded:
                        with ui.row().classes("items-center"):
                            ui.icon("check_circle").classes("text-positive")
                            ui.label("The database was created where you asked.") \
                                .classes("text-body2")
                        for line in (task.result or []):
                            ui.label(line).classes("text-caption text-grey-7")
                    else:
                        with ui.row().classes("items-center"):
                            ui.icon("error").classes("text-negative")
                            ui.label("Could not create the database.").classes("text-body2")
                        ui.label(str(task.error)).classes("text-caption text-grey-7")

            timer = ui.timer(0.3, check)

        def choose(which: str) -> None:
            self.state.data_choice = which
            self.persist()
            self.advance()

        with ui.row().classes("q-mt-md"):
            ui.button("Finish setup - build later in Studio",
                      on_click=lambda: choose("later")).props("color=primary")
            ui.button("Create the empty database now", on_click=initialise).props("flat") \
                .set_enabled(self.state.runtime_ready)
            ui.button("Back", on_click=lambda: self.go("models")).props("flat")

    def page_finish(self) -> None:
        ui.label("Ready").classes("text-h5 q-mb-sm")

        written = self._write_manifest()
        if written is None:
            with ui.card().classes("w-full bg-amber-1 q-mb-md"):
                ui.label("The installation is not complete enough to record yet.") \
                    .classes("text-body1")
                ui.label("Go back and install the model - everything else can wait.") \
                    .classes("text-body2")
            ui.button("Back", on_click=lambda: self.go("runtime")).props("flat")
            return

        with ui.card().classes("w-full bg-green-1 q-mb-md"):
            with ui.row().classes("items-center"):
                ui.icon("check_circle").classes("text-positive text-h5")
                ui.label(f"{branding.PRODUCT_NAME} is installed.").classes("text-body1")
            ui.label(f"Model release {written.code.tag} - opening this program again now "
                     f"starts Studio rather than this wizard.").classes("text-body2")

        gaps = written.missing_for_a_full_run()
        if gaps:
            with ui.card().classes("w-full q-mb-md"):
                ui.label("Still to do").classes("text-subtitle2")
                for gap in gaps:
                    with ui.row().classes("items-start no-wrap"):
                        ui.icon("info").classes("text-primary")
                        ui.label(gap).classes("text-body2")
                ui.label("Studio shows these on its home page too - nothing is lost by "
                         "closing now.").classes("text-caption text-grey-7")

        with ui.expansion("What was recorded").classes("w-full q-mb-md"):
            ui.label(f"Code {written.code.tag} ({written.code.commit[:12] or 'unknown commit'})") \
                .classes("text-body2")
            ui.label(f"Archive {written.code.archive_sha256[:16] or '-'}") \
                .classes("text-caption text-grey-7")
            ui.label(f"Fitted models: {written.fits.source}").classes("text-body2")
            ui.label(f"Terms {written.terms.version} accepted {written.terms.accepted_at}") \
                .classes("text-caption text-grey-7")
            ui.label("No credentials are stored in this file - only whether each was tested.") \
                .classes("text-caption text-grey-7")

        with ui.row():
            ui.button("Close", on_click=app.shutdown).props("color=primary")

    def _write_manifest(self):
        """Record the installation. Returns None when there is nothing worth recording."""
        from dandelion import manifest as manifest_mod
        from dandelion.paths import SEEDED_FROM_RELEASE, junctions_for

        if not self.state.can_finish:
            return None

        install = self._configured_install()
        tag = self.state.code_tag or DEFAULT_TAG

        code_manifest = None
        published = install.app_dir / f"code_manifest-{tag}.json"
        if published.is_file():
            try:
                code_manifest = json.loads(published.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                code_manifest = None

        seeded = {}
        for relative in SEEDED_FROM_RELEASE:
            path = install.store_dir(tag) / relative
            if path.is_file():
                seeded[relative] = sha256_file(path)

        fits_record = manifest_mod.FittedModels(
            source="downloaded" if self.state.fits_ready
            else ("fit-locally" if self.state.fits_choice == "fit-myself" else "none"),
            tag=tag if self.state.fits_ready else "",
        )

        record = manifest_mod.build(
            app_version=branding_version(),
            app_root=install.app_root, data_root=install.data_root, tag=tag,
            code_manifest=code_manifest, wizard_state=self.state, fits=fits_record,
            junctions={str(j.link): str(j.target) for j in junctions_for(install, tag)},
            seeded=seeded,
        )

        # Belt and braces: a manifest gets attached to support emails. If a secret ever
        # reaches this structure it is a bug in our own code, so refuse to write rather than
        # claim a scrub that did not happen.
        stored = list(credentials.stored_environment().values())
        if not manifest_mod.contains_no_secrets(record, stored):
            raise RuntimeError(
                "refusing to write the install record: it contains a stored credential. "
                "This is a bug - please report it."
            )

        record.save(install.manifest_file)

        # Listed in Windows "Apps & features" only once there is genuinely something to
        # uninstall - an entry pointing at a failed install is worse than none.
        from dandelion import uninstall as uninstall_mod

        uninstall_mod.register(install, app_version=record.app_version,
                               executable=Path(sys.executable), product_name=branding.PRODUCT_NAME)

        self.state.finished = True
        self.persist()
        return record


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


def branding_version() -> str:
    """The application's own version, read where the entry point defines it."""
    try:
        from dandelion.__main__ import __version__

        return __version__
    except Exception:                                    # noqa: BLE001 - never block a finish
        return "unknown"
