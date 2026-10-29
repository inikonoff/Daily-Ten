import random
import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from openai import AsyncOpenAI

from src import metrics
from src.config import (
    CONTEXT_MESSAGES,
    MIN_SUMMARY_VOICE_WORDS,
    REPLY_WORD_LIMIT,
    REPLY_WORD_TOLERANCE,
    settings,
)
from src.prompt import (
    OPENING_USER_MESSAGE,
    SUMMARY_PROMPT,
    SUMMARY_PROMPT_LITE,
    build_system_prompt,
)
from src.session import EARLY, OPENING, shorten_reply, word_count

logger = logging.getLogger(__name__)

RAW_LOG_CHARS = 500  # сколько символов сырого ответа писать в лог при провале разбора

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def strip_think(text: Optional[str]) -> Optional[str]:
    """
    gpt-oss иногда протекает reasoning прямо в message.content (<think>...</think>
    или без закрывающего тега), съедая весь max_tokens. Проверено на speechflow.
    """
    if not text:
        return text
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"^.*?</think>", "", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def parse_json_robust(text: Optional[str]) -> Dict[str, Any]:
    """
    Достаёт JSON-объект из ответа модели: чистый JSON, JSON в ```-блоке или
    объект, окружённый лишним текстом. ValueError, если объекта нет.
    """
    cleaned = _FENCE.sub("", (strip_think(text) or "").strip()).strip()
    if not cleaned:
        raise ValueError("Empty response after stripping reasoning")
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError("No JSON object found in response")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("JSON is not an object")
    return data


class GroqClient:
    def __init__(self, api_keys: List[str]):
        self.clients = [
            AsyncOpenAI(
                api_key=key.strip(),
                base_url="https://api.groq.com/openai/v1",
                timeout=60.0,
            )
            for key in api_keys
            if key.strip()
        ]
        self.current_index = 0
        self._lock = asyncio.Lock()
        logger.info(f"✅ Инициализировано {len(self.clients)} Groq клиентов")

    # ─── Think-leak guard ────────────────────────────────────────────────────

    @staticmethod
    def _strip_think(text: Optional[str]) -> Optional[str]:
        return strip_think(text)

    def _extract_text(self, response, label: str = "response") -> str:
        raw = response.choices[0].message.content
        text = self._strip_think(raw)
        text = text.strip() if text else text
        if not text:
            logger.warning(f"⚠️ {label}: empty after stripping reasoning, raw={(raw or '')[:RAW_LOG_CHARS]!r}")
            raise ValueError(f"Empty response after stripping reasoning ({label})")
        return text

    # ─── Модели ──────────────────────────────────────────────────────────────

    @staticmethod
    def _models() -> List[str]:
        """Основная модель, затем запасная (если задана и отличается)."""
        models = [settings.GROQ_MODEL]
        fallback = settings.GROQ_MODEL_FALLBACK.strip()
        if fallback and fallback != settings.GROQ_MODEL:
            models.append(fallback)
        return models

    @staticmethod
    def _light_models() -> List[str]:
        """Для простых задач (перевод): сначала лёгкая модель, тяжёлая как запасная."""
        fallback = settings.GROQ_MODEL_FALLBACK.strip()
        if fallback and fallback != settings.GROQ_MODEL:
            return [fallback, settings.GROQ_MODEL]
        return [settings.GROQ_MODEL]

    # ─── Multi-key rotation + запасная модель ────────────────────────────────

    async def _get_next_client(self) -> Optional[AsyncOpenAI]:
        if not self.clients:
            return None
        async with self._lock:
            client = self.clients[self.current_index]
            self.current_index = (self.current_index + 1) % len(self.clients)
            return client

    async def _make_request(
        self,
        func: Callable[[AsyncOpenAI, Optional[str]], Awaitable[Any]],
        models: Sequence[Optional[str]] = (None,),
        max_attempts: Optional[int] = None,
        label: str = "groq",
    ):
        """
        Вызывает func(client, model): ротация ключей, при исчерпании попыток на
        основной модели переход на запасную. `max_attempts` ограничивает общее
        число вызовов (чтобы повторы не раздували нагрузку на лимиты).
        """
        if not self.clients:
            raise Exception("Нет доступных Groq клиентов")

        errors = []
        total = 0
        for index, model in enumerate(models):
            attempts = len(self.clients) * 2 if index == 0 else len(self.clients)
            for _ in range(attempts):
                if max_attempts is not None and total >= max_attempts:
                    break
                client = await self._get_next_client()
                if not client:
                    break
                total += 1
                try:
                    result = await func(client, model)
                except Exception as e:
                    errors.append(f"{model}: {e}")
                    logger.warning(f"❌ Groq request failed ({label}, model={model}, attempt {total}): {e}")
                    metrics.record(label, "error", model=model, detail=f"{type(e).__name__}: {e}")
                    await asyncio.sleep(0.5 + random.random())
                    continue
                if index > 0:
                    metrics.record(label, "fallback_used", model=model)
                return result

        raise Exception(f"Все Groq клиенты недоступны: {'; '.join(errors[:3])}")

    async def _chat_json(
        self,
        client: AsyncOpenAI,
        model: Optional[str],
        messages: List[Dict[str, str]],
        *,
        max_tokens: int,
        label: str,
        temps: Sequence[float],
        validate: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Один JSON-вызов с мягким разбором. При провале разбора или проверки
        повторяет тот же запрос со следующей температурой из `temps` (обычно
        0) и пометкой, что прошлый ответ отклонён. В лог WARNING уходит модель,
        размер промпта и сырой ответ (обрезанный).
        """
        last_error: Optional[ValueError] = None
        prompt_chars = sum(len(m["content"]) for m in messages)
        for index, temperature in enumerate(temps):
            msgs = messages if index == 0 else self._with_correction(messages, last_error)
            response = await client.chat.completions.create(
                model=model,
                extra_body={"include_reasoning": False},
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            try:
                data = parse_json_robust(raw)
                return validate(data) if validate else data
            except ValueError as e:
                last_error = e
                metrics.record(label, "bad_json", model=model, detail=str(e),
                               prompt_chars=prompt_chars, raw=raw)
                logger.warning(
                    f"⚠️ {label}: model={model} temp={temperature} prompt_chars={prompt_chars} "
                    f"error={e} raw={(raw or '')[:RAW_LOG_CHARS]!r}"
                )
        raise last_error  # type: ignore[misc]

    @staticmethod
    def _with_correction(messages: List[Dict[str, str]], error: Optional[Exception]) -> List[Dict[str, str]]:
        fixed = list(messages)
        fixed[0] = {
            "role": "system",
            "content": messages[0]["content"]
            + f"\n\n# CORRECTION\nYour previous output was rejected: {error}. "
            "Return ONLY one valid JSON object in exactly the required format.",
        }
        return fixed

    # ─── Единый ответ: разговор + молчаливый лог ошибки в одном вызове ──────

    async def generate_reply(
        self,
        text: Optional[str],
        *,
        name: str,
        level: str,
        history: List[Dict[str, str]],
        mode: str,
        reply_no: int,
        force_answer: bool = False,
        teacher_summaries: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Возвращает {"category", "mistake", "corrected", "counts_as_answer", "reply"}
        или None, если все попытки провалились (вызывающий код не меняет
        состояние сессии и просит юзера повторить). `text=None` — открытие
        сессии (режим OPENING). `history` — переписка текущей сессии без
        `text`. См. PERSONA_PROMPT.md.
        """
        system_prompt = build_system_prompt(
            name=name,
            level=level,
            mode=mode,
            reply_no=reply_no,
            force_answer=force_answer,
            teacher_summaries=teacher_summaries,
        )

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-CONTEXT_MESSAGES:])
        messages.append({"role": "user", "content": text if text is not None else OPENING_USER_MESSAGE})

        def _validate(data: Dict[str, Any]) -> Dict[str, Any]:
            reply = data.get("reply")
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("Missing 'reply' field")
            return data

        async def _chat(client, model):
            return await self._chat_json(
                client, model, messages,
                max_tokens=500, label="generate_reply", temps=(0.7, 0.0), validate=_validate,
            )

        try:
            result = await self._make_request(
                _chat, models=self._models(), max_attempts=4, label="generate_reply"
            )
        except Exception as e:
            logger.error(f"❌ Ошибка generate_reply: {e}")
            return None

        reply = result["reply"].strip()
        if word_count(reply) > REPLY_WORD_LIMIT * REPLY_WORD_TOLERANCE:
            shortened = shorten_reply(reply, REPLY_WORD_LIMIT)
            logger.warning(
                f"⚠️ generate_reply: reply too long ({word_count(reply)} words), "
                f"shortened to {word_count(shortened)}"
            )
            metrics.record("generate_reply", "reply_shortened",
                           detail=f"{word_count(reply)} -> {word_count(shortened)} words")
            reply = shortened
        result["reply"] = reply
        result["counts_as_answer"] = (
            True if mode in (OPENING, EARLY) else bool(result.get("counts_as_answer", True))
        )
        return result

    # ─── Session Summary (SS) + Summary for Teacher (SS4T), один вызов ─────

    @staticmethod
    def _validate_summary(data: Dict[str, Any], require_voice: bool = False) -> Dict[str, Any]:
        """
        Жёстко проверяем только структуру. Короткий voice_script не провал:
        остальные поля пригодны для карточки (см. generate_session_summary).
        """
        user = data.get("user_summary")
        teacher = data.get("teacher_summary")
        if not isinstance(user, dict) or not isinstance(teacher, dict):
            raise ValueError("Summary: missing user_summary / teacher_summary")
        voice = user.get("voice_script")
        user["voice_script"] = voice.strip() if isinstance(voice, str) else ""
        if not isinstance(user.get("mistakes"), list):
            user["mistakes"] = []
        if not isinstance(user.get("focus"), list):
            user["focus"] = []
        if not isinstance(teacher.get("errors"), list):
            teacher["errors"] = []
        if not (user["voice_script"] or user.get("best_moment") or user["mistakes"] or user["focus"]):
            raise ValueError("Summary: user_summary is empty")
        if require_voice and word_count(user["voice_script"]) < MIN_SUMMARY_VOICE_WORDS:
            raise ValueError("Summary: voice_script suspiciously short")
        return data

    async def generate_session_summary(
        self,
        name: str,
        level: str,
        dialog: List[Dict[str, str]],
        errors: List[Dict[str, str]],
        avg_words: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Возвращает {"user_summary": {...}, "teacher_summary": {...}} или None
        после всех повторов. Порядок: полный промпт, затем упрощённый; если
        голосовой текст слишком короткий, одна дополнительная попытка, а после
        неё SS уходит без голосового (только карточка).
        """
        transcript = "\n".join(
            f'{"Mrs. Smith" if m["role"] == "assistant" else "Student"}: {m["content"]}'
            for m in dialog
        )
        noted = "\n".join(
            f'- {e["category"]}: "{e["mistake_text"]}" -> "{e["corrected_text"]}"' for e in errors
        ) or "(none noted)"
        user_content = (
            f"Student name: {name or 'unknown'}\n"
            f"Self-described level: {level or 'unknown'}\n"
            f"Average answer length: {avg_words} words\n\n"
            f"Mistakes noted silently during the conversation:\n{noted}\n\n"
            f"Conversation:\n{transcript}"
        )

        async def _run(prompt: str, temps: Sequence[float], max_attempts: int, require_voice: bool = False,
                       models: Optional[Sequence[str]] = None):
            def _validate(data):
                return self._validate_summary(data, require_voice=require_voice)

            async def _summary(client, model):
                return await self._chat_json(
                    client, model,
                    [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}],
                    max_tokens=2500, label="summary", temps=temps, validate=_validate,
                )

            return await self._make_request(
                _summary, models=models or self._models(), max_attempts=max_attempts, label="summary"
            )

        result = None
        for prompt, label, attempts in ((SUMMARY_PROMPT, "full", 3), (SUMMARY_PROMPT_LITE, "lite", 2)):
            try:
                result = await _run(prompt, (0.5, 0.0), attempts)
                if label == "lite":
                    metrics.record("summary", "lite_prompt")
                break
            except Exception as e:
                logger.error(f"❌ Ошибка generate_session_summary ({label} prompt): {e}")
        if not result:
            return None

        voice = result["user_summary"]["voice_script"]
        if word_count(voice) < MIN_SUMMARY_VOICE_WORDS:
            logger.warning(f"⚠️ SUMMARY_SHORT_VOICE: voice_script has {word_count(voice)} words")
            metrics.record("summary", "short_voice", detail=f"{word_count(voice)} words")
            try:
                result = await _run(
                    SUMMARY_PROMPT, (0.0,), 1, require_voice=True, models=self._models()[:1]
                )
            except Exception as e:
                logger.warning(f"⚠️ SUMMARY_SHORT_VOICE retry failed, sending card only: {e}")
                metrics.record("summary", "voice_dropped")
                result["user_summary"]["voice_script"] = ""
        return result

    # ─── Перевод (кнопка Translate) ──────────────────────────────────────────

    async def translate_text(self, text: str) -> str:
        async def _translate(client, model):
            response = await client.chat.completions.create(
                model=model,
                extra_body={"include_reasoning": False},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a translator. Translate the given English text to Russian.\n"
                            "Translate idioms by meaning, not literally. Preserve the tone. "
                            "Return only the translation, no comments."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                temperature=0.2,
                max_tokens=400,
            )
            return self._extract_text(response, "translate_text")

        try:
            return await self._make_request(_translate, models=self._light_models(), label="translate")
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return "Translation failed."

    # ─── Транскрибация голосовых юзера ──────────────────────────────────────

    async def transcribe_audio(self, audio_bytes: bytes) -> Optional[str]:
        async def _transcribe(client, model):
            response = await client.audio.transcriptions.create(
                model=settings.GROQ_STT_MODEL,
                file=("voice.ogg", audio_bytes, "audio/ogg"),
                language="en",
                response_format="text",
                temperature=0.0,
            )
            return response

        try:
            result = await self._make_request(_transcribe, label="stt")
            text = result if isinstance(result, str) else getattr(result, "text", str(result))
            return text.strip()
        except Exception as e:
            logger.error(f"❌ Ошибка транскрибации: {e}")
            return None

    # ─── TTS ─────────────────────────────────────────────────────────────────

    async def text_to_speech(self, text: str, voice: str) -> Optional[bytes]:
        if not settings.ENABLE_TTS:
            return None

        async def _tts(client, model):
            response = await client.audio.speech.create(
                model=settings.GROQ_TTS_MODEL,
                voice=voice,
                input=text,
                response_format="wav",
            )
            if hasattr(response, "content"):
                return response.content
            elif hasattr(response, "read"):
                return await response.read()
            return bytes(response)

        try:
            audio = await self._make_request(_tts, label="tts")
        except Exception as e:
            logger.error(f"❌ Ошибка TTS: {e}")
            metrics.record("tts", "tts_fail", model=settings.GROQ_TTS_MODEL, detail=str(e))
            return None
        metrics.record("tts", "tts_ok", model=settings.GROQ_TTS_MODEL)
        return audio


groq_client = GroqClient(settings.groq_api_keys_list)
