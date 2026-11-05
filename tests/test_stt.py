import asyncio
import subprocess

import pytest

from src import metrics, runtime_settings, stt
from src.groq_client import groq_client


@pytest.fixture
def fake(monkeypatch):
    """Подмена провайдеров и режима; calls фиксирует, кого позвали."""
    state = {"mode": "auto", "google": "google text", "whisper": "whisper text", "calls": []}

    async def google(audio):
        state["calls"].append("google")
        if isinstance(state["google"], Exception):
            raise state["google"]
        return state["google"]

    async def whisper(audio):
        state["calls"].append("whisper")
        return state["whisper"]

    async def mode():
        return state["mode"]

    monkeypatch.setattr(stt, "transcribe_google", google)
    monkeypatch.setattr(groq_client, "transcribe_audio", whisper)
    monkeypatch.setattr(runtime_settings, "stt_provider", mode)
    monkeypatch.setattr(metrics, "record", lambda *a, **k: None)
    return state


def run(duration=10):
    return asyncio.run(stt.transcribe(b"audio", duration=duration))


def test_auto_uses_google_first_and_skips_whisper(fake):
    assert run() == "google text"
    assert fake["calls"] == ["google"]


def test_auto_falls_back_to_whisper_when_google_fails(fake):
    fake["google"] = stt.GoogleSttError("blocked")
    assert run() == "whisper text"
    assert fake["calls"] == ["google", "whisper"]


def test_auto_falls_back_on_unexpected_google_errors_too(fake):
    fake["google"] = RuntimeError("boom")
    assert run() == "whisper text"


def test_auto_returns_none_when_both_fail(fake):
    fake["google"] = stt.GoogleSttError("blocked")
    fake["whisper"] = None
    assert run() is None
    assert fake["calls"] == ["google", "whisper"]


def test_google_only_has_no_fallback(fake):
    fake["mode"] = "google"
    fake["google"] = stt.GoogleSttError("blocked")
    assert run() is None
    assert fake["calls"] == ["google"]


def test_whisper_only_never_calls_google(fake):
    fake["mode"] = "groq"
    assert run() == "whisper text"
    assert fake["calls"] == ["whisper"]


@pytest.mark.parametrize("mode", ["auto", "google"])
def test_long_recordings_go_straight_to_whisper(fake, mode):
    fake["mode"] = mode
    assert run(duration=120) == "whisper text"
    assert fake["calls"] == ["whisper"]


def test_unknown_duration_is_treated_as_short(fake):
    assert asyncio.run(stt.transcribe(b"audio", duration=None)) == "google text"


# ─── ffmpeg и обёртка над библиотекой Google ────────────────────────────────

def _opus_bytes(tmp_path, seconds=2):
    import imageio_ffmpeg

    out = tmp_path / "t.ogg"
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "libopus", str(out)],
        check=True,
    )
    return out.read_bytes()


def test_ogg_is_converted_to_16khz_mono_pcm(tmp_path):
    pcm = asyncio.run(stt.ogg_to_pcm(_opus_bytes(tmp_path, seconds=2)))
    assert 2 * 16000 * 2 * 0.95 <= len(pcm) <= 2 * 16000 * 2 * 1.1  # 2 с * 16 кГц * 2 байта


def test_garbage_audio_raises_google_stt_error():
    with pytest.raises(stt.GoogleSttError):
        asyncio.run(stt.ogg_to_pcm(b"this is not audio"))


def test_library_errors_become_google_stt_error(monkeypatch):
    import speech_recognition as sr

    def raising(error):
        def recognize(self, audio, language=None):
            raise error
        return recognize

    for error in (sr.UnknownValueError(), sr.RequestError("blocked")):
        monkeypatch.setattr(sr.Recognizer, "recognize_google", raising(error))
        with pytest.raises(stt.GoogleSttError):
            stt._recognize_google_sync(b"\x00\x00" * 1600)


def test_empty_google_result_is_a_failure(monkeypatch):
    async def fake_pcm(audio):
        return b"\x00\x00" * 1600

    monkeypatch.setattr(stt, "ogg_to_pcm", fake_pcm)
    monkeypatch.setattr(stt, "_recognize_google_sync", lambda pcm: "   ")
    with pytest.raises(stt.GoogleSttError):
        asyncio.run(stt.transcribe_google(b"audio"))
