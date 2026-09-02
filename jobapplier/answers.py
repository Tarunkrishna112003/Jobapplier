"""Resolve an arbitrary form-field label to a value from the profile.

Rules are ordered and first-match-wins, so put specific patterns above general
ones. Anything that matches nothing comes back as `None` and is surfaced in the
review step as an unanswered field rather than being guessed at.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from .models import FieldFill

Rule = tuple[re.Pattern, Callable[[dict], Optional[str]], str]


def _g(profile: dict, *path: str, default: str = "") -> str:
    cur: Any = profile
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return str(cur) if cur is not None else default


def _clean(v: str) -> str:
    """TODO_CONFIRM placeholders must never reach a real form."""
    return "" if v.strip().upper().startswith("TODO_CONFIRM") else v


def build_rules() -> list[Rule]:
    P = _g
    r = re.compile
    return [
        # --- work authorisation: most consequential, matched first ---------
        (r(r"legally\s+authoriz|authoriz(ed|ation)\s+to\s+work|eligible\s+to\s+work|right\s+to\s+work|work\s+authorization\s+status", re.I),
         lambda p: "Yes" if p.get("work_authorization", {}).get("authorized_to_work_us") else "No",
         "work_auth.authorized"),

        (r(r"(require|need|sponsor).{0,40}(sponsor|visa|h-?1b|immigration)|sponsorship.{0,30}(now|future|required)|will\s+you\s+.{0,30}require", re.I),
         lambda p: P(p, "work_authorization", "requires_sponsorship_answer", default="Yes"),
         "work_auth.sponsorship"),

        (r(r"\bvisa\s+(type|status)\b|current\s+immigration|citizenship\s+status", re.I),
         lambda p: P(p, "work_authorization", "status"), "work_auth.status"),

        # --- identity ------------------------------------------------------
        (r(r"^\s*(first|given|fore)\s*name|legal\s+first", re.I),
         lambda p: P(p, "identity", "first_name"), "identity.first_name"),
        (r(r"^\s*(last|family|sur)\s*name|legal\s+last", re.I),
         lambda p: P(p, "identity", "last_name"), "identity.last_name"),
        (r(r"preferred\s+(first\s+)?name|nick\s*name|what.{0,20}call\s+you", re.I),
         lambda p: P(p, "identity", "preferred_name"), "identity.preferred_name"),
        (r(r"middle\s*name|middle\s+initial", re.I), lambda p: "", "identity.middle_name"),
        (r(r"full\s*name|^\s*name\s*$|your\s+name", re.I),
         lambda p: P(p, "identity", "full_name"), "identity.full_name"),
        (r(r"e-?mail", re.I), lambda p: P(p, "identity", "email"), "identity.email"),
        (r(r"country\s+code", re.I), lambda p: P(p, "identity", "phone_country_code"), "identity.phone_cc"),
        (r(r"phone|mobile|cell|telephone|contact\s+number", re.I),
         lambda p: P(p, "identity", "phone"), "identity.phone"),

        # --- address -------------------------------------------------------
        (r(r"street|address\s*(line)?\s*1|^\s*address\s*$|mailing\s+address", re.I),
         lambda p: P(p, "address", "street"), "address.street"),
        (r(r"address\s*(line)?\s*2|apt|suite|unit", re.I), lambda p: "", "address.line2"),
        (r(r"city\s+and\s+state|city\s*,\s*state|city\s*/\s*state", re.I),
         lambda p: f'{P(p, "address", "city")}, {P(p, "address", "state")}'.strip(", "),
         "address.city_state"),
        (r(r"\bcity\b|town|locality", re.I), lambda p: P(p, "address", "city"), "address.city"),
        (r(r"\b(state|province|region)\b", re.I),
         lambda p: P(p, "address", "state_full"), "address.state"),
        (r(r"zip|postal", re.I), lambda p: P(p, "address", "postal_code"), "address.zip"),
        (r(r"\bcountry\b", re.I), lambda p: P(p, "address", "country"), "address.country"),

        # --- links ---------------------------------------------------------
        (r(r"linked\s*in", re.I), lambda p: P(p, "links", "linkedin"), "links.linkedin"),
        (r(r"git\s*hub", re.I), lambda p: P(p, "links", "github"), "links.github"),
        (r(r"portfolio|personal\s+(web)?site|website|\burl\b", re.I),
         lambda p: P(p, "links", "portfolio") or P(p, "links", "linkedin"), "links.portfolio"),

        # --- education -----------------------------------------------------
        (r(r"school|university|college|institution", re.I),
         lambda p: (p.get("education") or [{}])[0].get("school", ""), "education.school"),
        (r(r"\bdegree\b|qualification", re.I),
         lambda p: (p.get("education") or [{}])[0].get("degree", ""), "education.degree"),
        (r(r"discipline|major|field\s+of\s+study|concentration", re.I),
         lambda p: (p.get("education") or [{}])[0].get("field", ""), "education.field"),
        (r(r"\bgpa\b|grade\s+point", re.I),
         lambda p: (p.get("education") or [{}])[0].get("gpa", ""), "education.gpa"),
        (r(r"graduation\s+(date|year)|expected\s+grad|end\s+date.{0,20}school", re.I),
         lambda p: (p.get("education") or [{}])[0].get("end", ""), "education.end"),

        # --- current employment ---------------------------------------------
        (r(r"(current|previous|recent|last)[\w\s/]{0,20}(employer|company\s+name)", re.I),
         lambda p: (p.get("experience") or [{}])[0].get("company", ""), "experience.company"),
        (r(r"(current|previous|recent|last)[\w\s/]{0,20}(job\s+)?(title|role|position)|job\s+title", re.I),
         lambda p: (p.get("experience") or [{}])[0].get("title", ""), "experience.title"),

        # --- screening -------------------------------------------------------
        (r(r"years?\s+of\s+(relevant\s+)?experience|how\s+many\s+years", re.I),
         lambda p: P(p, "screening_defaults", "years_of_experience"), "screening.years"),
        (r(r"how\s+did\s+you\s+(hear|find|learn)|referral\s+source|source\b", re.I),
         lambda p: P(p, "screening_defaults", "how_did_you_hear"), "screening.source"),
        (r(r"(ever|previously|before).{0,30}(been\s+)?(employed|worked)|former\s+employee|"
            r"worked\s+(for|at)\s+(us|this)", re.I),
         lambda p: P(p, "screening_defaults", "previously_employed_here"), "screening.prev_employed"),
        (r(r"relative|family\s+member|related\s+to.{0,20}employee|know\s+anyone", re.I),
         lambda p: P(p, "screening_defaults", "related_to_employee"), "screening.relative"),
        (r(r"non-?compete|restrictive\s+covenant", re.I),
         lambda p: P(p, "screening_defaults", "non_compete"), "screening.noncompete"),
        (r(r"convicted|criminal|felony", re.I),
         lambda p: P(p, "screening_defaults", "criminal_record"), "screening.criminal"),
        (r(r"18\s+years|over\s+18|age\s+of\s+majority", re.I),
         lambda p: P(p, "screening_defaults", "over_18"), "screening.over18"),
        (r(r"background\s+check", re.I),
         lambda p: P(p, "screening_defaults", "willing_background_check"), "screening.bgcheck"),
        (r(r"drug\s+(test|screen)", re.I),
         lambda p: P(p, "screening_defaults", "willing_drug_test"), "screening.drugtest"),
        (r(r"security\s+clearance", re.I),
         lambda p: P(p, "screening_defaults", "government_security_clearance"), "screening.clearance"),
        (r(r"essential\s+function|reasonable\s+accommodation.{0,30}perform", re.I),
         lambda p: P(p, "screening_defaults", "can_perform_essential_functions"), "screening.essential"),
        (r(r"driver'?s?\s+licen[sc]e", re.I),
         lambda p: P(p, "screening_defaults", "drivers_license"), "screening.license"),

        # --- preferences -----------------------------------------------------
        (r(r"(desired|expected|target).{0,20}(salary|compensation|pay)|salary\s+(expectation|requirement)|compensation\s+expectation", re.I),
         lambda p: P(p, "preferences", "desired_salary_number") or P(p, "preferences", "desired_salary"),
         "preferences.salary"),
        (r(r"(available|earliest|preferred).{0,20}start\s+date|when\s+can\s+you\s+start|notice\s+period", re.I),
         lambda p: P(p, "preferences", "earliest_start_date"), "preferences.start_date"),
        (r(r"relocat", re.I),
         lambda p: "Yes" if p.get("preferences", {}).get("open_to_relocate") else "No", "preferences.relocate"),
        (r(r"\bremote\b|work\s+from\s+home|hybrid|on-?site\s+preference", re.I),
         lambda p: "Yes" if p.get("preferences", {}).get("open_to_remote") else "No", "preferences.remote"),
        (r(r"willing\s+to\s+travel|travel\s+requirement", re.I),
         lambda p: "Yes" if p.get("preferences", {}).get("willing_to_travel") else "No", "preferences.travel"),
        (r(r"preferred\s+(location|work\s+location)|location\s+preference", re.I),
         lambda p: ", ".join(p.get("preferences", {}).get("locations_preferred", [])[:3]),
         "preferences.location"),

        # --- voluntary self-ID (always declines unless profile says otherwise)
        (r(r"hispanic|latino", re.I),
         lambda p: P(p, "voluntary_self_id", "hispanic_latino"), "eeo.hispanic"),
        (r(r"race|ethnic", re.I),
         lambda p: P(p, "voluntary_self_id", "race_ethnicity"), "eeo.race"),
        (r(r"\bgender\b|\bsex\b", re.I),
         lambda p: P(p, "voluntary_self_id", "gender"), "eeo.gender"),
        (r(r"veteran|military|protected\s+vet", re.I),
         lambda p: P(p, "voluntary_self_id", "veteran_status"), "eeo.veteran"),
        (r(r"disabilit|impairment", re.I),
         lambda p: P(p, "voluntary_self_id", "disability_status"), "eeo.disability"),
        (r(r"pronoun", re.I), lambda p: "", "eeo.pronouns"),

        # Marketing / messaging opt-ins - default to declining.
        (r(r"opt[- ]?in|receive.{0,30}(message|text|sms|whatsapp|email)|"
            r"marketing\s+communication|subscribe", re.I),
         lambda p: P(p, "screening_defaults", "marketing_opt_in", default="No"),
         "screening.opt_in"),
    ]


# Placeholder captions that carry no question - never treat these as fields.
_JUNK_LABEL = re.compile(
    r"^(select\.{0,3}|choose\.{0,3}|please\s+select\.{0,3}|--+|none|n/?a|\s*)$", re.I)

_RULES = build_rules()


def resolve(label: str, profile: dict) -> Optional[FieldFill]:
    """Return a FieldFill for `label`, or None when no rule matches."""
    text = " ".join((label or "").split())
    if not text or _JUNK_LABEL.match(text):
        return None
    for pattern, getter, source in _RULES:
        if pattern.search(text):
            try:
                value = _clean(str(getter(profile) or ""))
            except Exception:
                return None
            if value == "":
                return None
            return FieldFill(selector="", label=text, value=value, source=source, confidence=0.9)
    return None


def choose_option(value: str, options: list[str]) -> Optional[str]:
    """Pick the option from a <select>/radio group that best expresses `value`.

    Matching is deliberately generous - boards phrase the same answer many ways
    ("Yes", "Yes, I am authorized", "I am legally authorized to work").
    """
    if not options:
        return None
    v = value.strip().lower()
    opts = [(o, o.strip().lower()) for o in options if o and o.strip()]

    for orig, low in opts:                      # exact
        if low == v:
            return orig

    # Yes/No answers: match on the leading token, avoiding "no" inside "north".
    if v in ("yes", "no"):
        for orig, low in opts:
            if re.match(rf"^{v}\b", low):
                return orig

    for orig, low in opts:                      # containment, either direction
        if v and (v in low or low in v):
            return orig

    # Decline-to-answer variants are worded differently on every board.
    if any(k in v for k in ("decline", "not to answer", "don't wish", "do not want", "prefer not")):
        for orig, low in opts:
            if any(k in low for k in ("decline", "not to answer", "wish not", "don't wish",
                                       "do not want", "prefer not", "not disclose")):
                return orig

    # Token overlap as a last resort.
    vt = set(re.findall(r"[a-z]+", v))
    best, best_overlap = None, 0
    for orig, low in opts:
        overlap = len(vt & set(re.findall(r"[a-z]+", low)))
        if overlap > best_overlap:
            best, best_overlap = orig, overlap
    return best if best_overlap >= 1 else None
