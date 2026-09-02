"""Score discovered jobs against the profile and the config's role families."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .models import Job

_YEARS = re.compile(r"(\d+)\+?\s*(?:-\s*\d+\s*)?year", re.I)


def _title_score(title: str, cfg: dict) -> tuple[float, str | None, list[str]]:
    """Match a title against role families. Returns (score, family, reasons)."""
    t = title.lower()

    for bad in cfg.get("title_exclusions", []):
        if bad in t:
            return 0.0, None, [f"excluded title term: {bad!r}"]

    best_score, best_family, reasons = 0.0, None, []
    for key, fam in cfg.get("role_families", {}).items():
        if not any(m in t for m in fam.get("must", [])):
            continue
        score = 0.7
        hits = [b for b in fam.get("boost", []) if b.strip() and b.strip() in t]
        score += min(0.3, 0.1 * len(hits))
        score *= float(fam.get("weight", 1.0))
        if score > best_score:
            best_score, best_family = score, key
            reasons = [f"title matches {fam['label']}"]
            if hits:
                reasons.append(f"title boosts: {', '.join(hits[:4])}")
    return min(1.0, best_score), best_family, reasons


def _location_score(job: Job, cfg: dict) -> tuple[float, list[str]]:
    raw = (job.location or "").strip()
    loc = raw.lower()
    if not loc or loc in ("n/a", "na", "-", "various", "multiple locations"):
        return (0.45, ["location unspecified"]) if cfg["locations"].get("allow_unknown_country", True) \
            else (0.0, ["no location"])

    # Foreign offices first. Stripe has a big Dublin, Ireland presence, so a
    # bare "Dublin" must not be mistaken for Dublin, CA.
    for country in cfg["locations"].get("exclude_countries", []):
        if re.search(rf"\b{re.escape(country)}\b", loc):
            return 0.0, [f"non-US location: {raw}"]

    tiers = cfg["locations"].get("preferred", [])
    for i, tier in enumerate(tiers):
        for term in tier:
            if re.search(rf"\b{re.escape(term)}\b", loc):
                return max(0.2, 1.0 - i * 0.2), [f"location tier {i + 1}: {term}"]

    # Word-boundary country match - a substring test scores "Austin" as a hit
    # on "us", which silently let foreign roles through.
    countries = cfg["locations"].get("require_country", [])
    if any(re.search(rf"\b{re.escape(c)}\b", loc) for c in countries):
        return 0.3, ["US location, outside preferred metros"]
    if re.search(r",\s*[A-Z]{2}\b", raw):
        return 0.3, ["looks like a US state code"]
    return 0.0, [f"non-US or unrecognised location: {raw}"]


def _skill_score(job: Job, profile: dict) -> tuple[float, list[str]]:
    text = f"{job.title} {job.department} {job.description}".lower()
    if not text.strip():
        return 0.5, []
    skills = {s.lower() for group in profile.get("skills", {}).values() for s in group}
    hits = sorted({s for s in skills if s in text})
    if not hits:
        return 0.15, []
    return min(1.0, 0.3 + 0.07 * len(hits)), [f"skill overlap ({len(hits)}): {', '.join(hits[:6])}"]


def _freshness_score(job: Job) -> tuple[float, list[str]]:
    raw = (job.posted_at or "").strip()
    if not raw:
        return 0.5, []
    low = raw.lower()
    # Workday returns prose like "Posted 30+ Days Ago".
    if "today" in low or "just posted" in low:
        return 1.0, ["posted today"]
    m = re.search(r"(\d+)\+?\s*days?", low)
    if m:
        days = int(m.group(1))
        return max(0.1, 1.0 - days / 45.0), [f"posted ~{days}d ago"]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            dt = datetime.strptime(raw[:26].replace("Z", ""), fmt).replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - dt).days
            return max(0.1, 1.0 - days / 45.0), [f"posted ~{days}d ago"]
        except ValueError:
            continue
    return 0.5, []


def _years_required(job: Job) -> int | None:
    """Largest 'N years experience' figure mentioned in the posting."""
    text = f"{job.title} {job.description}"
    found = [int(m) for m in _YEARS.findall(text) if int(m) < 30]
    return max(found) if found else None


def score_job(job: Job, cfg: dict, profile: dict) -> Job:
    w = cfg["scoring"]["weights"]

    t_score, family, t_reasons = _title_score(job.title, cfg)
    if t_score == 0.0:
        job.score, job.reasons = 0.0, t_reasons or ["title did not match any role family"]
        return job

    l_score, l_reasons = _location_score(job, cfg)
    # A location we've positively rejected is disqualifying, not merely a low
    # component - a strong title score would otherwise drag a Bucharest or
    # Bangalore posting over the threshold, and neither is reachable on OPT.
    if l_score == 0.0:
        job.score, job.reasons = 0.0, l_reasons
        return job

    s_score, s_reasons = _skill_score(job, profile)
    f_score, f_reasons = _freshness_score(job)

    total = (
        w["title_match"] * t_score
        + w["location"] * l_score
        + w["skill_overlap"] * s_score
        + w["freshness"] * f_score
    )

    reasons = t_reasons + l_reasons + s_reasons + f_reasons

    years = _years_required(job)
    cap = cfg.get("max_years_experience", 5)
    if years is not None and years > cap:
        total *= 0.35
        reasons.append(f"wants {years}+ yrs experience (cap {cap}) - heavily penalised")
    elif years is not None:
        reasons.append(f"asks for {years} yrs experience")

    if family:
        reasons.insert(0, f"family={family}")

    job.score = round(total, 4)
    job.reasons = reasons
    return job


def rank(jobs: list[Job], cfg: dict, profile: dict) -> list[Job]:
    """Score, drop below-threshold rows, dedupe, and sort best-first."""
    scored = [score_job(j, cfg, profile) for j in jobs]
    floor = cfg["scoring"].get("min_score", 0.35)
    kept: dict[str, Job] = {}
    for j in scored:
        if j.score < floor:
            continue
        prev = kept.get(j.key)
        if prev is None or j.score > prev.score:
            kept[j.key] = j
    return sorted(kept.values(), key=lambda j: j.score, reverse=True)
