import asyncio

import pytest

from src import runtime_settings
from src.config import settings
from src.groq_client import GroqClient, groq_client


@pytest.fixture(autouse=True)
def clean_cache():
    runtime_settings.reset_cache()
    yield
    runtime_settings.reset_cache()


def test_defaults_are_cheap_models_and_auto_stt():
    assert settings.GROQ_MODEL == "openai/gpt-oss-20b"
    assert settings.GROQ_MODEL_FALLBACK == "openai/gpt-oss-120b"
    assert settings.GROQ_STT_MODEL == "whisper-large-v3-turbo"
    assert GroqClient._models() == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    assert GroqClient._light_models() == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    assert asyncio.run(runtime_settings.stt_provider()) == "auto"
    assert asyncio.run(runtime_settings.tts_enabled()) is True


def test_invalid_stored_value_falls_back_to_auto():
    runtime_settings._cache.update({runtime_settings.STT_PROVIDER_KEY: "bogus"})
    runtime_settings._loaded_at = 10**12  # кэш считается свежим
    assert asyncio.run(runtime_settings.stt_provider()) == "auto"


def test_stored_values_override_defaults():
    runtime_settings._cache.update({
        runtime_settings.STT_PROVIDER_KEY: "groq",
        runtime_settings.TTS_ENABLED_KEY: "false",
    })
    runtime_settings._loaded_at = 10**12
    assert asyncio.run(runtime_settings.stt_provider()) == "groq"
    assert asyncio.run(runtime_settings.tts_enabled()) is False


def test_unknown_key_is_rejected():
    with pytest.raises(KeyError):
        asyncio.run(runtime_settings.set_value("nope", "x"))


def test_tts_switched_off_in_settings_returns_no_audio(monkeypatch):
    async def off():
        return False

    monkeypatch.setattr(runtime_settings, "tts_enabled", off)
    assert asyncio.run(groq_client.text_to_speech("hello", voice="diana")) is None
