"""What has been run, and whether it still counts.

Phase 3 kept a history list in memory. It died with the window, which is fine for "what
happened just now" and useless for the two questions Phase 4's pages have to answer:

* **may this run?** `weathergen simulate` needs a fitted model. Upstream will not say so —
  it degrades — and the `requires` edge on a Job was, like `preflight` and
  `needs_credentials` before it, declared and enforced nowhere.
* **is this still current?** A demand model fitted before the last data refresh is stale.
  Nothing about the model file says that; only the two timestamps together do.

Append-only JSONL, one object per line. Appending is atomic enough for our purpose (one job
at a time, one process), a truncated final line costs one record rather than the file, and
the whole thing stays readable with `type journal.jsonl` when someone needs to send it.

**Nothing here may carry a secret.** Params reach the journal, and a job could in principle
be handed one, so every value goes through `credentials.scrub` on the way in — the same
treatment the logs get, for the same reason.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dandelion import credentials

#: Outcomes that mean the work actually happened. A refusal changed nothing and a
#: cancellation left the job half-done, so neither satisfies a dependency.
SUCCEEDED = "succeeded"


@dataclass(frozen=True)
class Entry:
    """One recorded run."""

    job_id: str
    outcome: str
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    tag: str = ""
    params: dict[str, Any] | None = None
    reason: str = ""
    warnings: int = 0
    log_path: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == SUCCEEDED

    @property
    def when(self) -> datetime | None:
        """When it FINISHED, since that is when its output became real."""
        stamp = self.finished_at or self.started_at
        if not stamp:
            return None
        try:
            moment = datetime.fromisoformat(stamp.strip().replace(" ", "T")[:19])
        except ValueError:
            return None
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _clean(value: Any) -> Any:
    """Scrub anything that could be a stored credential, at any depth."""
    if isinstance(value, str):
        return credentials.scrub(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


class Journal:
    """Append-only record of every job this installation has run."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # ------------------------------------------------------------------ writing
    def record(self, result: Any, tag: str = "", params: dict[str, Any] | None = None) -> Entry:
        """Append one `JobResult`. Returns what was written."""
        entry = Entry(
            job_id=result.job_id,
            outcome=result.outcome,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_s=round(float(result.duration_s or 0.0), 1),
            tag=tag,
            params=_clean(dict(params or {})),
            reason=credentials.scrub(result.reason or "")[:500],
            warnings=len(result.warning_lines or []),
            log_path=str(result.log_path or ""),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")
        return entry

    # ------------------------------------------------------------------ reading
    def entries(self, job_id: str | None = None, limit: int | None = None) -> list[Entry]:
        """Newest first. A corrupt line is skipped, never raised.

        A half-written final line is the expected failure mode after a hard kill, and losing
        the whole history to it would be a poor trade.
        """
        if not self.path.is_file():
            return []
        out: list[Entry] = []
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict) or "job_id" not in raw:
                    continue
                known = {k: v for k, v in raw.items() if k in Entry.__dataclass_fields__}
                try:
                    entry = Entry(**known)
                except TypeError:
                    continue
                if job_id is None or entry.job_id == job_id:
                    out.append(entry)
        out.reverse()
        return out[:limit] if limit else out

    def last(self, job_id: str) -> Entry | None:
        """The most recent run of this job, whatever became of it."""
        found = self.entries(job_id, limit=1)
        return found[0] if found else None

    def last_success(self, job_id: str, tag: str | None = None) -> Entry | None:
        """The most recent run that actually did the work.

        `tag` restricts to a code release: output produced by a different release of the
        model is not evidence that this one has been run.
        """
        for entry in self.entries(job_id):
            if entry.ok and (tag is None or entry.tag == tag):
                return entry
        return None

    def has_succeeded(self, job_id: str, tag: str | None = None) -> bool:
        return self.last_success(job_id, tag) is not None

    def missing_prerequisites(self, job_id: str, tag: str | None = None) -> list[str]:
        """Which of a job's `requires` have never succeeded. Direct edges only.

        Transitive closure is deliberately not walked: reporting the whole chain when one
        link is missing buries the thing the user should do next under things they cannot do
        yet.
        """
        from drivers.inventory import job as find_job

        return [dep for dep in find_job(job_id).requires
                if not self.has_succeeded(dep, tag)]
