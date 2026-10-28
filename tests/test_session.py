from datetime import datetime, timezone

from src.session import (
    CLOSING, NORMAL, OPENING, PREFINAL,
    minsk_date, mode_for_reply, reply_number, shorten_reply, truncate_message, word_count,
)


def test_reply_number_counts_opening_as_first():
    assert reply_number(0) == 1
    assert reply_number(9) == 10


def test_modes_follow_the_counter():
    modes = [mode_for_reply(n) for n in range(1, 12)]
    assert modes == [OPENING] + [NORMAL] * 8 + [PREFINAL, CLOSING]


def test_minsk_midnight_is_the_day_boundary():
    # 21:30 UTC = 00:30 по Минску (UTC+3) следующего дня
    assert str(minsk_date(datetime(2026, 10, 18, 21, 30, tzinfo=timezone.utc))) == "2026-10-19"
    assert str(minsk_date(datetime(2026, 10, 18, 20, 30, tzinfo=timezone.utc))) == "2026-10-18"


def test_word_count():
    assert word_count("one two  three\nfour") == 4
    assert word_count("") == 0


def test_truncate_message_keeps_short_text_and_cuts_on_word_boundary():
    assert truncate_message("short", 100) == "short"
    text = "word " * 100
    cut = truncate_message(text, 52)
    assert len(cut) <= 52 and not cut.endswith(" ") and cut.endswith("word")


def test_shorten_reply_keeps_first_and_last_sentence():
    text = "First reaction here. " + "Middle sentence with many words in it. " * 6 + "Tell me more about that."
    short = shorten_reply(text, 20)
    assert short.startswith("First reaction here.")
    assert short.endswith("Tell me more about that.")
    assert word_count(short) <= 20  # средние фразы убираются, пока не уложились в лимит
    assert short.count("Middle") < 6


def test_shorten_reply_leaves_two_sentences_alone():
    text = "A long first sentence " + "word " * 80 + ". And the closing one."
    assert shorten_reply(text, 10) == text.strip()
