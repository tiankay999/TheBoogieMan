"""Settings for the brain, read from the environment or brain/.env (never committed)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Keys live in .env at the repo root (copy .env.example). brain/.env still works too and wins if both exist.
load_dotenv(Path(__file__).with_name(".env"))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    anthropic_key: str | None
    groq_key: str | None
    openai_key: str | None
    openai_base: str | None
    model: str | None
    elevenlabs_key: str | None
    voice_id: str
    devil_voice_id: str | None
    tts_model: str
    stt_model: str
    mic_device: str | None
    voice_enabled: bool


def load() -> Settings:
    return Settings(
        llm_provider=os.environ.get("LLM_PROVIDER", "auto"),
        anthropic_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        # Accept the common misspelling too.
        groq_key=os.environ.get("GROQ_API_KEY") or os.environ.get("GROK_API_KEY") or None,
        openai_key=os.environ.get("LLM_API_KEY") or None,
        openai_base=os.environ.get("LLM_BASE_URL") or None,
        model=os.environ.get("LLM_MODEL") or os.environ.get("BOOGEYMAN_MODEL") or None,
        elevenlabs_key=os.environ.get("ELEVENLABS_API_KEY") or None,
        # Brian: deep, resonant premade voice. Swap for any voice id in your ElevenLabs account.
        voice_id=os.environ.get("ELEVENLABS_VOICE_ID", "nPczCjzI2devNBz1zQrb"),
        # Optional second voice used only in devil mode. Unset = the same voice, darkened.
        devil_voice_id=os.environ.get("ELEVENLABS_DEVIL_VOICE_ID") or None,
        tts_model=os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5"),
        stt_model=os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v2"),
        mic_device=os.environ.get("BOOGEYMAN_MIC") or None,
        voice_enabled=os.environ.get("BOOGEYMAN_VOICE", "1") != "0",
    )
