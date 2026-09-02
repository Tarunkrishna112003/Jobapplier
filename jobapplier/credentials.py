"""Portal signup/login credentials.

Loaded from secrets.json, which is gitignored. Nothing here is ever written
back to disk or included in the review payload shown by the UI - the password
must not end up in a screenshot caption, a log line, or the tracker DB.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets.json"


class Credentials:
    def __init__(self, email: str = "", password: str = ""):
        self.email = email
        self.password = password

    @property
    def usable(self) -> bool:
        return bool(self.email and self.password)

    def __repr__(self) -> str:      # never leak the password in a traceback
        return f"Credentials(email={self.email!r}, password=***)"


def load(path: Path | str = SECRETS) -> Credentials:
    p = Path(path)
    if not p.exists():
        return Credentials()
    try:
        data = json.loads(p.read_text()).get("signup", {})
    except Exception:
        return Credentials()
    return Credentials(data.get("email", ""), data.get("password", ""))


def redact(text: str, creds: Credentials) -> str:
    """Strip the password out of anything headed for a log or the UI."""
    if creds.password and creds.password in text:
        text = text.replace(creds.password, "***")
    return text
