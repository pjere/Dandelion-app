"""Studio: what you have, what is missing, and what is running.

The home page answers three questions a user actually has when they open this: is my data
current, can I run anything, and what happened last time. It is deliberately cheap — every
number comes from `ingest_log` or the filesystem, never from a command that scans the master
table, so it can be redrawn whenever without costing anything.

Where something is missing it says so plainly and offers the fix, rather than leaving the user
to discover it hours into a run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nicegui import ui  # noqa: E402

from dandelion import branding, credentials, freshness, pages  # noqa: E402
from dandelion.background import BackgroundTask, TaskView  # noqa: E402
from dandelion.jobs import JobEngine, summarise  # noqa: E402
from dandelion.manifest import InstallManifest  # noqa: E402
from dandelion.paths import Install, default_install  # noqa: E402
from dandelion.ui import choose_window_mode, free_port  # noqa: E402
from drivers.inventory import job as find_job  # noqa: E402

SEVERITY_COLOUR = {"fresh": "text-positive", "ageing": "text-warning",
                   "stale": "text-negative", "unknown": "text-grey-5"}

#: Years upstream calibrated against: 2019 normal, 2022 the crisis, 2023-24 high renewables
#: (scripts/backfill_entsoe.py:25). A free-text year would invite one with no data behind it.
BACKTEST_YEARS = [2019, 2022, 2023, 2024]


def directory_size(path: Path) -> int:
    """Bytes under a directory, never following a junction into another store."""
    import os

    if not path.exists():
        return 0
    total = 0
    for root, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not os.path.isjunction(Path(root) / d)]
        for name in filenames:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class Studio:
    def __init__(self, install: Install, manifest: InstallManifest):
        self.install = install
        self.manifest = manifest
        self.tag = manifest.code.tag
        self.engine = JobEngine(install, self.tag)
        self.task: BackgroundTask | None = None
        self.view = TaskView()
        self.history: list[str] = []
        self.body: ui.column | None = None
        self._picture: freshness.DataPicture | None = None
        #: (job_id, reason) when the last attempt was refused before it started.
        self.refusal: tuple[str, str] | None = None
        self.backtest_year: int = BACKTEST_YEARS[0]
        self.data_year: int = pages.YEARS[0]
        self.tab: str = "Home"
        self.journal = self.engine.journal
        #: Set by the activity card so a click can repaint just that card. Re-rendering the
        #: whole page from a handler would leave the previous card's timer alive, repainting
        #: into containers that no longer exist.
        self._repaint_activity = lambda: None

    # ------------------------------------------------------------------ data
    def picture(self, refresh: bool = False) -> freshness.DataPicture:
        if self._picture is None or refresh:
            self._picture = freshness.describe(self.install.data_dir / "pricemodeling.db")
        return self._picture

    @property
    def fits_shipped(self) -> bool:
        """The release brought its own fitted models, so nothing here was never fitted."""
        return getattr(self.manifest.fits, "source", "none") == "downloaded"

    def start(self, job_id: str, **params) -> None:
        """Public entry for the pages. Same path as any other click."""
        self._start(job_id, **params)

    # ------------------------------------------------------------------ chrome
    def render(self) -> None:
        assert self.body is not None
        self.body.clear()
        running = self.task is not None and self.task.running
        with self.body:
            self._header()
            # Plain containers whose visibility we set ourselves, rather than
            # `ui.tab_panels`. Three attempts at the Quasar carousel all left the strip
            # highlighting the new tab while `elementFromPoint` still painted the old
            # panel: binding the tabs alone parked the panels, binding both ends made the
            # two bindings fight, and the documented tabs-drives-panels linkage did not
            # take either.
            #
            # Toggling visibility also avoids re-rendering the page from a handler, which
            # is what left a previous activity card's timer alive repainting detached
            # containers in Phase 3. Nothing is rebuilt here; three containers exist and
            # exactly one is shown.
            panels: dict[str, ui.column] = {}

            def show(name: str) -> None:
                self.tab = name
                for label, container in panels.items():
                    container.set_visibility(label == name)

            with ui.tabs().classes("w-full").on_value_change(
                    lambda e: show(e.value)) as tabs:
                for name in ("Home", "Data", "Models"):
                    ui.tab(name)
            tabs.value = self.tab

            for name, build in (("Home", self._home),
                                ("Data", lambda: pages.data_page(self, running)),
                                ("Models", lambda: pages.models_page(self, running))):
                with ui.column().classes("w-full q-pt-md") as panel:
                    build()
                panels[name] = panel
            show(self.tab)

            self._activity_card()

    def _home(self) -> None:
        self._gaps()
        with ui.row().classes("w-full items-start no-wrap gap-4"):
            with ui.column().classes("flex-grow"):
                self._data_card()
            with ui.column().classes("w-80"):
                self._credentials_card()
                self._disk_card()

    def _header(self) -> None:
        with ui.row().classes("w-full items-center justify-between q-mb-md"):
            with ui.column().classes("gap-0"):
                ui.label(branding.PRODUCT_NAME).classes("text-h5")
                ui.label(f"Model release {self.tag}").classes("text-caption text-grey-7")
            ui.button(icon="refresh", on_click=lambda: (self.picture(refresh=True),
                                                       self.render())).props("flat round")

    def _gaps(self) -> None:
        """What the installation still needs, from the record written at install time."""
        gaps = self.manifest.missing_for_a_full_run()
        if not gaps:
            return
        with ui.card().classes("w-full bg-amber-1 q-mb-md"):
            ui.label("Before you can run a projection").classes("text-subtitle2")
            for gap in gaps:
                with ui.row().classes("items-start no-wrap"):
                    ui.icon("info").classes("text-primary")
                    ui.label(gap).classes("text-body2")

    # ------------------------------------------------------------------ cards
    def _data_card(self) -> None:
        picture = self.picture()
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center w-full justify-between"):
                ui.label("Data").classes("text-subtitle1")
                if picture.readable:
                    ui.label(f"{freshness.human_rows(picture.total_rows)} rows across "
                             f"{picture.total_sources} sources").classes("text-caption text-grey")

            if not picture.exists:
                ui.label("No database yet.").classes("text-body2 q-mt-sm")
                ui.label("Nothing has been downloaded. The data pages build it using your "
                         "own accounts.").classes("text-caption text-grey-7")
                return
            if picture.error:
                ui.label(picture.error).classes("text-body2 text-negative q-mt-sm")
                return
            if picture.empty:
                ui.label("The database exists but is empty.").classes("text-body2 q-mt-sm")
                ui.label("That is what a fresh install looks like - nothing has been "
                         "ingested yet.").classes("text-caption text-grey-7")
                return

            for family in picture.families:
                phrase, severity = freshness.staleness(family.last_ingested)
                with ui.row().classes("items-center no-wrap w-full q-py-xs"):
                    ui.icon("circle").classes(f"{SEVERITY_COLOUR[severity]} text-xs")
                    ui.label(family.label).classes("text-body2").style("width:190px")
                    ui.label(family.provider).classes("text-caption text-grey-6") \
                        .style("width:150px")
                    ui.label(freshness.human_rows(family.rows)).classes("text-caption") \
                        .style("width:60px")
                    ui.label(f"to {family.coverage_end}").classes("text-caption text-grey-7") \
                        .style("width:110px")
                    ui.label(phrase).classes(f"text-caption {SEVERITY_COLOUR[severity]}")
                    if family.problems:
                        ui.space()
                        ui.label(f"{family.problems} chunk(s) missing") \
                            .classes("text-caption text-warning")

            ui.label("‘to’ is how far the data reaches; the date on the right is when it was "
                     "last fetched.").classes("text-caption text-grey-6 q-mt-sm")

    def _credentials_card(self) -> None:
        state = credentials.status()
        recorded = self.manifest.credentials
        with ui.card().classes("w-full q-mb-md"):
            ui.label("Accounts").classes("text-subtitle1")
            for credential in credentials.CREDENTIALS:
                tested = recorded.get(credential.key, "untested")
                if tested == "passed":
                    icon, colour, note = "check_circle", "text-positive", "working"
                elif tested == "failed":
                    icon, colour, note = "error", "text-negative", "not working"
                elif state.get(credential.key):
                    icon, colour, note = "help", "text-grey-6", "stored, never tested"
                else:
                    icon, colour, note = "radio_button_unchecked", "text-grey-5", "not set up"
                with ui.row().classes("items-center no-wrap"):
                    ui.icon(icon).classes(colour)
                    ui.label(credential.label).classes("text-body2")
                    ui.space()
                    ui.label(note).classes("text-caption text-grey-7")

    def _disk_card(self) -> None:
        with ui.card().classes("w-full"):
            ui.label("Disk").classes("text-subtitle1")
            entries = [
                ("Data", self.install.data_root),
                ("Program", self.install.app_root / "envs"),
                ("German registry", self.install.mastr_dir),
            ]
            for label, path in entries:
                with ui.row().classes("items-center no-wrap w-full"):
                    ui.label(label).classes("text-body2")
                    ui.space()
                    ui.label(human_bytes(directory_size(path))).classes("text-caption")
            free = None
            try:
                import shutil

                free = shutil.disk_usage(self.install.data_root).free
            except OSError:
                pass
            if free is not None:
                ui.label(f"{human_bytes(free)} free on this drive") \
                    .classes("text-caption text-grey-7 q-mt-xs")

    # ------------------------------------------------------------------ activity
    def _activity_card(self) -> None:
        with ui.card().classes("w-full q-mt-md"):
            with ui.row().classes("items-center w-full justify-between"):
                ui.label("Activity").classes("text-subtitle1")
                cancel = ui.button("Cancel", on_click=self._cancel).props("flat color=negative")

            refusal_area = ui.column().classes("w-full")
            progress_area = ui.column().classes("w-full")
            buttons = ui.row().classes("q-mt-sm")

            def paint() -> None:
                cancel.set_visibility(self.task is not None and self.task.running)
                refusal_area.clear()
                if self.refusal is not None:
                    job_id, reason = self.refusal
                    with refusal_area, ui.card().classes("w-full bg-amber-1 q-mb-sm"):
                        with ui.row().classes("items-center no-wrap"):
                            ui.icon("info").classes("text-primary")
                            ui.label(find_job(job_id).title + " did not start").classes(
                                "text-subtitle2")
                        ui.label(reason).classes("text-body2")
                progress_area.clear()
                with progress_area:
                    running = self.task is not None and self.task.running
                    if running:
                        with ui.row().classes("items-center w-full"):
                            ui.spinner(size="sm")
                            ui.label(self.view.current or "Working…").classes("text-body2")
                        if self.view.fraction is not None:
                            ui.linear_progress(value=self.view.fraction).classes("w-full")
                        else:
                            ui.linear_progress().props("indeterminate").classes("w-full")
                        for line in self.engine.tail[-8:]:
                            ui.label(line).classes("text-caption text-grey-7") \
                                .style("font-family:Consolas,monospace")
                    for entry in reversed(self.history[-6:]):
                        ui.label(entry).classes("text-body2")
                    if not running and not self.history:
                        ui.label("Nothing has been run yet.").classes("text-body2 text-grey-7")

                buttons.clear()
                with buttons:
                    running = self.task is not None and self.task.running
                    ui.button("Check the database",
                              on_click=lambda: self._start("status")) \
                        .props("outline").set_enabled(not running)
                    year = ui.select(BACKTEST_YEARS, value=self.backtest_year,
                                     label="Year").props("outlined dense") \
                        .style("width:110px").bind_value(self, "backtest_year")
                    year.set_enabled(not running)
                    ui.button("Back-test that year",
                              on_click=lambda: self._start(
                                  "dispatch-backtest", year=int(self.backtest_year))) \
                        .props("outline").set_enabled(not running)

            self._repaint_activity = paint

            def poll() -> None:
                if self.task is None:
                    return
                changed = self.view.apply_all(self.task.drain())
                if self.engine.progress is not None:
                    fraction = self.engine.progress.fraction
                    label = self.engine.progress.label
                    eta = self.engine.progress.eta
                    self.view.current = (f"{label} — {self.engine.progress.percent}%"
                                         + (f", {eta} remaining" if eta and eta[0].isdigit()
                                            else ""))
                    self.view.fraction = fraction
                    changed = True
                if changed or self.task.finished:
                    paint()

            ui.timer(0.4, poll)
            paint()

    def _start(self, job_id: str, **params) -> None:
        if self.task is not None and self.task.running:
            ui.notify("Something is already running.", type="warning")
            return
        self.view = TaskView()
        task = BackgroundTask(job_id)
        self.task = task

        def work():
            result = self.engine.run(job_id, params)
            if result.outcome == "refused":
                #: A refusal is not a failure: nothing ran and nothing changed. It names
                #: what is missing, which is the one thing the user can act on, so it gets
                #: its own card rather than a line in a list of finished runs.
                self.refusal = (job_id, result.reason)
                return result
            self.refusal = None
            self.history.append(f"{job_id}: {summarise(result)}"
                                + (f" — {result.reason}" if result.reason else ""))
            return result

        task.start(work)
        self._repaint_activity()

    def _cancel(self) -> None:
        termination = self.engine.cancel()
        if termination is None:
            return
        if termination.clean:
            ui.notify("Cancelled. Nothing was left running.", type="positive")
        else:
            ui.notify(f"Cancelled, but {len(termination.survivors)} process(es) would not "
                      f"stop.", type="negative")


def run(install: Install | None = None, *, force_mode: str | None = None,
        headless_check: bool = False, show: bool | None = None,
        port: int | None = None) -> int:
    install = install or default_install()
    manifest = InstallManifest.load(install.manifest_file)
    if manifest is None or not manifest.usable:
        raise SystemExit("No usable installation found. Run the setup wizard first.")

    if manifest.app_root:
        install = Install(app_root=Path(manifest.app_root),
                          data_root=Path(manifest.data_root or install.data_root))
    studio = Studio(install, manifest)

    @ui.page("/")
    def index() -> None:
        ui.add_head_html("<style>body{font-family:Segoe UI,system-ui,sans-serif}</style>")
        with ui.column().classes("q-pa-lg w-full max-w-6xl mx-auto") as body:
            studio.body = body
        studio.render()

    if headless_check:
        return 0

    mode = choose_window_mode(force_mode)
    ui.run(native=mode.native, reload=False,
           show=(not mode.native) if show is None else show,
           port=port or free_port(),
           title=branding.PRODUCT_NAME, window_size=(1180, 860) if mode.native else None,
           favicon="🌱")
    return 0
