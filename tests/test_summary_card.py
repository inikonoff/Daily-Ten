from src.summary import CARD_LIMIT, render_card

FULL = {
    "best_moment": "You described the events with real feeling.",
    "mistakes": [{"was": "I have seen it yesterday", "correct": "I saw it yesterday", "why": "finished time needs past simple"}] * 5,
    "upgrades": [{"you_said": "very good book", "try": "a gripping read", "why": "more vivid"}] * 4,
    "idioms": [{"idiom": "a page-turner", "meaning": "a book you cannot put down", "example": "Tom Sawyer is a page-turner."}] * 3,
    "vocabulary": [{"word": "mischievous", "meaning": "playfully naughty", "example": "Tom is a mischievous boy."}] * 5,
    "level_cefr": "B1",
    "focus": ["past simple: 'I have seen it yesterday' -> 'I saw it yesterday'", "b", "c", "d"],
}


def test_card_shows_examples_and_limits_each_section():
    card = render_card(FULL)
    for needle in ("Worth a second look", "Say it better", "Idioms to try", "Words for your topics",
                   "Next time", "I saw it yesterday", "a gripping read", "a page-turner", "mischievous"):
        assert needle in card
    assert card.count("I have seen it yesterday") == 3 + 1  # 3 ошибки + пример в focus
    assert card.count("mischievous") == 4 + 4  # 4 слова: слово и пример
    assert "\nd" not in card  # focus не больше трёх


def test_card_without_extra_lists_still_renders_old_fields():
    card = render_card({"best_moment": "b", "level_cefr": "B1", "focus": ["f"], "mistakes": []})
    assert "Best moment" in card and "Level" in card and "Worth a second look" not in card


def test_card_is_escaped_and_fits_the_telegram_limit():
    huge = {"best_moment": "<b>x</b>", "mistakes": [{"was": "<i>" + "a" * 300, "correct": "b" * 300, "why": "c" * 300}] * 3,
            "upgrades": [{"you_said": "u" * 600, "try": "t" * 600, "why": "w" * 600}] * 3,
            "idioms": [{"idiom": "i" * 800, "meaning": "m" * 800, "example": "e" * 800}] * 2,
            "vocabulary": [{"word": "w" * 800, "meaning": "m" * 800, "example": "e" * 800}] * 4,
            "level_cefr": "B1", "focus": ["f" * 500] * 3}
    card = render_card(huge)
    assert len(card) <= CARD_LIMIT
    assert "&lt;b&gt;x&lt;/b&gt;" in card and "<i>" + "a" not in card
    assert card.count("<b>") == card.count("</b>") and card.count("<i>") == card.count("</i>")  # теги не обрезаны
