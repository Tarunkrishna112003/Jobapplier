"""Core data types shared across discovery, matching and filling."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Company:
    name: str
    sector: str = ""
    linkedin: str = ""
    portal: str = ""
    sponsorship_evidence: str = ""
    notes: str = ""

    # Filled in by ats.detect()
    ats: str = "unknown"          # greenhouse | lever | ashby | smartrecruiters | workday | custom
    ats_token: str = ""           # board token / org slug / workday tenant
    ats_site: str = ""            # workday career-site name
    ats_host: str = ""            # workday host, e.g. amazon.wd5.myworkdayjobs.com

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Job:
    company: str
    title: str
    url: str
    location: str = ""
    ats: str = "unknown"
    job_id: str = ""
    department: str = ""
    posted_at: str = ""
    description: str = ""
    remote: bool = False

    # Filled in by match.score_job()
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable dedupe key. Same posting listed twice collapses to one row."""
        basis = f"{self.company.lower().strip()}|{self.title.lower().strip()}|{self.job_id or self.url}"
        return hashlib.sha1(basis.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        return d


@dataclass
class FieldFill:
    """One resolved answer for one form field, with provenance for the review UI."""
    selector: str
    label: str
    value: str
    kind: str = "text"            # text | select | radio | checkbox | file | textarea
    source: str = "profile"       # profile | derived | default | unanswered
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


STATUS_QUEUED = "queued"
STATUS_FILLED = "filled"          # form filled, parked at final Submit, awaiting review
STATUS_SUBMITTED = "submitted"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_NEEDS_LOGIN = "needs_login"
