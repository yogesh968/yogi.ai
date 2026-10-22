"""Application configuration, loaded from .env (secrets never live in code)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
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

    # --- audio: phone mode (virtual cables). Substring match, case-insensitive. ---
    caller_input_device: str = "CABLE Output"      # Phone Link speaker -> CABLE Input -> we read CABLE Output
    ai_output_device: str = "Hi-Fi Cable Input"    # we play here -> Phone Link mic = Hi-Fi Cable Output
    # --- audio: test mode (your normal mic + headphones). Empty = Windows default device. ---
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
            raise ValueError("HOST must be a loopback address (127.0.0.1); this app is local-only by design")
        return v

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
