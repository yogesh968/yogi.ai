"""Phone number normalisation (India-first) to E.164."""
from __future__ import annotations

import re

_DIGITS = re.compile(r"\D")


def to_e164(raw: str, default_cc: str = "91") -> str:
    """'+91 98765-43210', '09876543210', '919876543210', '9876543210' -> '+919876543210'. '' if not a phone number."""
    if not raw:
        return ""
    raw = raw.strip()
    if raw.lower().startswith("sip:"):
        return ""
    plus = raw.startswith("+")
    d = _DIGITS.sub("", raw)
    if raw.startswith("00"):
        d, plus = d[2:], True
    if not plus:
        if len(d) == 11 and d.startswith("0"):
            d = default_cc + d[1:]
        elif len(d) == 10:
            d = default_cc + d
    return f"+{d}" if 6 <= len(d) <= 15 else ""


def same_number(a: str, b: str) -> bool:
    ea, eb = to_e164(a), to_e164(b)
    return bool(ea) and ea == eb
