"""Job-listing connectors.

Each connector takes a Company and returns live Job rows. These all use the
public, unauthenticated job-board APIs the ATS vendors expose for embedding
career pages - no scraping, no login, and stable response shapes.
"""
from __future__ import annotations

import html
import re
from typing import Callable

import httpx

from ..models import Company, Job

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

_TAG = re.compile(r"<[^>]+>")


def _text(raw: str | None, limit: int = 4000) -> str:
    """Flatten an HTML job description to plain text for keyword matching."""
    if not raw:
        return ""
    return html.unescape(_TAG.sub(" ", raw)).replace("\xa0", " ").strip()[:limit]


def _remote(location: str) -> bool:
    return bool(re.search(r"\bremote\b|\bwork from home\b|\banywhere\b", location, re.I))


# --- Greenhouse -----------------------------------------------------------

def greenhouse(company: Company, client: httpx.Client) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company.ats_token}/jobs"
    r = client.get(url, params={"content": "true"}, timeout=30.0)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        jobs.append(Job(
            company=company.name,
            title=j.get("title", ""),
            url=j.get("absolute_url", ""),
            location=loc,
            ats="greenhouse",
            job_id=str(j.get("id", "")),
            department=", ".join(d.get("name", "") for d in j.get("departments", []) or []),
            posted_at=j.get("updated_at", "") or "",
            description=_text(j.get("content")),
            remote=_remote(loc),
        ))
    return jobs


# --- Lever ----------------------------------------------------------------

def lever(company: Company, client: httpx.Client) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{company.ats_token}"
    r = client.get(url, params={"mode": "json"}, timeout=30.0)
    r.raise_for_status()
    jobs = []
    for j in r.json():
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ""
        jobs.append(Job(
            company=company.name,
            title=j.get("text", ""),
            url=j.get("hostedUrl", ""),
            location=loc,
            ats="lever",
            job_id=str(j.get("id", "")),
            department=cats.get("team", "") or cats.get("department", "") or "",
            posted_at=str(j.get("createdAt", "")),
            description=_text(j.get("descriptionPlain") or j.get("description")),
            remote=_remote(loc) or (cats.get("commitment", "") or "").lower() == "remote",
        ))
    return jobs


# --- Ashby ----------------------------------------------------------------

def ashby(company: Company, client: httpx.Client) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company.ats_token}"
    r = client.get(url, params={"includeCompensation": "true"}, timeout=30.0)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        loc = j.get("location", "") or ""
        jobs.append(Job(
            company=company.name,
            title=j.get("title", ""),
            url=j.get("jobUrl", "") or j.get("applyUrl", ""),
            location=loc,
            ats="ashby",
            job_id=str(j.get("id", "")),
            department=j.get("department", "") or j.get("team", "") or "",
            posted_at=j.get("publishedAt", "") or "",
            description=_text(j.get("descriptionHtml") or j.get("descriptionPlain")),
            remote=bool(j.get("isRemote")) or _remote(loc),
        ))
    return jobs


# --- SmartRecruiters ------------------------------------------------------

def smartrecruiters(company: Company, client: httpx.Client) -> list[Job]:
    base = f"https://api.smartrecruiters.com/v1/companies/{company.ats_token}/postings"
    jobs, offset = [], 0
    while True:
        r = client.get(base, params={"limit": 100, "offset": offset}, timeout=30.0)
        r.raise_for_status()
        payload = r.json()
        batch = payload.get("content", [])
        for j in batch:
            loc = j.get("location") or {}
            loc_str = ", ".join(
                x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x
            )
            jobs.append(Job(
                company=company.name,
                title=j.get("name", ""),
                url=(j.get("ref") or "").replace("api.smartrecruiters.com/v1/companies", "jobs.smartrecruiters.com")
                    or f"https://jobs.smartrecruiters.com/{company.ats_token}/{j.get('id','')}",
                location=loc_str,
                ats="smartrecruiters",
                job_id=str(j.get("id", "")),
                department=(j.get("department") or {}).get("label", ""),
                posted_at=j.get("releasedDate", "") or "",
                remote=bool(loc.get("remote")) or _remote(loc_str),
            ))
        offset += len(batch)
        if len(batch) < 100 or offset >= payload.get("totalFound", 0):
            break
    return jobs


# --- Workday --------------------------------------------------------------

def workday(company: Company, client: httpx.Client, search: str = "") -> list[Job]:
    """Workday's CXS endpoint - POST, paginated 20 at a time."""
    host, tenant, site = company.ats_host, company.ats_token, company.ats_site
    if not (host and site):
        return []
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

    jobs, offset = [], 0
    while offset < 200:  # cap: 10 pages per company per run
        r = client.post(
            url,
            json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30.0,
        )
        if r.status_code >= 400:
            break
        payload = r.json()
        batch = payload.get("jobPostings", [])
        for j in batch:
            loc = j.get("locationsText", "") or ""
            path = j.get("externalPath", "") or ""
            jobs.append(Job(
                company=company.name,
                title=j.get("title", ""),
                url=f"https://{host}/{site}{path}" if path else company.portal,
                location=loc,
                ats="workday",
                job_id=str(j.get("bulletFields", [""])[0] if j.get("bulletFields") else path),
                posted_at=j.get("postedOn", "") or "",
                remote=_remote(loc),
            ))
        offset += len(batch)
        if len(batch) < 20 or offset >= payload.get("total", 0):
            break
    return jobs


REGISTRY: dict[str, Callable[..., list[Job]]] = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workday": workday,
}
