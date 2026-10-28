from types import SimpleNamespace

from src import keyboards as kb


def test_progress_bar_fills_by_reply_number():
    assert kb.progress_bar(1) == "●○○○○○○○○○"
    assert kb.progress_bar(3) == "●●●○○○○○○○"
    assert kb.progress_bar(10) == "●" * 10
    assert kb.progress_bar(11) == "●" * 10  # не выходит за 10
    assert kb.progress_bar(0) == "○" * 10


def test_split_progress_bar_separates_last_line():
    assert kb.split_progress_bar("🎙 Daily Ten\n●●○○○○○○○○") == ("🎙 Daily Ten", "●●○○○○○○○○")
    assert kb.split_progress_bar("text\n\n●○○○○○○○○○") == ("text", "●○○○○○○○○○")
    assert kb.split_progress_bar("no bar here") == ("no bar here", "")


def test_finish_button_presence_is_detected():
    with_finish = kb.get_voice_keyboard(5, finish=True)
    without = kb.get_voice_keyboard(5)
    assert kb.has_finish_button(SimpleNamespace(inline_keyboard=with_finish.inline_keyboard))
    assert not kb.has_finish_button(SimpleNamespace(inline_keyboard=without.inline_keyboard))
    assert not kb.has_finish_button(None)


def test_text_and_translate_keyboards_keep_finish():
    for factory in (kb.get_text_shown_keyboard, kb.get_translate_shown_keyboard, kb.get_voice_keyboard):
        markup = factory(7, finish=True)
        assert kb.has_finish_button(SimpleNamespace(inline_keyboard=markup.inline_keyboard))
