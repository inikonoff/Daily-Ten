from src.prompt import build_system_prompt, format_memory
from src.session import CLOSING, EARLY, NORMAL, OPENING, PREFINAL

SS4T = {
    "topics": ["gardening", "a trip to Brest"],
    "interests": ["photography"],
    "answer_style": {"brevity": "short", "avg_words": 4.5},
    "errors": [
        {"phrase": "I has", "correction": "I have", "recurring": True},
        {"phrase": "in monday", "correction": "on Monday", "recurring": False},
    ],
    "level_estimate": "B1",
    "pick_up_next": ["her brother in Minsk"],
}


def test_every_mode_builds_a_prompt_with_student_and_turn_blocks():
    for mode in (OPENING, NORMAL, PREFINAL, CLOSING, EARLY):
        prompt = build_system_prompt("Anna", "beginner", mode, 4, teacher_summaries=[SS4T])
        assert "Anna" in prompt and "# THIS TURN" in prompt and "# YOUR NOTES" in prompt


def test_non_answer_reminder_only_for_regular_turns():
    assert "ignore the mode" in build_system_prompt("A", "x", NORMAL, 4)
    assert "ignore the mode" not in build_system_prompt("A", "x", EARLY, 4)
    assert "ignore the mode" not in build_system_prompt("A", "x", OPENING, 1)


def test_force_answer_is_added_for_regular_turns_only():
    assert "counts_as_answer must be true" in build_system_prompt("A", "x", NORMAL, 4, force_answer=True)
    assert "Treat the student's message as an answer" not in build_system_prompt("A", "x", NORMAL, 4)


def test_no_memory_block_without_summaries():
    assert "# YOUR NOTES" not in build_system_prompt("A", "x", OPENING, 1)


def test_format_memory_is_compact_and_keeps_only_recurring_errors():
    memory = format_memory([SS4T])
    assert "gardening" in memory and "photography" in memory and "level: B1" in memory
    assert "I has" in memory and "in monday" not in memory
    assert "{" not in memory  # не JSON-дамп
    assert len(memory) < len(str(SS4T))


def test_format_memory_skips_empty_notes():
    assert format_memory([{}]) == ""
