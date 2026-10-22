from typing import Optional

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


FINISH_ASK_DATA = "finish_ask"
FINISH_BUTTON_TEXT = "🏁 Finish the session"


def _add_finish_row(builder: InlineKeyboardBuilder, finish: bool) -> None:
    """Кнопка досрочного завершения: показывается, когда пар уже достаточно для Summary."""
    if finish:
        builder.row(InlineKeyboardButton(text=FINISH_BUTTON_TEXT, callback_data=FINISH_ASK_DATA))


def has_finish_button(markup) -> bool:
    """Есть ли кнопка «Finish» в клавиатуре сообщения (Text/Translate её сохраняют)."""
    if not markup:
        return False
    return any(b.callback_data == FINISH_ASK_DATA for row in markup.inline_keyboard for b in row)


def get_voice_keyboard(message_id: int, finish: bool = False) -> InlineKeyboardMarkup:
    """Под голосовым ответом Mrs. Smith: показать текст / перевод."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📝 Text", callback_data=f"text_{message_id}"),
        InlineKeyboardButton(text="🌐 Translate", callback_data=f"translate_{message_id}"),
    )
    _add_finish_row(builder, finish)
    return builder.as_markup()


def get_text_shown_keyboard(message_id: int, finish: bool = False) -> InlineKeyboardMarkup:
    """После показа текста — только Translate (Original вернёт обратно к тексту)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🌐 Translate", callback_data=f"translate_{message_id}"))
    _add_finish_row(builder, finish)
    return builder.as_markup()


def get_finish_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Yes, wrap up", callback_data="finish_yes"),
        InlineKeyboardButton(text="Keep going", callback_data="finish_no"),
    )
    return builder.as_markup()


def get_status_keyboard(finish: bool) -> Optional[InlineKeyboardMarkup]:
    if not finish:
        return None
    builder = InlineKeyboardBuilder()
    _add_finish_row(builder, True)
    return builder.as_markup()


def get_translate_shown_keyboard(message_id: int, finish: bool = False) -> InlineKeyboardMarkup:
    """
    После перевода — Original. Важно: Original должен снова показать текст
    (ту же функцию, что и кнопка Text), а не сбрасывать всё в пустоту — это
    была реальная найденная в speechflow ошибка (см. roadmap.md того проекта),
    тут делаем сразу правильно.
    """
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔤 Original", callback_data=f"text_{message_id}"))
    _add_finish_row(builder, finish)
    return builder.as_markup()


def get_start_ten_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="▶️ Start the ten", callback_data="ten_start"))
    return builder.as_markup()


def get_summary_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📋 My Session Summary", callback_data="summary_show"))
    return builder.as_markup()


# ─── Счётчик прогресса (первая версия — оценим в деле, формат правится здесь) ─

PROGRESS_TOTAL = 10
PROGRESS_FILLED = "●"
PROGRESS_EMPTY = "○"
_PROGRESS_CHARS = (PROGRESS_FILLED, PROGRESS_EMPTY)


def progress_bar(reply_no: int) -> str:
    """●●●○○○○○○○ — заполнено столько кружков, какой номер реплики МС (1…10)."""
    filled = max(0, min(reply_no, PROGRESS_TOTAL))
    return PROGRESS_FILLED * filled + PROGRESS_EMPTY * (PROGRESS_TOTAL - filled)


def split_progress_bar(text: str) -> tuple:
    """
    Отделяет полоску (последняя строка из кружков) от текста подписи/сообщения.
    Нужно кнопкам Text/Translate, чтобы не терять полоску при редактировании.
    Возвращает (текст_без_полоски, полоска_или_пустая_строка).
    """
    lines = text.rstrip().split("\n")
    last = lines[-1].strip() if lines else ""
    if last and all(ch in _PROGRESS_CHARS for ch in last):
        return "\n".join(lines[:-1]).rstrip(), last
    return text, ""


# ─── Админ-панель ────────────────────────────────────────────────────────────

def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="❗ Error categories", callback_data="admin_errors"))
    builder.row(InlineKeyboardButton(text="🔟 Sessions", callback_data="admin_sessions"))
    builder.row(InlineKeyboardButton(text="👥 Users", callback_data="admin_users_0"))
    return builder.as_markup()


def get_admin_back_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="« Back", callback_data="admin_back"))
    return builder.as_markup()


def get_admin_users_keyboard(users: list, offset: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for u in users:
        label = u["name"] or str(u["telegram_id"])
        builder.row(InlineKeyboardButton(text=label, callback_data=f"admin_user_{u['telegram_id']}"))
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="« Prev", callback_data=f"admin_users_{max(0, offset - 20)}"))
    if len(users) == 20:
        nav.append(InlineKeyboardButton(text="Next »", callback_data=f"admin_users_{offset + 20}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="« Back", callback_data="admin_back"))
    return builder.as_markup()
