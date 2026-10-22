import random
import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from src.config import CONTEXT_MESSAGES, settings
from src.prompt import OPENING_USER_MESSAGE, SUMMARY_PROMPT, build_system_prompt
from src.session import EARLY, OPENING

logger = logging.getLogger(__name__)

MODEL = "openai/gpt-oss-120b"

# Минимум слов в voice_script SS: короче — почти наверняка обрыв/мусор (известный баг Groq).
MIN_SUMMARY_VOICE_WORDS = 40


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
    # gpt-oss-120b иногда протекает reasoning прямо в message.content
    # (<think>...</think> или без закрывающего тега), съедая весь max_tokens
    # и оставляя пустой/обрезанный ответ. Проверено на speechflow — закладываем
    # защиту с первого дня, а не после жалоб пользователей.

    @staticmethod
    def _strip_think(text: Optional[str]) -> Optional[str]:
        if not text:
            return text
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"^.*?</think>", "", cleaned, flags=re.DOTALL)
        return cleaned.strip()

    def _extract_text(self, response, label: str = "response") -> str:
        text = self._strip_think(response.choices[0].message.content)
        text = text.strip() if text else text
        if not text:
            raise ValueError(f"Empty response after stripping reasoning ({label})")
        return text

    def _extract_json(self, response, label: str = "response") -> Dict[str, Any]:
        text = self._extract_text(response, label)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {label}: {e}") from e

    # ─── Multi-key rotation ──────────────────────────────────────────────────

    async def _get_next_client(self) -> Optional[AsyncOpenAI]:
        if not self.clients:
            return None
        async with self._lock:
            client = self.clients[self.current_index]
            self.current_index = (self.current_index + 1) % len(self.clients)
            return client

    async def _make_request(self, func, *args, **kwargs):
        if not self.clients:
            raise Exception("Нет доступных Groq клиентов")

        errors = []
        for attempt in range(len(self.clients) * 2):
            client = await self._get_next_client()
            if not client:
                break
            try:
                return await func(client, *args, **kwargs)
            except Exception as e:
                errors.append(str(e))
                logger.warning(f"❌ Groq request failed (attempt {attempt + 1}): {e}")
                await asyncio.sleep(0.5 + random.random())

        raise Exception(f"Все Groq клиенты недоступны: {'; '.join(errors[:3])}")

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

        async def _chat(client):
            response = await client.chat.completions.create(
                model=MODEL,
                extra_body={"include_reasoning": False},
                messages=messages,
                temperature=0.7,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            return self._extract_json(response, "generate_reply")

        try:
            result = await self._make_request(_chat)
            reply = result.get("reply")
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("Missing 'reply' field")
            result["reply"] = reply.strip()
            result["counts_as_answer"] = (
                True if mode in (OPENING, EARLY) else bool(result.get("counts_as_answer", True))
            )
            return result
        except Exception as e:
            logger.error(f"❌ Ошибка generate_reply: {e}")
            return None

    # ─── Session Summary (SS) + Summary for Teacher (SS4T), один вызов ─────

    @staticmethod
    def _validate_summary(data: Dict[str, Any]) -> Dict[str, Any]:
        """Структура и защита от обрыва: короткий voice_script = провал, вызов повторится."""
        user = data.get("user_summary")
        teacher = data.get("teacher_summary")
        if not isinstance(user, dict) or not isinstance(teacher, dict):
            raise ValueError("Summary: missing user_summary / teacher_summary")
        voice = user.get("voice_script")
        if not isinstance(voice, str) or len(voice.split()) < MIN_SUMMARY_VOICE_WORDS:
            raise ValueError("Summary: voice_script missing or suspiciously short")
        if not isinstance(user.get("mistakes"), list):
            user["mistakes"] = []
        if not isinstance(teacher.get("errors"), list):
            teacher["errors"] = []
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
        после всех повторов. Диалог целиком (реплики МС + ответы юзера).
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

        async def _summary(client):
            response = await client.chat.completions.create(
                model=MODEL,
                extra_body={"include_reasoning": False},
                messages=[
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.5,
                max_tokens=2500,
                response_format={"type": "json_object"},
            )
            return self._validate_summary(self._extract_json(response, "generate_session_summary"))

        try:
            return await self._make_request(_summary)
        except Exception as e:
            logger.error(f"❌ Ошибка generate_session_summary: {e}")
            return None

    # ─── Перевод (кнопка Translate) ──────────────────────────────────────────

    async def translate_text(self, text: str) -> str:
        async def _translate(client):
            response = await client.chat.completions.create(
                model=MODEL,
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
            return await self._make_request(_translate)
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return "Translation failed."

    # ─── Транскрибация голосовых юзера ──────────────────────────────────────

    async def transcribe_audio(self, audio_bytes: bytes) -> Optional[str]:
        async def _transcribe(client):
            response = await client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=("voice.ogg", audio_bytes, "audio/ogg"),
                language="en",
                response_format="text",
                temperature=0.0,
            )
            return response

        try:
            result = await self._make_request(_transcribe)
            text = result if isinstance(result, str) else getattr(result, "text", str(result))
            return text.strip()
        except Exception as e:
            logger.error(f"❌ Ошибка транскрибации: {e}")
            return None

    # ─── TTS ─────────────────────────────────────────────────────────────────

    async def text_to_speech(self, text: str, voice: str) -> Optional[bytes]:
        async def _tts(client):
            response = await client.audio.speech.create(
                model="canopylabs/orpheus-v1-english",
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
            return await self._make_request(_tts)
        except Exception as e:
            logger.error(f"❌ Ошибка TTS: {e}")
            return None


groq_client = GroqClient(settings.groq_api_keys_list)
