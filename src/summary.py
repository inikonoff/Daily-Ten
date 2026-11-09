"""
Session Summary (SS) и SS4T: генерация (один LLM-вызов, результат хранится в
БД) и доставка юзеру. Общая для handlers.py (после закрывающей реплики,
/summary) и background.py (таймаут), поэтому вынесена отдельно.

`user_summary` в БД — JSON-строка: из неё на лету собираются голосовое и
текстовая карточка. Домашки нет; блок можно добавить новым полем.
"""

import html
import json
import logging
from typing import Any, Dict, Optional, Set

from aiogram import Bot
from aiogram.types import BufferedInputFile

from src.config import DISPLAY_NAME, VOICE
from src.db import db
from src.groq_client import groq_client
from src.keyboards import get_voice_keyboard

logger = logging.getLogger(__name__)

# Юзеры, у которых прямо сейчас идёт генерация/отправка SS. Пока юзер здесь,
# его сообщения не считаются ответами («One moment, I'm gathering my notes»).
# Процесс один (polling), поэтому достаточно памяти процесса.
generating: Set[int] = set()

CARD_LIMIT = 4096  # лимит Telegram на текстовое сообщение


def needs_summary(session: Dict[str, Any]) -> bool:
    """Завершённая сессия, которой положена SS, но она ещё не сгенерирована."""
    if session["status"] != "completed" or session["user_summary"]:
        return False
    return session["completion_reason"] in ("finished", "early") or session["pairs_done"] >= 3


# Сколько пунктов каждой секции показывает карточка в чате. Полные списки
# остаются в user_summary (для подробного анализа, например в Mini App).
CARD_MISTAKES = 3
CARD_UPGRADES = 3
CARD_IDIOMS = 2
CARD_VOCABULARY = 4
CARD_FOCUS = 3


def render_card(user_summary: Dict[str, Any]) -> str:
    """Текстовая карточка с разбором (HTML). Секции, не поместившиеся в лимит Telegram, отбрасываются целиком."""
    esc = html.escape

    def text(value: Any) -> str:
        return esc(str(value or "").strip())

    def items(key: str, limit: int) -> list:
        return [i for i in (user_summary.get(key) or []) if isinstance(i, dict)][:limit]

    sections = []  # каждая секция: список строк

    best = user_summary.get("best_moment")
    if best:
        sections.append(["⭐ <b>Best moment</b>", text(best)])

    mistakes = items("mistakes", CARD_MISTAKES)
    if mistakes:
        sections.append(["✏️ <b>Worth a second look</b>"] + [
            f"• {text(m.get('was'))} → <b>{text(m.get('correct'))}</b>\n  <i>{text(m.get('why'))}</i>"
            for m in mistakes
        ])

    upgrades = items("upgrades", CARD_UPGRADES)
    if upgrades:
        sections.append(["💡 <b>Say it better</b>"] + [
            f"• {text(u.get('you_said'))} → <b>{text(u.get('try'))}</b>\n  <i>{text(u.get('why'))}</i>"
            for u in upgrades
        ])

    idioms = items("idioms", CARD_IDIOMS)
    if idioms:
        sections.append(["🧩 <b>Idioms to try</b>"] + [
            f"• <b>{text(i.get('idiom'))}</b>: {text(i.get('meaning'))}\n  <i>{text(i.get('example'))}</i>"
            for i in idioms
        ])

    vocabulary = items("vocabulary", CARD_VOCABULARY)
    if vocabulary:
        sections.append(["📚 <b>Words for your topics</b>"] + [
            f"• <b>{text(v.get('word'))}</b>: {text(v.get('meaning'))}\n  <i>{text(v.get('example'))}</i>"
            for v in vocabulary
        ])

    level = user_summary.get("level_cefr")
    if level:
        sections.append([f"📈 <b>Level</b> (roughly): {text(level)}"])

    focus = [f for f in (user_summary.get("focus") or []) if f][:CARD_FOCUS]
    if focus:
        sections.append(["🎯 <b>Next time</b>"] + [f"• {text(f)}" for f in focus])

    header = f"📋 <b>{esc(DISPLAY_NAME)} — Session Summary</b>"
    while sections:
        card = "\n\n".join([header] + ["\n".join(section) for section in sections])
        if len(card) <= CARD_LIMIT:
            return card
        sections.pop()  # не помещается: убираем последнюю секцию целиком, теги остаются закрытыми
    return header


async def generate_and_store(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Генерирует SS + SS4T по сессии и пишет в БД. Возвращает user_summary (dict)
    или None, если генерация провалилась (сессия остаётся без user_summary,
    повтор — по /summary или следующему сообщению юзера).
    """
    user = await db.get_or_create_user(session["user_id"])
    dialog = await db.get_session_dialog(session["id"], limit=200)
    errors = await db.get_session_errors(session["id"])
    stats = await db.get_session_answer_stats(session["id"])

    result = await groq_client.generate_session_summary(
        name=user.get("name") or "",
        level=user.get("level") or "",
        dialog=dialog,
        errors=errors,
        avg_words=stats["avg_words"],
    )
    if not result:
        return None

    user_summary = result["user_summary"]
    teacher_summary = result["teacher_summary"]
    # Длину ответов считает код, а не LLM.
    style = teacher_summary.get("answer_style")
    if not isinstance(style, dict):
        style = {}
    style["avg_words"] = stats["avg_words"]
    teacher_summary["answer_style"] = style
    if user_summary.get("level_cefr") and not teacher_summary.get("level_estimate"):
        teacher_summary["level_estimate"] = user_summary["level_cefr"]

    await db.save_summary(
        session["id"], json.dumps(user_summary, ensure_ascii=False), teacher_summary
    )
    return user_summary


async def send_summary(bot: Bot, chat_id: int, user_id: int, user_summary: Dict[str, Any]) -> None:
    """Голосовое + текстовая карточка. Если TTS недоступен, остаётся карточка."""
    script = str(user_summary.get("voice_script") or "").strip()
    if script:
        voice_bytes = await groq_client.text_to_speech(script, voice=VOICE)
        if voice_bytes:
            sent = await bot.send_voice(
                chat_id,
                BufferedInputFile(voice_bytes, filename="summary.wav"),
                caption=f"🎙 {DISPLAY_NAME} — Session Summary",
            )
            msg_id = await db.save_message(user_id, "assistant", script)
            await db.set_message_telegram_id(msg_id, sent.message_id)
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=sent.message_id,
                reply_markup=get_voice_keyboard(sent.message_id),
            )
    await bot.send_message(chat_id, render_card(user_summary), parse_mode="HTML")


async def deliver_summary(
    bot: Bot, session: Dict[str, Any], *, notify_on_fail: bool = True
) -> bool:
    """
    Генерирует (если ещё нет) и отправляет SS. Ошибки отправки (бот
    заблокирован и т.п.) ловим и логируем: на сессию они не влияют.
    """
    user_id = session["user_id"]
    generating.add(user_id)
    try:
        user_summary: Optional[Dict[str, Any]] = None
        if session.get("user_summary"):
            user_summary = json.loads(session["user_summary"])
        else:
            user_summary = await generate_and_store(session)

        if not user_summary:
            if notify_on_fail:
                await _safe_send(
                    bot, user_id,
                    "I couldn't finish my notes just now. Send /summary in a little "
                    "while and I'll try again.",
                )
            return False

        try:
            await send_summary(bot, user_id, user_id, user_summary)
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить SS юзеру {user_id}: {e}")
            return False
        return True
    finally:
        generating.discard(user_id)


async def _safe_send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось отправить сообщение юзеру {chat_id}: {e}")
