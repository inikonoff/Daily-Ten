import asyncio

import pytest

from src.groq_client import GroqClient, parse_json_robust, strip_think


# ─── разбор JSON ─────────────────────────────────────────────────────────────

def test_parse_plain_json():
    assert parse_json_robust('{"reply": "hi"}') == {"reply": "hi"}


def test_parse_json_in_code_fence():
    assert parse_json_robust('```json\n{"reply": "hi"}\n```') == {"reply": "hi"}


def test_parse_json_with_surrounding_text():
    assert parse_json_robust('Here you go: {"reply": "hi", "x": {"y": 1}} hope it helps') == {
        "reply": "hi", "x": {"y": 1},
    }


def test_parse_strips_think_blocks_and_unclosed_think():
    assert parse_json_robust('<think>hmm {"a": 1}</think>{"reply": "ok"}') == {"reply": "ok"}
    assert strip_think("reasoning without tag</think>answer") == "answer"


@pytest.mark.parametrize("bad", ["", None, "no json here", "[1, 2]", '{"broken": '])
def test_parse_rejects_non_objects(bad):
    with pytest.raises(ValueError):
        parse_json_robust(bad)


# ─── проверка Summary ────────────────────────────────────────────────────────

def _summary(voice="word " * 60):
    return {
        "user_summary": {"voice_script": voice, "best_moment": "b", "mistakes": [], "focus": ["f"]},
        "teacher_summary": {"errors": [], "topics": []},
    }


def test_summary_with_short_voice_is_valid_unless_voice_required():
    ok = GroqClient._validate_summary(_summary("too short"))
    assert ok["user_summary"]["voice_script"] == "too short"
    with pytest.raises(ValueError):
        GroqClient._validate_summary(_summary("too short"), require_voice=True)


def test_summary_without_content_is_rejected():
    empty = {"user_summary": {"voice_script": ""}, "teacher_summary": {}}
    with pytest.raises(ValueError):
        GroqClient._validate_summary(empty)
    with pytest.raises(ValueError):
        GroqClient._validate_summary({"user_summary": "text", "teacher_summary": {}})


def test_summary_coerces_missing_lists():
    data = {"user_summary": {"best_moment": "b"}, "teacher_summary": {}}
    fixed = GroqClient._validate_summary(data)
    assert fixed["user_summary"]["mistakes"] == [] and fixed["teacher_summary"]["errors"] == []


# ─── запасная модель и ротация ключей ────────────────────────────────────────

@pytest.fixture
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr("src.groq_client.asyncio.sleep", fast)


def test_fallback_model_is_used_after_primary_fails(no_sleep):
    client = GroqClient(["k1"])
    seen = []

    async def func(c, model):
        seen.append(model)
        if model == "primary":
            raise RuntimeError("model down")
        return "ok"

    assert asyncio.run(client._make_request(func, models=["primary", "fallback"])) == "ok"
    assert seen == ["primary", "primary", "fallback"]  # 2 попытки на ключ, затем запасная


def test_max_attempts_caps_total_calls(no_sleep):
    client = GroqClient(["k1", "k2"])
    calls = []

    async def func(c, model):
        calls.append(model)
        raise RuntimeError("down")

    with pytest.raises(Exception):
        asyncio.run(client._make_request(func, models=["a", "b"], max_attempts=3))
    assert len(calls) == 3


def test_keys_rotate_between_attempts(no_sleep):
    client = GroqClient(["k1", "k2"])
    used = []

    async def func(c, model):
        used.append(c.api_key)
        if len(used) == 1:
            raise RuntimeError("rate limit")
        return "ok"

    asyncio.run(client._make_request(func))
    assert used == ["k1", "k2"]


def test_no_clients_raises():
    with pytest.raises(Exception):
        asyncio.run(GroqClient([])._make_request(lambda c, m: None))
