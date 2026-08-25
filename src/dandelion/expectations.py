"""What to tell someone before, and during, a long job.

Two failures this exists to prevent, both observed while building Phase 3.

**The silent job.** `ingest-remit` ran for seventeen minutes and printed one line, at the
end. Under a pipe that is indistinguishable from a hang, and the reasonable thing for a user
to do about a hang is kill it — losing seventeen minutes of somebody else's API quota and
leaving a half-filled table. A window that says "this one is quiet, and that is normal"
costs nothing and prevents exactly that.

**The invented estimate.** Where upstream emits `powersim_core.progress` lines the engine
reports a real fraction and a real ETA. Where it does not, this module reports elapsed time
against a *measured typical*, and says so in those words. It never converts a typical into a
percentage — a job at 90% of its usual duration is not 90% done, and a bar that says so
would be a lie the moment a job runs long.
"""
from __future__ import annotations

from drivers.inventory import job as find_job


def human_duration(seconds: float) -> str:
    """A duration as a person would say it."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s" if rest else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def roughly(seconds: float) -> str:
    """A duration rounded the way an expectation should be — never to false precision."""
    if seconds <= 0:
        return ""
    minutes = seconds / 60
    if minutes < 1:
        return "under a minute"
    if minutes < 10:
        return f"about {round(minutes)} minutes"
    if minutes < 90:
        return f"about {int(round(minutes / 5) * 5)} minutes"
    return f"about {minutes / 60:.1f} hours".replace(".0 ", " ")


def before(job_id: str, years: int = 1) -> str:
    """What to say next to the button, or "" when there is nothing honest to say.

    `years` scales the estimate for jobs whose work is per-year. It is a multiplier on a
    measured single-year run, not a model of the provider.
    """
    job = find_job(job_id)
    if not job.typical_seconds:
        return ""
    estimate = roughly(job.typical_seconds * max(1, years))
    if job.quiet:
        return f"{estimate}, and it prints nothing until it finishes"
    return estimate


def during(job_id: str, elapsed: float, has_real_progress: bool = False) -> str:
    """What to say while it runs, when upstream gives no progress line of its own."""
    if has_real_progress:
        return ""
    job = find_job(job_id)
    shown = human_duration(elapsed)
    if not job.typical_seconds:
        return f"running for {shown}"

    typical = roughly(job.typical_seconds)
    if elapsed > job.typical_seconds * 1.5:
        # Longer than usual is worth saying plainly. It is not necessarily wrong — a wider
        # date range or a slow provider both do this — and inventing a new estimate here
        # would just be a second guess dressed as information.
        return (f"running for {shown}, longer than the usual {typical}. "
                f"Still working; a wider range or a slow provider will do this.")
    if job.quiet:
        return f"running for {shown} of {typical}. This one prints nothing until it is done."
    return f"running for {shown} of {typical}"
