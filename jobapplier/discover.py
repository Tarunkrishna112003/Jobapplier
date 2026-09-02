"""Discovery: resolve each company's ATS, pull live postings, score, store."""
from __future__ import annotations

import concurrent.futures as cf
import json
from pathlib import Path
from typing import Any

import httpx

from . import ats as ats_mod
from .companies import load_companies, load_companies_json, save_companies
from .connectors import boards, custom
from .match import rank
from .models import Company, Job

UA = boards.UA

# Upper bound on postings pulled from one company before scoring.
_RAW_SAFETY_LIMIT = 3000


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"},
        timeout=timeout,
        follow_redirects=True,
    )


def resolve_companies(cfg: dict, refresh: bool = False, progress=None) -> list[Company]:
    """Load companies and stamp each with its detected ATS, caching to JSON."""
    cache = Path(cfg["storage"]["companies_json"])
    if cache.exists() and not refresh:
        return load_companies_json(cache)

    companies = load_companies(cfg["sources"]["companies_xlsx"], cfg["sources"].get("companies_sheet"))
    timeout = cfg["discovery"]["timeout_seconds"]

    def work(c: Company) -> Company:
        with _client(timeout) as client:
            try:
                return ats_mod.detect(c, client)
            except Exception:
                c.ats = "custom"
                return c

    with cf.ThreadPoolExecutor(max_workers=cfg["discovery"]["concurrency"]) as pool:
        futures = {pool.submit(work, c): c for c in companies}
        out = []
        for fut in cf.as_completed(futures):
            c = fut.result()
            out.append(c)
            if progress:
                progress(c)

    out.sort(key=lambda c: c.name)
    save_companies(out, cache)
    return out


def fetch_company_jobs(company: Company, cfg: dict) -> tuple[list[Job], str]:
    """Pull postings for one company. Returns (jobs, status message)."""
    timeout = cfg["discovery"]["timeout_seconds"]
    queries = cfg["discovery"]["search_queries"]

    with _client(timeout) as client:
        try:
            if company.ats in boards.REGISTRY:
                fn = boards.REGISTRY[company.ats]
                if company.ats == "workday":
                    jobs: list[Job] = []
                    seen: set[str] = set()
                    for q in queries:
                        for j in fn(company, client, search=q):
                            if j.key not in seen:
                                seen.add(j.key)
                                jobs.append(j)
                        if len(jobs) >= cap:
                            break
                else:
                    jobs = fn(company, client)
            elif company.ats in custom.REGISTRY:
                jobs = custom.REGISTRY[company.ats](company, client, queries, limit=cap)
            else:
                return [], f"no connector for ats={company.ats!r} - use `manual` links"
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}"

    # Deliberately NOT truncated to `cap` here: the cap limits how many jobs
    # we act on, and applying it before scoring would throw away good matches
    # purely because of the order the board returned them.
    return jobs[:_RAW_SAFETY_LIMIT], f"{len(jobs)} raw postings"


def discover(cfg: dict, profile: dict, companies: list[Company],
             progress=None) -> tuple[list[Job], list[dict[str, Any]]]:
    """Fan out across companies, then rank everything collected."""
    all_jobs: list[Job] = []
    report: list[dict[str, Any]] = []

    with cf.ThreadPoolExecutor(max_workers=cfg["discovery"]["concurrency"]) as pool:
        futures = {pool.submit(fetch_company_jobs, c, cfg): c for c in companies}
        for fut in cf.as_completed(futures):
            company = futures[fut]
            try:
                jobs, msg = fut.result()
            except Exception as exc:
                jobs, msg = [], f"{type(exc).__name__}: {exc}"
            all_jobs.extend(jobs)
            entry = {"company": company.name, "ats": company.ats, "raw": len(jobs), "note": msg}
            report.append(entry)
            if progress:
                progress(entry)

    ranked = rank(all_jobs, cfg, profile)
    return ranked, report


def manual_links(companies: list[Company], cfg: dict) -> list[dict[str, str]]:
    """Pre-filtered search URLs for portals with no usable API."""
    out = []
    query = cfg["discovery"]["search_queries"][0]
    for c in companies:
        if c.ats in boards.REGISTRY or c.ats in custom.REGISTRY:
            continue
        out.append({
            "company": c.name,
            "sector": c.sector,
            "portal": c.portal,
            "ats": c.ats,
            "search_url": custom.manual_search_url(c, query),
        })
    return sorted(out, key=lambda d: d["company"])


def save_jobs(jobs: list[Job], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([j.to_dict() for j in jobs], indent=2))
    return p
