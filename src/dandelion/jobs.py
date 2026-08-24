"""Run one upstream command, honestly.

Everything Studio asks of the research code goes through here. The engine is small, but four
things about upstream make it less trivial than "run a subprocess":

**Exit code is not a success signal.** `extract-rte` and `extract-entsoe` catch their own
exceptions, print `[ERREUR] …` and exit 0. A job engine trusting `returncode` would report a
failed ingest as a success, and the user would find out days later when a projection came back
short. Every job declares stdout patterns that mean failure, and they are scanned.

**Some failures are silent successes of a different kind.** `weathergen simulate` with the
climate trend on and no deltas prints one line and produces an untrended cube labelled as
trended. That line is a failure marker too.

**Progress exists, in a specific format.** `powersim_core.progress` prints parseable lines
under a pipe. Where they appear the engine reports a real fraction and a real ETA; where they
do not it reports elapsed time and says nothing about completion.

**Only one job at a time.** The stages share one SQLite database and one Parquet lake.

Logs are written as they arrive, scrubbed of anything that looks like a stored credential,
because a log is the first thing a stuck user sends to somebody else.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from dandelion import credentials
from dandelion.paths import Install
from dandelion.process_group import ProcessGroup, TerminationResult
from drivers.inventory import PROGRESS_COUNTER_RE, PROGRESS_RE, Kind
from drivers.inventory import job as find_job

#: How many recent output lines Studio keeps in memory for the live view. The full log is on
#: disk; this is only what the window shows without opening it.
TAIL_LINES = 400


@dataclass
class Progress:
    """What upstream said about its own progress, or nothing."""

    label: str = ""
    done: int = 0
    total: int = 0
    percent: int | None = None
    elapsed: str = ""
    eta: str = ""
    rate: str = ""
    note: str = ""

    @property
    def fraction(self) -> float | None:
        if self.total <= 0:
            return None
        return min(1.0, self.done / self.total)


def parse_progress(line: str) -> Progress | None:
    """A `powersim_core.progress` line, or None for ordinary output."""
    match = PROGRESS_RE.match(line)
    if match:
        return Progress(
            label=match["label"], done=int(match["done"]), total=int(match["total"]),
            percent=int(match["pct"]), elapsed=match["elapsed"], eta=match["eta"],
            rate=match["rate"], note=match["note"] or "",
        )
    counter = PROGRESS_COUNTER_RE.match(line)
    if counter:
        return Progress(label=counter["label"], done=int(counter["done"]),
                        elapsed=counter["elapsed"], rate=counter["rate"],
                        note=counter["note"] or "")
    return None


@dataclass
class JobResult:
    job_id: str
    argv: list[str]
    cwd: str
    log_path: str
    started_at: str
    finished_at: str = ""
    exit_code: int | None = None
    #: "succeeded" | "failed" | "cancelled" | "refused"
    outcome: str = "running"
    #: Why it failed, in words a user can act on.
    reason: str = ""
    #: Lines matching a job's declared failure markers.
    failure_lines: list[str] = field(default_factory=list)
    warning_lines: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    termination: TerminationResult | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "succeeded"


class JobBusyError(RuntimeError):
    """Raised when a second job is submitted while one is running."""


def render_argv(job, install: Install, tag: str, params: dict[str, object]) -> list[str]:
    """Fill a job's argv template.

    `{python}` becomes the release environment's interpreter; a console-script job resolves
    its command inside that environment's Scripts directory rather than trusting PATH, which
    on a user's machine may hold a different Python entirely.
    """
    values = {"python": str(install.python(tag)), **{k: str(v) for k, v in params.items()}}
    argv: list[str] = []
    for index, token in enumerate(job.argv):
        if index == 0 and job.kind is Kind.CONSOLE:
            argv.append(str(install.env_dir(tag) / "Scripts" / f"{token}.exe"))
            continue
        rendered = token
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", value)
        if re.fullmatch(r"\{[a-z_]+\}", rendered):
            raise ValueError(
                f"job {job.id!r} needs a value for {rendered} and none was given"
            )
        argv.append(rendered)

    for group in getattr(job, "optional_argv", ()):
        needed = {m for token in group for m in re.findall(r"\{([a-z_]+)\}", token)}
        if not needed or not needed <= params.keys():
            continue
        for token in group:
            rendered = token
            for key, value in values.items():
                rendered = rendered.replace("{" + key + "}", value)
            argv.append(rendered)
    return argv


def declared_preflight(install: Install, tag: str, params: dict[str, object]
                       ) -> Callable[[str], str | None]:
    """Run the checks a job DECLARES, and return why it must not start.

    Without this the `preflight` field on a job is documentation: the registry validates
    that every declared check is implemented, but nothing would actually call it, and the
    job would run unguarded — the precise fail-open the field exists to prevent.
    """
    from drivers import preflight as checks

    def run(job_id: str) -> str | None:
        job = find_job(job_id)
        for name in job.preflight:
            if name == "fr-history-present":
                year = params.get("year")
                if year is None:
                    continue
                outcome = checks.check_fr_history(
                    install.data_dir / "pricemodeling.db", int(year))
            elif name == "stack-inputs-present":
                year = params.get("year")
                if year is None:
                    continue
                outcome = checks.check_stack_inputs(
                    install.data_dir / "pricemodeling.db", int(year))
            elif name == "markup-model-present":
                outcome = checks.check_markup_model(
                    install.code_dir(tag) / "dispatch_model" / "reports")
            elif name == "cmip6-deltas-present":
                outcome = checks.check_cmip6_deltas(
                    install.code_dir(tag) / "weathergen" / "config.yaml")
            else:  # pragma: no cover - validate_registry forbids reaching this
                continue
            if not outcome.ok:
                return outcome.reason
        return None

    return run


class JobEngine:
    """Runs one upstream command at a time, against one installed release."""

    def __init__(self, install: Install, tag: str):
        self.install = install
        self.tag = tag
        self._group: ProcessGroup | None = None
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        self.result: JobResult | None = None
        self.tail: list[str] = []
        self.progress: Progress | None = None

    # ------------------------------------------------------------------ state
    @property
    def busy(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # ------------------------------------------------------------------ running
    def run(self, job_id: str, params: dict[str, object] | None = None, *,
            on_line: Callable[[str], None] | None = None,
            on_progress: Callable[[Progress], None] | None = None,
            preflight: Callable[[str], str | None] | None = None) -> JobResult:
        """Run a job to completion. Blocks — call it from a background thread.

        `preflight` returns a reason to refuse, or None. It exists so the engine can decline
        to start a job whose inputs are missing rather than let it produce a wrong answer:
        a projection without the fitted markup wedge, or a trended simulation without the
        climate deltas.
        """
        with self._lock:
            if self.busy:
                raise JobBusyError(
                    "Another job is already running. The model stages share one database, "
                    "so they run one at a time."
                )
            self._cancelled = False

        job = find_job(job_id)
        argv = render_argv(job, self.install, self.tag, params or {})
        cwd = self.install.code_dir(self.tag) / job.cwd if job.cwd != "." \
            else self.install.code_dir(self.tag)

        started = datetime.now(UTC)
        log_path = self.install.logs_dir / (
            f"{started.strftime('%Y%m%d-%H%M%S')}-{job_id}.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)

        result = JobResult(job_id=job_id, argv=argv, cwd=str(cwd), log_path=str(log_path),
                           started_at=started.isoformat(timespec="seconds"))
        self.result = result
        self.tail = []
        self.progress = None

        if preflight is None:
            preflight = declared_preflight(self.install, self.tag, params or {})
        if preflight is not None:
            refusal = preflight(job_id)
            if refusal:
                result.outcome = "refused"
                result.reason = refusal
                result.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
                return result

        failure_patterns = [re.compile(p) for p in job.failure_markers]
        warning_patterns = [re.compile(p) for p in job.warning_markers]
        env = credentials.job_environment()
        # Parameters are also exposed as DANDELION_<NAME>. Some upstream entry points are
        # module-level functions rather than CLI commands, so the driver invokes them with
        # `python -c` and a constant runner string; passing values through the environment
        # keeps that string a constant instead of something built by concatenation.
        for key, value in (params or {}).items():
            env[f"DANDELION_{key.upper()}"] = str(value)
        clock = time.monotonic()

        self._group = ProcessGroup(f"job-{job_id}")
        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as log:
                log.write(f"# {job_id}\n# {' '.join(argv)}\n# cwd={cwd}\n"
                          f"# started {result.started_at}\n\n")
                log.flush()
                try:
                    self._process = self._group.spawn(
                        argv, cwd=str(cwd), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace", bufsize=1,
                    )
                except OSError as exc:
                    result.outcome = "failed"
                    result.reason = f"Could not start {job_id}: {exc}"
                    log.write(result.reason + "\n")
                    return self._finish(result, clock)

                assert self._process.stdout is not None
                for raw in self._process.stdout:
                    line = credentials.scrub(raw.rstrip("\n"))
                    log.write(line + "\n")

                    progress = parse_progress(line)
                    if progress is not None:
                        self.progress = progress
                        if on_progress:
                            on_progress(progress)
                    else:
                        self.tail.append(line)
                        del self.tail[:-TAIL_LINES]
                        if on_line:
                            on_line(line)

                    for pattern in failure_patterns:
                        if pattern.search(line):
                            result.failure_lines.append(line)
                    for pattern in warning_patterns:
                        if pattern.search(line):
                            result.warning_lines.append(line)

                result.exit_code = self._process.wait()
        finally:
            if self._group is not None:
                self._group.close()

        return self._finish(result, clock)

    def _finish(self, result: JobResult, clock: float) -> JobResult:
        result.duration_s = round(time.monotonic() - clock, 1)
        result.finished_at = datetime.now(UTC).isoformat(timespec="seconds")

        if self._cancelled:
            result.outcome = "cancelled"
            result.reason = "Cancelled."
        elif result.failure_lines:
            # The important case: upstream printed an error and exited 0 anyway.
            result.outcome = "failed"
            result.reason = result.failure_lines[0]
        elif result.exit_code not in (0, None):
            result.outcome = "failed"
            result.reason = f"Exited with code {result.exit_code}."
        elif result.outcome == "running":
            result.outcome = "succeeded"

        self._process = None
        return result

    # ------------------------------------------------------------------ cancelling
    def cancel(self) -> TerminationResult | None:
        """Stop the running job and everything it started."""
        if self._group is None or not self.busy:
            return None
        self._cancelled = True
        termination = self._group.terminate()
        if self.result is not None:
            self.result.termination = termination
        return termination


def summarise(result: JobResult) -> str:
    """One line for the history list."""
    minutes, seconds = divmod(int(result.duration_s), 60)
    duration = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
    if result.outcome == "succeeded" and result.warning_lines:
        return f"finished with warnings in {duration}"
    return {
        "succeeded": f"finished in {duration}",
        "failed": f"failed after {duration}",
        "cancelled": f"cancelled after {duration}",
        "refused": "did not start",
    }.get(result.outcome, result.outcome)
