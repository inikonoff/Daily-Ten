import asyncio
import html
import logging
from typing import Dict, Optional

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.config import (
    ADMIN_IDS,
    VOICE,
    DISPLAY_NAME,
    PRODUCT_NAME,
    SESSION_PAIRS,
    CONTEXT_MESSAGES,
    SS4T_CONTEXT,
    MAX_CONSECUTIVE_NON_ANSWERS,
    MIN_PAIRS_TO_FINISH_EARLY,
)
from src.db import db
from src.groq_client import groq_client
from src.keyboards import (
    get_voice_keyboard,
    get_text_shown_keyboard,
    get_translate_shown_keyboard,
    get_start_ten_keyboard,
    get_summary_keyboard,
    get_finish_confirm_keyboard,
    get_status_keyboard,
    has_finish_button,
    FINISH_ASK_DATA,
    progress_bar,
    split_progress_bar,
)
from src.prompt import EARLY_USER_MESSAGE
from src.session import EARLY, OPENING, mode_for_reply, minsk_date, reply_number, word_count
from src.summary import deliver_summary, generating, needs_summary

logger = logging.getLogger(__name__)
router = Router()

# Telegram отклоняет edit_caption выше этого лимита (MEDIA_CAPTION_TOO_LONG) —
# у обычного текстового сообщения лимит 4096, у подписи к медиа — 1024.
# Это реальный баг, пойманный в speechflow (см. его changelog) — здесь
# закладываем защиту сразу, а не после жалоб пользователей.
TELEGRAM_CAPTION_LIMIT = 1024

SUMMARY_BUSY_TEXT = "One moment, I'm gathering my notes."
RETRY_TEXT = "Forgive me, I lost my thread for a moment. Could you say that once more?"

# In-memory кэш текста под кнопками Text/Translate — быстрый путь, с
# фоллбэком на БД (messages.telegram_message_id) после рестарта процесса.
_originals: Dict[int, str] = {}
_translations: Dict[int, str] = {}

# Один юзер — один обработчик за раз: счётчик пар и состояние сессии нельзя
# менять параллельно (aiogram обрабатывает апдейты конкурентно).
_user_locks: Dict[int, asyncio.Lock] = {}


def _user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = _user_locks[user_id] = asyncio.Lock()
    return lock


async def _edit_display(message: Message, new_content: str, reply_markup) -> None:
    """
    Text/Translate-кнопки висят и под голосовыми (подпись к медиа), и под
    обычными текстовыми ответами (если юзер писал текстом) — edit_caption
    работает только для первых, edit_text только для вторых. Без этой
    развилки Translate на текстовом ответе падал бы с ошибкой API.
    """
    is_media = bool(message.voice or message.audio or message.video or message.photo or message.document)
    if is_media:
        await message.edit_caption(caption=new_content, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await message.edit_text(new_content, parse_mode="HTML", reply_markup=reply_markup)


def _cache_original(telegram_message_id: int, text: str) -> None:
    if len(_originals) > 5000:
        _originals.clear()
    _originals[telegram_message_id] = text


async def _get_original(user_id: int, telegram_message_id: int) -> Optional[str]:
    cached = _originals.get(telegram_message_id)
    if cached:
        return cached
    text = await db.get_message_by_telegram_id(user_id, telegram_message_id)
    if text:
        _cache_original(telegram_message_id, text)
    return text


# ─── Онбординг ────────────────────────────────────────────────────────────────

class Onboarding(StatesGroup):
    waiting_name = State()
    waiting_level = State()


def _level_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Beginner", callback_data="level_beginner"),
        InlineKeyboardButton(text="Intermediate", callback_data="level_intermediate"),
        InlineKeyboardButton(text="Advanced", callback_data="level_advanced"),
    )
    return builder.as_markup()


async def _begin_onboarding(message: Message, state: FSMContext) -> None:
    await state.set_state(Onboarding.waiting_name)
    await message.answer(
        f"Hello, I'm {DISPLAY_NAME}, and this is {PRODUCT_NAME}. Each day we have "
        f"one conversation of ten exchanges, and at the end I write you my "
        f"notes on your English. What should I call you?"
    )


@router.message(Onboarding.waiting_name, F.text, ~F.text.startswith("/"))
async def onboarding_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()[:100]
    if not name:
        await message.answer("Just your name is fine.")
        return
    await db.set_name(message.from_user.id, name)
    await state.set_state(Onboarding.waiting_level)
    await message.answer(
        f"Lovely to meet you, {name}. How would you describe your English?",
        reply_markup=_level_keyboard(),
    )


@router.callback_query(Onboarding.waiting_level, F.data.startswith("level_"))
async def onboarding_level(callback: CallbackQuery, state: FSMContext, bot: Bot):
    level = callback.data.removeprefix("level_")
    await db.set_level(callback.from_user.id, level)
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await _dispatch(callback.message, bot, callback.from_user.id, text=None, as_voice=False)


# ─── Команды ──────────────────────────────────────────────────────────────────

def _status_text(session: dict) -> str:
    reply_no = min(reply_number(session["pairs_done"]), SESSION_PAIRS)
    return f"We're in the middle of today's ten. Reply {reply_no} of {SESSION_PAIRS}.\n\n{progress_bar(reply_no)}"


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user = await db.get_or_create_user(message.from_user.id)
    if not (user["name"] and user["level"]):
        await _begin_onboarding(message, state)
        return
    await _send_status(message, message.from_user.id)


async def _send_status(target: Message, user_id: int) -> None:
    """Статус для повторного /start: ничего не сбрасывает и не запускает."""
    session = await db.get_active_session(user_id)
    if session:
        await target.answer(
            _status_text(session),
            reply_markup=get_status_keyboard(_can_finish(session)),
        )
        return
    last = await db.get_last_session(user_id)
    if _finished_today(last):
        await target.answer(
            "Today's ten is done. The next one is tomorrow.",
            reply_markup=get_summary_keyboard(),
        )
        return
    await target.answer(
        "Ready for a new ten whenever you are.", reply_markup=get_start_ten_keyboard()
    )


@router.message(Command("ten"))
async def cmd_ten(message: Message, state: FSMContext, bot: Bot):
    await _request_ten(message, state, bot, message.from_user.id)


@router.callback_query(F.data == "ten_start")
async def cq_ten_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    await _request_ten(callback.message, state, bot, callback.from_user.id)


async def _request_ten(target: Message, state: FSMContext, bot: Bot, user_id: int) -> None:
    user = await db.get_or_create_user(user_id)
    if not (user["name"] and user["level"]):
        await _begin_onboarding(target, state)
        return
    if await state.get_state() is not None:
        await state.clear()
    await _dispatch(target, bot, user_id, text=None, as_voice=False)


@router.message(Command("summary"))
async def cmd_summary(message: Message, bot: Bot):
    await _show_summary(message, bot, message.from_user.id)


@router.callback_query(F.data == "summary_show")
async def cq_summary_show(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await _show_summary(callback.message, bot, callback.from_user.id)


async def _show_summary(target: Message, bot: Bot, user_id: int) -> None:
    if user_id in generating:
        await target.answer(SUMMARY_BUSY_TEXT)
        return
    async with _user_lock(user_id):
        if user_id in generating:
            await target.answer(SUMMARY_BUSY_TEXT)
            return
        last = await db.get_last_session(user_id)
        if last and needs_summary(last):
            # SS не сгенерировалась в прошлый раз — пробуем ещё раз.
            session = last
        else:
            session = await db.get_latest_summarized_session(user_id)
        if not session:
            if await db.get_active_session(user_id):
                await target.answer(f"My notes come after the {SESSION_PAIRS}th reply. We're not there yet.")
            else:
                await target.answer("There's no summary yet. Start a ten and I'll write you one.",
                                    reply_markup=get_start_ten_keyboard())
            return
        generating.add(user_id)
    await deliver_summary(bot, session)


# ─── Досрочное завершение («Finish the session») ─────────────────────────────

@router.message(Command("finish"))
async def cmd_finish(message: Message, bot: Bot):
    await _request_finish(message, bot, message.from_user.id, confirmed=False)


@router.callback_query(F.data == FINISH_ASK_DATA)
async def cq_finish_ask(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await _request_finish(callback.message, bot, callback.from_user.id, confirmed=False)


@router.callback_query(F.data == "finish_yes")
async def cq_finish_yes(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await _request_finish(callback.message, bot, callback.from_user.id, confirmed=True)


@router.callback_query(F.data == "finish_no")
async def cq_finish_no(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Of course. Let's go on, then.")


async def _request_finish(target: Message, bot: Bot, user_id: int, confirmed: bool) -> None:
    """
    Юзер сам завершает сессию досрочно (не раньше MIN_PAIRS_TO_FINISH_EARLY пар).
    Сначала подтверждение, потом прощальная реплика МС (пишет LLM) и Summary.
    """
    to_summarize: Optional[dict] = None
    async with _user_lock(user_id):
        if user_id in generating:
            await target.answer(SUMMARY_BUSY_TEXT)
            return
        session = await db.get_active_session(user_id)
        if not session:
            await target.answer(
                "There's nothing to wrap up right now.", reply_markup=get_start_ten_keyboard()
            )
            return
        if not _can_finish(session):
            await target.answer(
                f"Let's talk a little longer first. I need at least "
                f"{MIN_PAIRS_TO_FINISH_EARLY} exchanges to write you anything useful. "
                f"We have {session['pairs_done']} so far."
            )
            return
        if not confirmed:
            await target.answer(
                "Shall we wrap up here? I'll write my notes from what we've said so far.",
                reply_markup=get_finish_confirm_keyboard(),
            )
            return
        to_summarize = await _finish_early(target, session)
    if to_summarize:
        await deliver_summary(bot, to_summarize)


async def _finish_early(target: Message, session: dict) -> Optional[dict]:
    user_id = session["user_id"]
    user = await db.get_or_create_user(user_id)
    history = await db.get_session_dialog(session["id"], CONTEXT_MESSAGES)
    teacher_summaries = await db.get_recent_teacher_summaries(user_id, SS4T_CONTEXT)

    result = await groq_client.generate_reply(
        EARLY_USER_MESSAGE,
        name=user.get("name") or "",
        level=user.get("level") or "intermediate",
        history=history,
        mode=EARLY,
        reply_no=reply_number(session["pairs_done"]),
        teacher_summaries=teacher_summaries,
    )
    if not result:
        await target.answer(RETRY_TEXT)
        return None

    finished = await db.finish_session(session["id"], session["pairs_done"], reason="early")
    if not finished:
        return None
    generating.add(user_id)
    try:
        await _send_mc_reply(target, user_id, session["id"], result["reply"], None, as_voice=False)
    except Exception:
        generating.discard(user_id)  # SS позже сгенерируется по /summary или сообщению юзера
        raise
    return finished


# ─── Машина состояний сессии ──────────────────────────────────────────────────

def _can_finish(session: dict) -> bool:
    """Досрочно завершить можно, когда пар уже достаточно для осмысленной Summary."""
    return session["pairs_done"] >= MIN_PAIRS_TO_FINISH_EARLY


def _finished_today(session: Optional[dict]) -> bool:
    """Десятка пройдена сегодня: finished/early и завершена в текущие сутки по Минску.
    Закрытая по таймауту сессия не блокирует новую."""
    return bool(
        session
        and session["status"] == "completed"
        and session["completion_reason"] in ("finished", "early")
        and session["completed_at"]
        and minsk_date(session["completed_at"]) == minsk_date()
    )


async def _send_mc_reply(
    target: Message,
    user_id: int,
    session_id: int,
    reply: str,
    reply_no: Optional[int],
    as_voice: bool,
    show_finish: bool = False,
) -> None:
    """
    Отправляет реплику МС и сохраняет её. Полоска прогресса — отдельная
    последняя строка (в голосовом — в подписи, в TTS не попадает);
    `reply_no=None` — без полоски (закрывающая реплика).
    """
    bar = progress_bar(reply_no) if reply_no else ""
    bot_msg_id = await db.save_message(user_id, "assistant", reply, session_id=session_id)

    if as_voice:
        voice_bytes = await groq_client.text_to_speech(reply, voice=VOICE)
        if voice_bytes:
            caption = f"🎙 {DISPLAY_NAME}" + (f"\n{bar}" if bar else "")
            sent = await target.answer_voice(
                BufferedInputFile(voice_bytes, filename="reply.wav"), caption=caption
            )
            await db.set_message_telegram_id(bot_msg_id, sent.message_id)
            _cache_original(sent.message_id, reply)
            await sent.edit_reply_markup(
                reply_markup=get_voice_keyboard(sent.message_id, finish=show_finish)
            )
            return
        body = f"💬 {html.escape(reply)}"
    else:
        body = html.escape(reply)

    sent = await target.answer(body + (f"\n\n{bar}" if bar else ""), parse_mode="HTML")
    await db.set_message_telegram_id(bot_msg_id, sent.message_id)
    _cache_original(sent.message_id, reply)
    await sent.edit_reply_markup(
        reply_markup=get_text_shown_keyboard(sent.message_id, finish=show_finish)
    )


async def _open_session(target: Message, user: dict, as_voice: bool) -> None:
    """Новая сессия: МС открывает её сама, опираясь на последние SS4T."""
    user_id = user["telegram_id"]
    teacher_summaries = await db.get_recent_teacher_summaries(user_id, SS4T_CONTEXT)
    result = await groq_client.generate_reply(
        None,
        name=user.get("name") or "",
        level=user.get("level") or "intermediate",
        history=[],
        mode=OPENING,
        reply_no=reply_number(0),
        teacher_summaries=teacher_summaries,
    )
    if not result:
        # Сессию не создаём: иначе остался бы «активный» разговор без реплики МС.
        await target.answer("Forgive me, I couldn't start our conversation just now. Try /ten again in a moment.")
        return
    session = await db.create_session(user_id, minsk_date())
    await _send_mc_reply(target, user_id, session["id"], result["reply"], reply_number(0), as_voice)


async def _continue_session(
    target: Message, user: dict, session: dict, text: str, as_voice: bool
) -> Optional[dict]:
    """
    Сообщение юзера в активной сессии. Возвращает сессию, для которой надо
    отправить SS (после закрывающей реплики), иначе None.
    """
    user_id = user["telegram_id"]
    pairs = session["pairs_done"]
    streak = session["non_answer_streak"]
    force = streak >= MAX_CONSECUTIVE_NON_ANSWERS

    # Режим выбираем до вызова LLM, по предположению «сообщение засчитается».
    prospective_reply_no = reply_number(pairs + 1)
    history = await db.get_session_dialog(session["id"], CONTEXT_MESSAGES)
    teacher_summaries = await db.get_recent_teacher_summaries(user_id, SS4T_CONTEXT)

    result = await groq_client.generate_reply(
        text,
        name=user.get("name") or "",
        level=user.get("level") or "intermediate",
        history=history,
        mode=mode_for_reply(prospective_reply_no),
        reply_no=prospective_reply_no,
        force_answer=force,
        teacher_summaries=teacher_summaries,
    )
    if not result:
        # Ничего не сохраняем и счётчик не трогаем: юзер просто повторит.
        await target.answer(RETRY_TEXT)
        return None

    counts = True if force else result["counts_as_answer"]

    await db.save_message(
        user_id, "user", text,
        session_id=session["id"], counted=counts, word_count=word_count(text),
    )
    if result.get("category") not in (None, "none", "") and result.get("mistake"):
        await db.log_error(
            user_id, result["category"], result["mistake"], result.get("corrected", ""),
            session_id=session["id"],
        )

    if counts:
        pairs += 1
        streak = 0
    else:
        streak += 1

    if counts and pairs >= SESSION_PAIRS:
        # Закрывающая реплика: закрываем сессию атомарно ДО отправки, чтобы
        # сообщения юзера не считались ответами, пока МС прощается и пишет SS.
        finished = await db.finish_session(session["id"], pairs)
        if finished:
            generating.add(user_id)
        try:
            await _send_mc_reply(target, user_id, session["id"], result["reply"], None, as_voice)
        except Exception:
            generating.discard(user_id)  # SS позже сгенерируется по /summary или сообщению юзера
            raise
        return finished

    await db.advance_session(session["id"], pairs, streak)
    await _send_mc_reply(
        target, user_id, session["id"], result["reply"], reply_number(pairs), as_voice,
        show_finish=pairs >= MIN_PAIRS_TO_FINISH_EARLY,
    )
    return None


async def _dispatch(
    target: Message, bot: Bot, user_id: int, text: Optional[str], as_voice: bool
) -> None:
    """
    Единая точка входа. `text` — сообщение юзера; `text=None` — явный запрос
    десятки (/ten, кнопка, конец онбординга), он ответом не считается.
    """
    to_summarize: Optional[dict] = None
    async with _user_lock(user_id):
        if user_id in generating:
            await target.answer(SUMMARY_BUSY_TEXT)
            return

        user = await db.get_or_create_user(user_id)
        session = await db.get_active_session(user_id)

        if session:
            if text is None:
                await target.answer(
                    _status_text(session),
                    reply_markup=get_status_keyboard(_can_finish(session)),
                )
            else:
                to_summarize = await _continue_session(target, user, session, text, as_voice)
        else:
            last = await db.get_last_session(user_id)
            if last and needs_summary(last):
                # SS не сгенерировалась в прошлый раз — повторяем, новую не начинаем.
                generating.add(user_id)
                to_summarize = last
            elif _finished_today(last):
                await target.answer(
                    "That's your ten for today. We'll start the next one tomorrow.",
                    reply_markup=get_summary_keyboard(),
                )
            else:
                # Новая сессия. Сообщение, которое её вызвало, ответом не считается.
                await _open_session(target, user, as_voice)

    if to_summarize:
        await deliver_summary(bot, to_summarize)


async def _handle_input(
    message: Message, state: FSMContext, bot: Bot, text: str, as_voice: bool
) -> None:
    user_id = message.from_user.id
    is_admin = user_id in ADMIN_IDS

    burst = await db.check_burst(user_id, is_admin)
    if burst["blocked"]:
        return
    if burst["warn"]:
        await message.answer("You're sending messages very fast — slow down a little so I can keep up.")

    user = await db.get_or_create_user(user_id)
    if not (user["name"] and user["level"]):
        # Онбординг не пройден (например, FSM-состояние потерялось при рестарте).
        await _begin_onboarding(message, state)
        return

    await _dispatch(message, bot, user_id, text=text, as_voice=as_voice)


@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext, bot: Bot):
    if await state.get_state() is not None:
        await message.answer("Text works better while we're getting set up — go ahead.")
        return
    if message.from_user.id in generating:
        await message.answer(SUMMARY_BUSY_TEXT)
        return
    file = await bot.get_file(message.voice.file_id)
    audio_bytes = (await bot.download_file(file.file_path)).read()
    text = await groq_client.transcribe_audio(audio_bytes)
    if not text:
        await message.answer("I couldn't quite hear that — mind trying again?")
        return
    await _handle_input(message, state, bot, text, as_voice=True)


@router.message(F.text)
async def handle_text(message: Message, state: FSMContext, bot: Bot):
    if await state.get_state() is not None:
        await message.answer("Go ahead and tap one of the buttons above.")
        return
    await _handle_input(message, state, bot, message.text, as_voice=False)


# ─── Text / Translate кнопки ──────────────────────────────────────────────────

def _with_bar(caption: str, current: Optional[str]) -> str:
    """Сохраняет полоску прогресса реплики при редактировании подписи/текста."""
    _, bar = split_progress_bar(current or "")
    return f"{caption}\n\n{bar}" if bar else caption


@router.callback_query(F.data.startswith("text_"))
async def cq_show_text(callback: CallbackQuery):
    message_id = int(callback.data.removeprefix("text_"))
    text = await _get_original(callback.from_user.id, message_id)
    if not text:
        await callback.answer("Not available.", show_alert=True)
        return
    finish = has_finish_button(callback.message.reply_markup)
    caption = _with_bar(f"💬 {html.escape(text)}", callback.message.caption or callback.message.text)
    if len(caption) > TELEGRAM_CAPTION_LIMIT:
        # Подпись к медиа ограничена 1024 символами — длинный текст
        # (например, Session Summary) уходит отдельным сообщением.
        sent = await callback.message.answer(caption, parse_mode="HTML")
        _cache_original(sent.message_id, text)
        await sent.edit_reply_markup(
            reply_markup=get_text_shown_keyboard(sent.message_id, finish=finish)
        )
    else:
        await _edit_display(
            callback.message, caption, get_text_shown_keyboard(message_id, finish=finish)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("translate_"))
async def cq_translate(callback: CallbackQuery):
    message_id = int(callback.data.removeprefix("translate_"))
    text = await _get_original(callback.from_user.id, message_id)
    if not text:
        await callback.answer("Not available.", show_alert=True)
        return

    translation = _translations.get(message_id)
    if not translation:
        translation = await groq_client.translate_text(text)
        _translations[message_id] = translation

    finish = has_finish_button(callback.message.reply_markup)
    caption = _with_bar(f"🌐 {html.escape(translation)}", callback.message.caption or callback.message.text)
    if len(caption) > TELEGRAM_CAPTION_LIMIT:
        sent = await callback.message.answer(caption, parse_mode="HTML")
        _cache_original(sent.message_id, text)  # Original на новом сообщении должен работать
        _translations[sent.message_id] = translation
        await sent.edit_reply_markup(
            reply_markup=get_translate_shown_keyboard(sent.message_id, finish=finish)
        )
    else:
        await _edit_display(
            callback.message, caption, get_translate_shown_keyboard(message_id, finish=finish)
        )
    await callback.answer()
