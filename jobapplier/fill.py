"""Playwright form filler.

Opens a job's application form in a persistent browser profile (so logins
survive between runs), resolves every field it can from the profile, uploads
the resume, screenshots the completed form - and stops.

It never clicks the final Submit. Submission is a separate, explicit step in
review.py that you trigger per job after looking at what was filled.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import (
    Browser, BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright
)

from . import credentials as creds_mod
from .answers import choose_option, resolve
from .models import FieldFill

# Buttons that open the form. Ordered - the most explicit wins.
APPLY_PATTERNS = [
    r"^\s*apply\s+now\s*$", r"^\s*apply\s+for\s+this\s+job\s*$", r"^\s*apply\s*$",
    r"apply\s+manually", r"^\s*submit\s+application\s*$", r"start\s+your\s+application",
]

# Anything matching this is the real submit - we locate it to prove the form is
# complete, then deliberately leave it alone.
SUBMIT_PATTERNS = [
    r"^\s*submit\s+application\s*$", r"^\s*submit\s*$", r"^\s*send\s+application\s*$",
    r"^\s*apply\s*$", r"^\s*finish\s*$",
]

_SKIP_TYPES = {"hidden", "submit", "button", "image", "reset"}


@dataclass
class FillResult:
    status: str
    note: str = ""
    fields: list[dict[str, Any]] = None
    unanswered: list[str] = None
    screenshot: str = ""
    submit_selector: str = ""
    url: str = ""

    def __post_init__(self):
        self.fields = self.fields or []
        self.unanswered = self.unanswered or []


class Filler:
    def __init__(self, profile: dict, cfg: dict):
        self.profile = profile
        self.cfg = cfg
        self.resume = Path(profile.get("documents", {}).get("resume", "")).resolve()
        self.creds = creds_mod.load()
        self._pw = None
        self.ctx: Optional[BrowserContext] = None

    # --- lifecycle --------------------------------------------------------

    def __enter__(self) -> "Filler":
        self._pw = sync_playwright().start()
        profile_dir = Path(self.cfg["apply"]["browser_profile"]).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        self.ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.cfg["apply"].get("headless", False),
            slow_mo=self.cfg["apply"].get("slow_mo_ms", 0),
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        return self

    def __exit__(self, *exc):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # --- label extraction -------------------------------------------------

    @staticmethod
    def _label_for(page: Page, el) -> str:
        """Best available human label for a control.

        Tries, in order: aria-label, the <label for=...> text, an ancestor
        <label>, the field's own placeholder/name, then the nearest preceding
        text node. Boards vary wildly, so we take whatever we can get.
        """
        try:
            return el.evaluate(
                """(node) => {
                    const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    let v = clean(node.getAttribute('aria-label'));
                    if (v) return v;

                    const labelledby = node.getAttribute('aria-labelledby');
                    if (labelledby) {
                        const parts = labelledby.split(/\\s+/)
                            .map(id => document.getElementById(id))
                            .filter(Boolean)
                            .map(n => clean(n.innerText));
                        if (parts.join(' ').trim()) return clean(parts.join(' '));
                    }

                    if (node.id) {
                        const lab = document.querySelector(`label[for="${CSS.escape(node.id)}"]`);
                        if (lab && clean(lab.innerText)) return clean(lab.innerText);
                    }

                    const anc = node.closest('label');
                    if (anc && clean(anc.innerText)) return clean(anc.innerText);

                    // Fieldset legend (radio groups usually live in one).
                    const fs = node.closest('fieldset');
                    if (fs) {
                        const lg = fs.querySelector('legend');
                        if (lg && clean(lg.innerText)) return clean(lg.innerText);
                    }

                    // Walk up looking for a wrapper that carries a short caption.
                    let p = node.parentElement;
                    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
                        const own = clean(p.innerText);
                        if (own && own.length < 200) return own;
                    }

                    return clean(node.getAttribute('placeholder'))
                        || clean(node.getAttribute('name'))
                        || '';
                }"""
            ) or ""
        except Exception:
            return ""

    @staticmethod
    def _selector_for(el) -> str:
        try:
            return el.evaluate(
                """(n) => n.id ? '#' + n.id
                     : (n.name ? `${n.tagName.toLowerCase()}[name="${n.name}"]` : n.tagName.toLowerCase())"""
            ) or ""
        except Exception:
            return ""

    # --- navigation -------------------------------------------------------

    def _open_form(self, page: Page) -> str:
        """Click through to the actual application form if we're on a job ad.

        Located by link href and visible text rather than ARIA role: several
        boards (Stripe among them) render apply links whose computed
        accessible name doesn't match their visible label, so a role+name
        lookup silently finds nothing.
        """
        # An href pointing at an apply route is the strongest signal available.
        for selector in ("a[href*='/apply']", "a[href*='apply?']", "a[href*='application']"):
            try:
                loc = page.locator(selector)
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    if el.is_visible():
                        el.click(timeout=8000)
                        page.wait_for_load_state("networkidle", timeout=20000)
                        return f"followed apply link ({selector})"
            except Exception:
                continue

        # Otherwise match on the visible label of a link or button.
        for label in ("Apply now", "Apply for this job", "Apply for this role",
                      "Apply manually", "Start your application", "Apply"):
            for tag in ("a", "button"):
                try:
                    loc = page.locator(f"{tag}:has-text('{label}')")
                    for i in range(min(loc.count(), 5)):
                        el = loc.nth(i)
                        if el.is_visible():
                            el.click(timeout=8000)
                            page.wait_for_load_state("networkidle", timeout=20000)
                            return f"clicked {tag} {label!r}"
                except Exception:
                    continue

        return "no apply control found - assuming already on the form"

    @staticmethod
    def _needs_login(page: Page) -> bool:
        try:
            body = (page.inner_text("body", timeout=5000) or "").lower()
        except Exception:
            return False
        markers = ["sign in to continue", "create an account to apply",
                   "please sign in", "log in to apply", "returning applicant"]
        has_password = page.locator("input[type='password']").count() > 0
        return has_password and any(m in body for m in markers)

    # --- portal authentication --------------------------------------------

    def _try_auth(self, page: Page) -> str:
        """Log in - or sign up - on a portal that gates its application form.

        Returns a short status string. Portals that email a verification link
        can't be finished here; those are reported so you can complete them by
        hand once, after which the saved browser profile keeps you signed in.
        """
        if not self.creds.usable:
            return "no credentials configured"

        email_box = page.locator(
            "input[type='email'], input[name*='email' i], input[id*='email' i]"
        ).first
        pw_box = page.locator("input[type='password']").first

        if not (email_box.count() and pw_box.count()):
            return "no login form found"

        try:
            email_box.fill(self.creds.email, timeout=5000)
            pw_box.fill(self.creds.password, timeout=5000)
        except Exception as exc:
            return f"could not fill credentials: {type(exc).__name__}"

        # A "confirm password" box means this is a signup form, not a login.
        confirms = page.locator(
            "input[type='password'][name*='confirm' i], input[type='password'][id*='confirm' i]"
        )
        is_signup = confirms.count() > 0
        if is_signup:
            try:
                confirms.first.fill(self.creds.password, timeout=5000)
            except Exception:
                pass

        for pattern in [r"^\s*sign\s*in\s*$", r"^\s*log\s*in\s*$", r"^\s*continue\s*$",
                        r"^\s*create\s+account\s*$", r"^\s*sign\s*up\s*$", r"^\s*submit\s*$"]:
            try:
                btn = page.get_by_role("button", name=re.compile(pattern, re.I)).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=8000)
                    page.wait_for_load_state("networkidle", timeout=20000)
                    kind = "signup" if is_signup else "login"
                    if page.locator("input[type='password']").count():
                        return f"{kind} submitted but a password field is still showing - " \
                               f"may need email verification"
                    return f"{kind} succeeded"
            except Exception:
                continue

        return "credentials filled but no submit button matched"

    # --- frame selection ---------------------------------------------------

    @staticmethod
    def _form_frame(page: Page):
        """Return the frame that actually holds the application form.

        Career sites routinely embed the ATS form in an iframe (Stripe serves a
        Greenhouse embed), so scanning only the main frame finds nothing. We
        pick whichever frame exposes the most fillable controls.
        """
        best, best_count = page.main_frame, 0
        for frame in page.frames:
            url = (frame.url or "").lower()
            # Skip analytics, consent and captcha frames outright.
            if any(k in url for k in ("recaptcha", "googletagmanager", "privacycompl",
                                      "googleapis", "doubleclick", "hcaptcha")):
                continue
            try:
                n = frame.locator("input:not([type='hidden']), select, textarea").count()
            except Exception:
                continue
            if n > best_count:
                best, best_count = frame, n
        return best, best_count

    @staticmethod
    def _has_captcha(page: Page) -> bool:
        """A captcha means this one can't be completed unattended."""
        for frame in page.frames:
            u = (frame.url or "").lower()
            if "recaptcha" in u or "hcaptcha" in u or "turnstile" in u:
                return True
        try:
            return page.locator(".g-recaptcha, [data-sitekey], .h-captcha").count() > 0
        except Exception:
            return False

    # --- field filling ----------------------------------------------------

    def fill_form(self, page: Page, scope=None) -> tuple[list[FieldFill], list[str]]:
        scope = scope if scope is not None else page
        filled: list[FieldFill] = []
        unanswered: list[str] = []
        seen_groups: set[str] = set()

        # --- text-ish inputs and textareas --------------------------------
        for el in scope.locator("input, textarea").all():
            try:
                if not el.is_visible():
                    continue
                tag = el.evaluate("n => n.tagName.toLowerCase()")
                itype = (el.get_attribute("type") or "text").lower() if tag == "input" else "textarea"
                if itype in _SKIP_TYPES:
                    continue

                label = self._label_for(page, el)
                sel = self._selector_for(el)

                if itype == "file":
                    if self.resume.exists() and re.search(r"resume|cv|attach|upload", label, re.I):
                        el.set_input_files(str(self.resume))
                        filled.append(FieldFill(sel, label or "resume upload",
                                                self.resume.name, "file", "documents.resume", 1.0))
                    continue

                if itype in ("radio", "checkbox"):
                    group = el.get_attribute("name") or label
                    if group in seen_groups:
                        continue
                    handled = self._fill_choice_group(scope, el, group, label, filled)
                    if handled:
                        seen_groups.add(group)
                    elif label:
                        unanswered.append(label)
                    continue

                if el.input_value():          # already populated (autofill/session)
                    continue

                ff = resolve(label, self.profile)
                if ff is None:
                    if label:
                        unanswered.append(label)
                    continue
                el.fill(ff.value, timeout=5000)
                ff.selector, ff.kind = sel, itype
                filled.append(ff)
            except Exception:
                continue

        # --- native selects ------------------------------------------------
        for el in scope.locator("select").all():
            try:
                if not el.is_visible():
                    continue
                label = self._label_for(page, el)
                ff = resolve(label, self.profile)
                options = el.locator("option").all_text_contents()
                if ff is None:
                    if label:
                        unanswered.append(label)
                    continue
                pick = choose_option(ff.value, options)
                if pick is None:
                    unanswered.append(f"{label} (no option matched {ff.value!r})")
                    continue
                el.select_option(label=pick, timeout=5000)
                filled.append(FieldFill(self._selector_for(el), label, pick, "select", ff.source, 0.85))
            except Exception:
                continue

        # Boards frequently render a visible control plus a mirrored hidden one,
        # which would otherwise show the same answer twice in the review list.
        deduped, seen = [], set()
        for f in filled:
            sig = (f.label, f.value)
            if sig in seen:
                continue
            seen.add(sig)
            deduped.append(f)

        unanswered = list(dict.fromkeys(u for u in unanswered if u.strip()))
        return deduped, unanswered

    def _fill_choice_group(self, scope, el, group: str, label: str,
                           filled: list[FieldFill]) -> bool:
        """Pick the right radio/checkbox within a same-named group."""
        ff = resolve(label, self.profile)
        if ff is None:
            return False
        try:
            members = scope.locator(f"input[name='{group}']").all() if group else [el]
        except Exception:
            members = [el]

        labels = []
        for m in members:
            labels.append(self._label_for(scope, m))
        pick = choose_option(ff.value, labels)
        if pick is None:
            return False
        for m, lab in zip(members, labels):
            if lab == pick:
                try:
                    m.check(timeout=5000)
                    filled.append(FieldFill(self._selector_for(m), label, pick,
                                            "radio", ff.source, 0.85))
                    return True
                except Exception:
                    return False
        return False

    # --- submit discovery (locate, never click) ---------------------------

    SUBMIT_LABELS = ["Submit application", "Submit Application", "Send application",
                     "Submit", "Finish", "Apply"]

    @classmethod
    def _submit_locator(cls, page: Page, scope=None):
        """The real submit control, or None. Located but never clicked here."""
        if scope is None:
            scope, _ = cls._form_frame(page)
        for label in cls.SUBMIT_LABELS:
            for tag in ("button", "input[type='submit']", "a"):
                sel = (f"{tag}:has-text('{label}')" if tag != "input[type='submit']"
                       else f"input[type='submit'][value*='{label}' i]")
                try:
                    loc = scope.locator(sel)
                    for i in range(min(loc.count(), 5)):
                        el = loc.nth(i)
                        if el.is_visible() and el.is_enabled():
                            return el, label
                except Exception:
                    continue
        return None, ""

    @classmethod
    def _find_submit(cls, page: Page, scope=None) -> str:
        el, label = cls._submit_locator(page, scope)
        if el is None:
            return ""
        try:
            return f"{label}: {el.evaluate('n => n.outerHTML')[:200]}"
        except Exception:
            return label

    # --- public entry point -----------------------------------------------

    def prepare(self, job_url: str, job_key: str) -> FillResult:
        """Fill one application and park at the Submit button."""
        shots = Path(self.cfg["storage"]["screenshots"])
        shots.mkdir(parents=True, exist_ok=True)
        page = self.ctx.new_page()
        page.set_default_timeout(self.cfg["apply"].get("per_job_timeout_seconds", 180) * 1000)

        try:
            page.goto(job_url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass

            nav_note = self._open_form(page)

            if self._needs_login(page):
                auth_note = self._try_auth(page)
                if self._needs_login(page):
                    shot = str(shots / f"{job_key}_login.png")
                    page.screenshot(path=shot, full_page=True)
                    return FillResult(
                        "needs_login",
                        f"portal requires an account ({auth_note}) - finish the login in the "
                        f"open browser window; the session is saved for next time",
                        screenshot=shot, url=page.url,
                    )
                nav_note = f"{nav_note}; {auth_note}"
                self._open_form(page)

            scope, field_count = self._form_frame(page)
            if field_count == 0:
                # Embedded forms often mount a beat after networkidle.
                page.wait_for_timeout(2500)
                scope, field_count = self._form_frame(page)

            filled, unanswered = self.fill_form(page, scope)

            if not filled:
                shot = str(shots / f"{job_key}_nofields.png")
                page.screenshot(path=shot, full_page=True)
                return FillResult("failed", f"no fillable fields found ({nav_note})",
                                  unanswered=unanswered, screenshot=shot, url=page.url)

            submit = self._find_submit(page, scope)
            captcha = self._has_captcha(page)
            shot = ""
            if self.cfg["apply"].get("screenshot_before_submit", True):
                shot = str(shots / f"{job_key}_filled.png")
                page.screenshot(path=shot, full_page=True)

            return FillResult(
                "filled",
                f"{len(filled)} fields filled, {len(unanswered)} unanswered"
                + (" - CAPTCHA present, solve it in the browser before submitting" if captcha else "")
                + f" ({nav_note})",
                fields=[f.to_dict() for f in filled],
                unanswered=unanswered,
                screenshot=shot,
                submit_selector=submit,
                url=page.url,
            )
        except Exception as exc:
            return FillResult("failed", f"{type(exc).__name__}: {exc}", url=page.url)
        finally:
            # Leave the tab open in review mode so you can inspect and submit.
            if self.cfg["apply"].get("headless", False):
                page.close()
