"""Connectors for large employers running bespoke career portals.

These endpoints are less stable than the vendor board APIs, so every connector
here is allowed to fail: discover.py catches the error and falls back to
`manual_search_url`, which emits a pre-filtered search link to click through.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urlencode

import httpx

from ..models import Company, Job
from .boards import _remote, _text


def amazon(company: Company, client: httpx.Client, queries: list[str], limit: int = 100) -> list[Job]:
    """amazon.jobs exposes a JSON search endpoint used by its own front end."""
    jobs: list[Job] = []
    seen: set[str] = set()
    for q in queries:
        offset = 0
        while offset < limit:
            r = client.get(
                "https://www.amazon.jobs/en/search.json",
                params={
                    "base_query": q,
                    "country": "USA",
                    "result_limit": 100,
                    "offset": offset,
                    "sort": "recent",
                },
                timeout=30.0,
            )
            r.raise_for_status()
            payload = r.json()
            batch = payload.get("jobs", [])
            for j in batch:
                jid = str(j.get("id_icims") or j.get("id") or "")
                if jid in seen:
                    continue
                seen.add(jid)
                loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country_code")] if x)
                path = j.get("job_path", "") or ""
                jobs.append(Job(
                    company=company.name,
                    title=j.get("title", ""),
                    url=f"https://www.amazon.jobs{path}" if path else company.portal,
                    location=loc,
                    ats="amazon",
                    job_id=jid,
                    department=j.get("business_category", "") or "",
                    posted_at=j.get("posted_date", "") or "",
                    description=_text(
                        (j.get("description") or "") + " " + (j.get("basic_qualifications") or "")
                    ),
                    remote=_remote(loc),
                ))
            offset += len(batch)
            if len(batch) < 100 or offset >= payload.get("hits", 0):
                break
    return jobs


def microsoft(company: Company, client: httpx.Client, queries: list[str], limit: int = 100) -> list[Job]:
    """jobs.careers.microsoft.com search API (gcsservices)."""
    jobs: list[Job] = []
    seen: set[str] = set()
    for q in queries:
        page = 1
        while page <= max(1, limit // 20):
            r = client.get(
                "https://gcsservices.careers.microsoft.com/search/api/v1/search",
                params={"q": q, "l": "en_us", "pg": page, "pgSz": 20, "o": "Recent", "flt": "true"},
                headers={"Accept": "application/json", "Referer": "https://jobs.careers.microsoft.com/"},
                timeout=30.0,
            )
            r.raise_for_status()
            operation = r.json().get("operationResult", {}).get("result", {})
            batch = operation.get("jobs", [])
            for j in batch:
                jid = str(j.get("jobId", ""))
                if jid in seen:
                    continue
                seen.add(jid)
                props = j.get("properties", {}) or {}
                locs = props.get("locations") or []
                loc = ", ".join(locs[:2]) if locs else (props.get("primaryLocation") or "")
                jobs.append(Job(
                    company=company.name,
                    title=j.get("title", ""),
                    url=f"https://jobs.careers.microsoft.com/global/en/job/{jid}",
                    location=loc,
                    ats="microsoft",
                    job_id=jid,
                    department=props.get("profession", "") or "",
                    posted_at=j.get("postingDate", "") or "",
                    description=_text(props.get("description")),
                    remote=_remote(loc) or str(props.get("workSiteFlexibility", "")).lower().startswith("up to 100"),
                ))
            page += 1
            if len(batch) < 20:
                break
    return jobs


# --- fallback: build a pre-filtered search URL to click through -----------

_SEARCH_TEMPLATES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"metacareers\.com"), "https://www.metacareers.com/jobs?q={q}"),
    (re.compile(r"google\.com/about/careers"),
     "https://www.google.com/about/careers/applications/jobs/results/?q={q}&location=United%20States"),
    (re.compile(r"apple\.com/careers"), "https://jobs.apple.com/en-us/search?search={q}&location=united-states-USA"),
    (re.compile(r"icims\.com"), "{portal}/jobs/search?searchKeyword={q}"),
    (re.compile(r"eightfold\.ai"), "{portal}?query={q}"),
    (re.compile(r"successfactors|sapsf"), "{portal}?searchKeyword={q}"),
]


def manual_search_url(company: Company, query: str) -> str:
    """Best-effort pre-filtered search link for portals we can't query directly."""
    q = quote_plus(query)
    portal = (company.portal or "").rstrip("/")
    for pattern, template in _SEARCH_TEMPLATES:
        if pattern.search(company.portal or ""):
            return template.format(q=q, portal=portal)
    # Generic guess: most career sites accept ?q= or ?search=
    sep = "&" if "?" in portal else "?"
    return f"{portal}{sep}{urlencode({'q': query})}"


REGISTRY = {
    "amazon": amazon,
    "microsoft": microsoft,
}
