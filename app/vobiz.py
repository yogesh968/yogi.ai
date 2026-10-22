"""Minimal Vobiz REST client + VobizXML builders."""
from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape, quoteattr

import httpx

from .config import get_settings


class VobizError(RuntimeError):
    pass


def _friendly(status: int, body: str) -> str:
    low = body.lower()
    if status == 401:
        return "Vobiz rejected the credentials: check VOBIZ_AUTH_ID / VOBIZ_AUTH_TOKEN in .env."
    if status == 402 or "balance" in low:
        return "Vobiz balance is too low to place the call. Recharge in the Vobiz console."
    if "not owned" in low or "from" in low and "invalid" in low:
        return "VOBIZ_NUMBER isn't a number on your Vobiz account (use E.164, e.g. +9180…)."
    return f"Vobiz API error {status}: {body[:300]}"


async def make_call(to: str, answer_url: str, hangup_url: str, time_limit_s: int = 3600) -> str:
    """Dial `to` from your Vobiz number. Returns Vobiz's request/call UUID."""
    s = get_settings()
    url = f"{s.vobiz_api_base}/Account/{s.vobiz_auth_id}/Call/"
    payload: dict[str, Any] = {
        "from": s.vobiz_number, "to": to, "answer_url": answer_url, "answer_method": "POST",
        "hangup_url": hangup_url, "hangup_method": "POST", "time_limit": time_limit_s,
    }
    headers = {"X-Auth-ID": s.vobiz_auth_id, "X-Auth-Token": s.vobiz_auth_token}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.status_code >= 300:
        raise VobizError(_friendly(r.status_code, r.text))
    data = r.json()
    return str(data.get("request_uuid") or data.get("call_uuid") or "")


def stream_xml(ws_url: str, status_url: str, sample_rate: int = 16000) -> str:
    """Hand the call to our WebSocket. <Hangup/> runs when we send `stop` (e.g. after the goodbye)."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" audioTrack="inbound" keepCallAlive="true"
            contentType="audio/x-l16;rate={sample_rate}"
            statusCallbackUrl={quoteattr(status_url)} statusCallbackMethod="POST">{escape(ws_url)}</Stream>
    <Hangup/>
</Response>"""


def busy_xml(message: str = "") -> str:
    speak = f'<Speak voice="WOMAN" language="en-US">{escape(message)}</Speak>' if message else ""
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>{speak}<Hangup reason="busy"/></Response>'
