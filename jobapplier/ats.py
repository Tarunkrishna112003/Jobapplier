"""Work out which applicant-tracking system backs each company's careers page.

Portal URLs in the spreadsheet are corporate landing pages ("apple.com/careers"),
not ATS endpoints. Detection runs in three passes, cheapest first:

  1. Pattern-match the portal URL itself (already an ATS host).
  2. Fetch the page and sniff for ATS hosts in links, iframes and scripts.
  3. Fall back to a hand-maintained override table for large employers that
     run bespoke portals with their own public search APIs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import httpx

from .models import Company

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


@dataclass
class AtsMatch:
    ats: str
    token: str = ""
    site: str = ""
    host: str = ""


# --- pass 1: URL patterns -------------------------------------------------

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/(?:embed/)?[^/]*[?&]for=([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)")),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"([A-Za-z0-9_-]+)\.workable\.com")),
    ("recruitee", re.compile(r"([A-Za-z0-9_-]+)\.recruitee\.com")),
    ("icims", re.compile(r"([A-Za-z0-9_-]+)\.icims\.com")),
    ("taleo", re.compile(r"([A-Za-z0-9_-]+)\.taleo\.net")),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)")),
    ("successfactors", re.compile(r"([A-Za-z0-9_-]+)\.(?:successfactors|sapsf)\.(?:com|eu)")),
    ("eightfold", re.compile(r"([A-Za-z0-9_-]+)\.eightfold\.ai")),
]

# Workday needs three parts: host, tenant, career-site name.
#   https://<tenant>.wdN.myworkdayjobs.com/<lang>/<site>
_WORKDAY = re.compile(
    r"https?://([A-Za-z0-9_-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"
)


def match_url(url: str) -> Optional[AtsMatch]:
    """Pass 1 - recognise a URL that already points at a known ATS."""
    if not url:
        return None

    wd = _WORKDAY.search(url)
    if wd:
        host, site = wd.group(1), wd.group(2)
        tenant = host.split(".")[0]
        return AtsMatch(ats="workday", token=tenant, site=site, host=host)

    for name, pattern in _PATTERNS:
        m = pattern.search(url)
        if m:
            return AtsMatch(ats=name, token=m.group(1))
    return None


# --- pass 2: fetch and sniff ---------------------------------------------

def sniff_page(url: str, client: httpx.Client) -> Optional[AtsMatch]:
    """Pass 2 - pull the careers page and look for an ATS host inside it."""
    try:
        resp = client.get(url, follow_redirects=True, timeout=20.0)
    except Exception:
        return None
    if resp.status_code >= 400:
        return None

    # The redirect target itself is often the ATS.
    landed = match_url(str(resp.url))
    if landed:
        return landed

    html = resp.text[:600_000]
    # Score every ATS reference found in the body and take the most frequent,
    # so a stray footer link doesn't outvote the real embedded board.
    best: Optional[AtsMatch] = None
    best_count = 0
    for candidate_url in set(re.findall(r"https?://[^\s\"'<>\\)]+", html)):
        m = match_url(candidate_url)
        if not m:
            continue
        count = html.count(m.token)
        if count > best_count:
            best, best_count = m, count
    return best


# --- pass 2b: probe vendor APIs by company slug ---------------------------
# Careers landing pages are usually JS-rendered marketing shells with no ATS
# link in the served HTML, so sniffing alone leaves most companies unresolved.
# Guessing the board token from the company name and asking the vendor API
# directly is both cheaper and far more accurate - the endpoint only returns
# jobs when a board by that name genuinely exists.

_SLUG_NOISE = re.compile(r"\b(inc|corp|corporation|llc|ltd|company|holdings|plc)\b")

# Boards that exist but are vendor sandboxes rather than real job listings.
_TEST_TITLE = re.compile(r"test|bug\s*bash|do\s*not\s*apply|dummy|sample|^\d+$", re.I)
_MIN_REAL_JOBS = 5


def slug_variants(name: str) -> list[str]:
    """Candidate board tokens for a company name.

    Deliberately conservative: full-name slugs only. Matching on the first word
    alone produced false hits (Capital One -> "capital", General Motors ->
    "general") that pointed at unrelated companies' boards.
    """
    n = _SLUG_NOISE.sub("", name.lower().replace("&", " and "))
    base = re.sub(r"[^a-z0-9]+", "", n)
    dash = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    return [s for s in dict.fromkeys([base, dash]) if len(s) > 2]


_BOARD_PROBES: list[tuple[str, str, Optional[str]]] = [
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{s}/jobs", "jobs"),
    ("lever", "https://api.lever.co/v0/postings/{s}?mode=json", None),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/{s}", "jobs"),
    ("smartrecruiters", "https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=20", "content"),
]


def _is_real_board(items: list) -> bool:
    """Reject vendor sandbox boards - they poison discovery with fake jobs."""
    if len(items) < _MIN_REAL_JOBS:
        return False
    titles = []
    for j in items[:20]:
        if isinstance(j, dict):
            titles.append(str(j.get("title") or j.get("text") or j.get("name") or ""))
    if not titles:
        return False
    junk = sum(1 for t in titles if _TEST_TITLE.search(t.strip()))
    return junk / len(titles) < 0.3


def probe_boards(company: Company, client: httpx.Client) -> Optional[AtsMatch]:
    """Pass 2b - try the vendor job-board APIs with slugs of the company name."""
    for s in slug_variants(company.name):
        for ats, template, key in _BOARD_PROBES:
            try:
                r = client.get(template.format(s=s), timeout=12.0)
                if r.status_code != 200:
                    continue
                payload = r.json()
                items = payload.get(key) if key else payload
                if isinstance(items, list) and _is_real_board(items):
                    return AtsMatch(ats=ats, token=s)
            except Exception:
                continue
    return None


# --- pass 3: overrides for bespoke portals -------------------------------
# Large employers that run their own stack. Each has a connector in
# connectors/custom.py keyed by this name.
OVERRIDES: dict[str, AtsMatch] = {
    "Amazon": AtsMatch(ats="amazon", token="amazon"),
    "Amazon Web Services": AtsMatch(ats="amazon", token="amazon"),
    "Microsoft": AtsMatch(ats="microsoft", token="microsoft"),
    "Google": AtsMatch(ats="google", token="google"),
    "Meta": AtsMatch(ats="meta", token="meta"),
    "Apple": AtsMatch(ats="apple", token="apple"),
}


def detect(company: Company, client: httpx.Client | None = None, sniff: bool = True) -> Company:
    """Resolve and stamp the ATS fields on a company, in place."""
    override = OVERRIDES.get(company.name)
    if override:
        m = override
    else:
        m = match_url(company.portal)
        if m is None and sniff:
            owns_client = client is None
            client = client or httpx.Client(headers={"User-Agent": UA})
            try:
                m = sniff_page(company.portal, client)
                if m is None:
                    m = probe_boards(company, client)
            finally:
                if owns_client:
                    client.close()

    if m:
        company.ats = m.ats
        company.ats_token = m.token
        company.ats_site = m.site
        company.ats_host = m.host
    else:
        company.ats = "custom"
    return company
