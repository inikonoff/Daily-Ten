"""
Распознавание голосовых юзера. Провайдеры:

- Google: бесплатное веб-API через библиотеку SpeechRecognition. Неофициальное
  (без SLA), принимает записи до ~минуты. Telegram присылает OGG/Opus, поэтому
  сначала ffmpeg (бинарник из пакета imageio-ffmpeg) переводит запись в PCM 16 кГц.
- Whisper на Groq (groq_client.transcribe_audio).

Режим выбирается в админке (runtime_settings.stt_provider):
auto   — сначала Google, при ошибке или пустом результате Whisper;
google — только Google, без запасного пути (кроме длинных записей);
groq   — только Whisper.
Записи длиннее GOOGLE_STT_MAX_SECONDS Google не принимает, их всегда
распознаёт Whisper.
"""

import asyncio
import logging
from typing import Optional

from src import metrics, runtime_settings
from src.config import FFMPEG_TIMEOUT_SECONDS, GOOGLE_STT_MAX_SECONDS, GOOGLE_STT_TIMEOUT_SECONDS
from src.groq_client import groq_client

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # байта (s16le)


class GoogleSttError(Exception):
    """Google не смог распознать: сеть, лимит, блокировка или речь не разобрана."""


async def ogg_to_pcm(audio_bytes: bytes) -> bytes:
    """OGG/Opus → сырой PCM s16le, 16 кГц, моно."""
    import imageio_ffmpeg

    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-loglevel", "error", "-i", "pipe:0",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(audio_bytes), FFMPEG_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        raise GoogleSttError("ffmpeg timeout")
    if process.returncode != 0 or not out:
        raise GoogleSttError(f"ffmpeg failed: {err.decode(errors='replace')[:200]}")
    return out


def _recognize_google_sync(pcm: bytes) -> str:
    import speech_recognition as sr

    recognizer = sr.Recognizer()
    recognizer.operation_timeout = GOOGLE_STT_TIMEOUT_SECONDS
    try:
        return recognizer.recognize_google(sr.AudioData(pcm, SAMPLE_RATE, SAMPLE_WIDTH), language="en-US")
    except sr.UnknownValueError:
        raise GoogleSttError("speech not understood")
    except sr.RequestError as e:
        raise GoogleSttError(f"request failed: {e}")


async def transcribe_google(audio_bytes: bytes) -> str:
    pcm = await ogg_to_pcm(audio_bytes)
    text = await asyncio.to_thread(_recognize_google_sync, pcm)
    text = (text or "").strip()
    if not text:
        raise GoogleSttError("empty result")
    return text


async def _try_google(audio_bytes: bytes) -> Optional[str]:
    try:
        text = await transcribe_google(audio_bytes)
    except Exception as e:  # любой сбой Google = неудача провайдера, не ошибка бота
        logger.warning(f"⚠️ Google STT failed: {e}")
        metrics.record("stt", "stt_fail", model="google", detail=str(e))
        return None
    metrics.record("stt", "stt_ok", model="google")
    return text


async def _try_whisper(audio_bytes: bytes) -> Optional[str]:
    text = await groq_client.transcribe_audio(audio_bytes)
    if text:
        metrics.record("stt", "stt_ok", model="whisper")
        return text
    metrics.record("stt", "stt_fail", model="whisper")
    return None


async def transcribe(audio_bytes: bytes, duration: Optional[int] = None) -> Optional[str]:
    """Текст голосового или None, если ни один из положенных провайдеров не справился."""
    mode = await runtime_settings.stt_provider()
    too_long = duration is not None and duration > GOOGLE_STT_MAX_SECONDS

    if mode == runtime_settings.STT_GROQ or too_long:
        return await _try_whisper(audio_bytes)

    text = await _try_google(audio_bytes)
    if text or mode == runtime_settings.STT_GOOGLE:
        return text
    return await _try_whisper(audio_bytes)  # auto: запасной путь
