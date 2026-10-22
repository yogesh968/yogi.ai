"""Application configuration, loaded from .env (secrets never live in code)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # --- secrets ---
    gemini_api_key: str = Field(..., min_length=10)
    database_url: str = Field(..., min_length=10)

    # --- owner ---
    owner_name: str = "Owner"
    caller_languages: str = "English, Hindi, Hinglish"
    timezone: str = "Asia/Kolkata"

    # --- models (verified against ai.google.dev docs, Oct 2026) ---
    live_model: str = "gemini-3.8-live"
    postcall_model: str = "gemini-3.8-flash"

    # --- phone calls via Vobiz (cloud telephony; calls forwarded from your mobile) ---
    owner_phone: str = ""                 # your mobile, E.164 (+91...): used to recognise forwarded calls
    vobiz_auth_id: str = ""
    vobiz_auth_token: str = ""
    vobiz_number: str = ""                # the Vobiz number you rented, E.164
    vobiz_webhook_secret: str = ""        # random string; part of the webhook URLs Vobiz calls
    vobiz_api_base: str = "https://api.vobiz.ai/api/v1"
    max_concurrent_calls: int = Field(2, ge=1, le=10)

    # --- public deployment (empty = local only) ---
    public_base_url: str = ""             # e.g. https://yogi-assistant.duckdns.org
    dashboard_password: str = ""
    secret_key: str = ""                  # random, signs login cookies

    # --- local test mode (your laptop mic + headphones). Empty = system default device. ---
    test_input_device: str = ""
    test_output_device: str = ""
    # Preferred Windows host API. WASAPI has the lowest latency; MME truncates names to 31 chars.
    audio_hostapi: str = "Windows WASAPI"
    chunk_ms: int = Field(20, ge=10, le=100)
    silence_rms_threshold: int = Field(350, ge=10, le=10000)
    # Server-side voice activity detection. Start "low" resists line noise/echo falsely interrupting the AI;
    # end "high" + short silence = snappier replies.
    vad_start_sensitivity: Literal["high", "low", "default"] = "low"
    vad_end_sensitivity: Literal["high", "low", "default"] = "high"
    vad_silence_ms: int = Field(500, ge=100, le=3000)

    # --- defaults for settings that are editable in the dashboard ---
    default_voice: str = "Puck"
    default_recording_enabled: bool = True
    default_silence_timeout_s: int = Field(45, ge=5, le=3600)

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8765
    recordings_dir: Path = ROOT / "data" / "recordings"
    record_sample_rate: int = 16000

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        if v not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("HOST must be a loopback address (127.0.0.1); in the cloud, Caddy serves the public side")
        return v

    @property
    def public_host(self) -> str:
        return urlparse(self.public_base_url).hostname or ""

    @property
    def vobiz_enabled(self) -> bool:
        return bool(self.vobiz_auth_id and self.vobiz_auth_token and self.vobiz_number and self.public_base_url)

    @model_validator(mode="after")
    def _public_needs_auth(self) -> "Settings":
        if self.public_base_url:
            if not self.public_base_url.startswith("https://"):
                raise ValueError("PUBLIC_BASE_URL must start with https://")
            if len(self.dashboard_password) < 10:
                raise ValueError("DASHBOARD_PASSWORD (min 10 chars) is required when PUBLIC_BASE_URL is set")
            if len(self.secret_key) < 32:
                raise ValueError("SECRET_KEY (min 32 chars) is required when PUBLIC_BASE_URL is set")
            if self.vobiz_auth_id and len(self.vobiz_webhook_secret) < 16:
                raise ValueError("VOBIZ_WEBHOOK_SECRET (min 16 chars) is required for Vobiz")
        return self

    @field_validator("recordings_dir")
    @classmethod
    def _abs_dir(cls, v: Path) -> Path:
        p = v if v.is_absolute() else ROOT / v
        p.mkdir(parents=True, exist_ok=True)
        return p.resolve()


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as e:  # friendlier than a raw pydantic dump
        raise SystemExit(
            f"Configuration error: {e}\n\nCopy .env.example to .env and fill in GEMINI_API_KEY and DATABASE_URL."
        ) from None


GEMINI_VOICES: dict[str, str] = {
    "Zephyr": "Bright", "Puck": "Upbeat", "Charon": "Informative", "Kore": "Firm", "Fenrir": "Excitable",
    "Leda": "Youthful", "Orus": "Firm", "Aoede": "Breezy", "Callirrhoe": "Easy-going", "Autonoe": "Bright",
    "Enceladus": "Breathy", "Iapetus": "Clear", "Umbriel": "Easy-going", "Algieba": "Smooth",
    "Despina": "Smooth", "Erinome": "Clear", "Algenib": "Gravelly", "Rasalgethi": "Informative",
    "Laomedeia": "Upbeat", "Achernar": "Soft", "Alnilam": "Firm", "Schedar": "Even", "Gacrux": "Mature",
    "Pulcherrima": "Forward", "Achird": "Friendly", "Zubenelgenubi": "Casual", "Vindemiatrix": "Gentle",
    "Sadachbia": "Lively", "Sadaltager": "Knowledgeable", "Sulafat": "Warm",
}
