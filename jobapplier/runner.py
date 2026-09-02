"""Sequential run engine behind the web UI.

Design constraint from the brief: one company at a time, no overlap. A single
worker thread owns the entire run - and the Playwright browser with it, since
the sync API is not thread-safe. A run lock makes a second Start a no-op while
one is already going, so two runs can never interleave on the same browser.

The worker blocks on `_decision` whenever a filled application needs a verdict,
so nothing is ever submitted without an explicit click from the UI.
"""
from __future__ import annotations

import json
import threading
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .db import Tracker
from .discover import fetch_company_jobs, resolve_companies
from .match import rank
from .models import (
    Company, STATUS_FAILED, STATUS_FILLED, STATUS_NEEDS_LOGIN,
    STATUS_QUEUED, STATUS_SKIPPED, STATUS_SUBMITTED,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PendingJob:
    """An application that's been filled and is waiting for a verdict."""
    key: str
    company: str
    title: str
    location: str
    url: str
    score: float
    fields: list[dict[str, Any]] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)
    screenshot: str = ""
    note: str = ""


@dataclass
class RunState:
    running: bool = False
    stopping: bool = False
    phase: str = "idle"                 # idle | resolving | company | awaiting | done | error
    company_index: int = 0
    company_total: int = 0
    current_company: str = ""
    pending: Optional[dict] = None       # PendingJob awaiting a decision
    log: list[dict[str, str]] = field(default_factory=list)
    tally: dict[str, int] = field(default_factory=lambda: {
        "companies_done": 0, "jobs_found": 0, "filled": 0,
        "submitted": 0, "skipped": 0, "failed": 0, "needs_login": 0,
    })
    error: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Runner:
    """Owns one sequential run. Thread-safe for the API to read/poke."""

    MAX_LOG = 400

    def __init__(self, cfg: dict, profile: dict):
        self.cfg = cfg
        self.profile = profile
        self.state = RunState()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._decision: Optional[str] = None
        self._decision_ready = threading.Event()
        self._stop = threading.Event()

    # --- state helpers ----------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return self.state.to_dict()

    def _log(self, level: str, message: str) -> None:
        with self._lock:
            self.state.log.append({"ts": _now(), "level": level, "message": message})
            if len(self.state.log) > self.MAX_LOG:
                del self.state.log[: len(self.state.log) - self.MAX_LOG]

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self.state, k, v)

    def _bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.state.tally[key] = self.state.tally.get(key, 0) + n

    # --- control ----------------------------------------------------------

    def start(self, companies_filter: str = "", max_jobs_per_company: int = 3,
              min_score: float = 0.0, dry_run: bool = False) -> bool:
        """Begin a run. Returns False if one is already in flight."""
        with self._lock:
            if self.state.running:
                return False
            self.state = RunState(running=True, phase="resolving", started_at=_now())
        self._stop.clear()
        self._decision = None
        self._decision_ready.clear()

        self._thread = threading.Thread(
            target=self._run_guarded,
            args=(companies_filter, max_jobs_per_company, min_score, dry_run),
            daemon=True,
            name="jobapplier-runner",
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._set(stopping=True)
        # Unblock a worker parked on a decision.
        self._decision = "skip"
        self._decision_ready.set()
        self._log("warn", "Stop requested — finishing the current step.")

    def decide(self, key: str, action: str) -> bool:
        """Record the UI's verdict for the pending application."""
        with self._lock:
            pending = self.state.pending
            if not pending or pending["key"] != key:
                return False
        self._decision = action
        self._decision_ready.set()
        return True

    def _await_decision(self, timeout: float = 3600.0) -> str:
        self._decision_ready.wait(timeout=timeout)
        self._decision_ready.clear()
        d = self._decision or "skip"
        self._decision = None
        return d

    # --- the run ----------------------------------------------------------

    def _run_guarded(self, *args) -> None:
        try:
            self._run(*args)
        except Exception:
            self._log("error", traceback.format_exc(limit=3))
            self._set(phase="error", error=traceback.format_exc(limit=3))
        finally:
            self._set(running=False, stopping=False, pending=None, finished_at=_now())
            if self.snapshot()["phase"] not in ("error",):
                self._set(phase="done")
            self._log("info", "Run finished.")

    def _run(self, companies_filter: str, max_jobs: int, min_score: float, dry_run: bool) -> None:
        from .fill import Filler
        from .review import submit_job

        self._log("info", "Resolving company list and ATS backends…")
        companies = resolve_companies(self.cfg, refresh=False)
        if companies_filter:
            needle = companies_filter.lower()
            companies = [c for c in companies if needle in c.name.lower()]
        companies.sort(key=lambda c: c.name)
        self._set(company_total=len(companies))
        self._log("info", f"{len(companies)} companies queued. Processing one at a time.")

        tracker = Tracker(self.cfg["storage"]["db"])
        try:
            # One browser for the whole run, owned by this thread only.
            with Filler(self.profile, self.cfg) as filler:
                for idx, company in enumerate(companies, 1):
                    if self._stop.is_set():
                        self._log("warn", "Stopped before finishing the company list.")
                        break

                    self._set(phase="company", company_index=idx, current_company=company.name)
                    self._log("info", f"[{idx}/{len(companies)}] {company.name} — searching ({company.ats})")

                    jobs = self._jobs_for(company, tracker, min_score, max_jobs)
                    if not jobs:
                        self._log("dim", f"{company.name}: no matching openings")
                        self._bump("companies_done")
                        continue

                    self._bump("jobs_found", len(jobs))
                    self._log("info", f"{company.name}: {len(jobs)} matching role(s)")

                    for row in jobs:
                        if self._stop.is_set():
                            break
                        self._process_one(row, filler, tracker, submit_job, dry_run)

                    self._bump("companies_done")
        finally:
            tracker.close()

    def _jobs_for(self, company: Company, tracker: Tracker,
                  min_score: float, max_jobs: int) -> list:
        """Fetch + score this company's openings, then return the top queued rows."""
        try:
            found, note = fetch_company_jobs(company, self.cfg)
        except Exception as exc:
            self._log("error", f"{company.name}: discovery failed — {type(exc).__name__}: {exc}")
            return []

        if not found:
            if note and "no connector" in note:
                self._log("dim", f"{company.name}: {note}")
            return []

        ranked = rank(found, self.cfg, self.profile)
        if ranked:
            tracker.upsert_jobs(ranked)

        floor = max(min_score, self.cfg["scoring"].get("min_score", 0.35))
        return tracker.queue(limit=max_jobs, status=STATUS_QUEUED,
                             company=company.name, min_score=floor)

    def _process_one(self, row, filler, tracker: Tracker, submit_job, dry_run: bool) -> None:
        """Fill one application, park it, and wait for the UI's verdict."""
        self._log("info", f"  filling: {row['title'][:60]}")
        try:
            result = filler.prepare(row["url"], row["key"])
        except Exception as exc:
            tracker.set_status(row["key"], STATUS_FAILED, f"{type(exc).__name__}: {exc}")
            self._bump("failed")
            self._log("error", f"  {row['title'][:44]} — {type(exc).__name__}: {exc}")
            return

        tracker.set_status(row["key"], result.status, result.note,
                           filled_fields=result.fields, screenshot=result.screenshot)

        if result.status == STATUS_NEEDS_LOGIN:
            self._bump("needs_login")
            self._log("warn", f"  {row['company']} needs a login — log in once in the browser "
                              f"window; the session is saved for next time.")
            return
        if result.status != STATUS_FILLED:
            self._bump("failed")
            self._log("error", f"  could not fill: {result.note}")
            return

        self._bump("filled")

        pending = PendingJob(
            key=row["key"], company=row["company"], title=row["title"],
            location=row["location"] or "", url=row["url"], score=row["score"],
            fields=result.fields, unanswered=result.unanswered,
            screenshot=result.screenshot, note=result.note,
        )
        self._set(phase="awaiting", pending=asdict(pending))
        self._log("info", f"  ready for review: {len(result.fields)} fields filled, "
                          f"{len(result.unanswered)} blank")

        action = self._await_decision()
        self._set(phase="company", pending=None)

        if action == "submit":
            ok = submit_job(filler, tracker.get(row["key"]), tracker, dry_run=dry_run)
            if ok:
                self._bump("submitted")
                self._log("ok", f"  ✓ submitted — {row['company']}: {row['title'][:44]}")
            else:
                self._bump("failed")
                self._log("error", f"  ✗ submit failed — {row['company']}: {row['title'][:44]}")
        else:
            tracker.set_status(row["key"], STATUS_SKIPPED, "skipped in web UI")
            self._bump("skipped")
            self._log("dim", f"  skipped — {row['title'][:44]}")
