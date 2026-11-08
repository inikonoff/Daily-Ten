"""Повторы и запасные пути LLM-слоя на заглушке Groq-клиента (без сети)."""

import asyncio
from types import SimpleNamespace

import pytest

from src import metrics
from src.groq_client import GroqClient
from src.prompt import SUMMARY_PROMPT, SUMMARY_PROMPT_LITE
from src.session import NORMAL


def _response(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class FakeClient:
    """Отвечает по очереди из `answers` (str или callable(kwargs) -> str)."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []
        self.api_key = "fake"
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return _response(answer(kwargs) if callable(answer) else answer)


@pytest.fixture
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr("src.groq_client.asyncio.sleep", fast)


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr(metrics, "record", lambda label, event, **kw: seen.append((label, event)))
    return seen


def _client(fake):
    client = GroqClient(["k"])
    client.clients = [fake]
    return client


def _reply_kwargs():
    return dict(name="Anna", level="beginner", history=[], mode=NORMAL, reply_no=4)


def test_think_leak_and_bad_json_are_retried_at_temperature_zero(no_sleep, events):
    fake = FakeClient([
        "<think>let me think</think>not json at all",
        '{"category": "none", "mistake": "", "corrected": "", "counts_as_answer": true, "reply": "Tell me more."}',
    ])
    result = asyncio.run(_client(fake).generate_reply("hello", **_reply_kwargs()))
    assert result["reply"] == "Tell me more."
    assert [r["temperature"] for r in fake.requests] == [0.7, 0.0]
    assert "# CORRECTION" in fake.requests[1]["messages"][0]["content"]
    assert ("generate_reply", "bad_json") in events


def test_reply_returns_none_when_everything_fails(no_sleep, events):
    fake = FakeClient(["{garbage"])  # битый JSON, не реплика: показывать юзеру нельзя
    assert asyncio.run(_client(fake).generate_reply("hello", **_reply_kwargs())) is None
    assert ("generate_reply", "error") in events


def test_long_reply_is_shortened(no_sleep, events):
    long_reply = "First thought. " + "A long middle sentence with quite a few words in it. " * 8 + "Tell me why."
    fake = FakeClient([f'{{"category": "none", "counts_as_answer": true, "reply": "{long_reply}"}}'])
    result = asyncio.run(_client(fake).generate_reply("hello", **_reply_kwargs()))
    assert result["reply"].startswith("First thought.") and result["reply"].endswith("Tell me why.")
    assert len(result["reply"].split()) < len(long_reply.split())
    assert ("generate_reply", "reply_shortened") in events


GOOD_SUMMARY = (
    '{"user_summary": {"voice_script": "' + "word " * 60 + '", "best_moment": "b", '
    '"mistakes": [], "level_cefr": "B1", "focus": ["f"]}, "teacher_summary": {"errors": [], "topics": []}}'
)
SHORT_VOICE_SUMMARY = (
    '{"user_summary": {"voice_script": "too short", "best_moment": "b", "mistakes": [], "focus": ["f"]}, '
    '"teacher_summary": {"errors": []}}'
)


def _summary_kwargs():
    return dict(name="Anna", level="beginner", dialog=[{"role": "user", "content": "hi"}], errors=[], avg_words=1.0)


def test_summary_falls_back_to_lite_prompt(no_sleep, events):
    def answer(kwargs):
        system = kwargs["messages"][0]["content"]
        return GOOD_SUMMARY if system.startswith(SUMMARY_PROMPT_LITE) else "broken"

    fake = FakeClient([answer])
    result = asyncio.run(_client(fake).generate_session_summary(**_summary_kwargs()))
    assert result["user_summary"]["level_cefr"] == "B1"
    assert any(r["messages"][0]["content"].startswith(SUMMARY_PROMPT) for r in fake.requests)
    assert ("summary", "lite_prompt") in events


def test_short_voice_is_dropped_but_card_survives(no_sleep, events):
    fake = FakeClient([SHORT_VOICE_SUMMARY])
    result = asyncio.run(_client(fake).generate_session_summary(**_summary_kwargs()))
    assert result["user_summary"]["voice_script"] == ""
    assert result["user_summary"]["best_moment"] == "b"
    assert ("summary", "short_voice") in events and ("summary", "voice_dropped") in events


def test_short_voice_recovers_on_retry(no_sleep, events):
    fake = FakeClient([SHORT_VOICE_SUMMARY, GOOD_SUMMARY])
    result = asyncio.run(_client(fake).generate_session_summary(**_summary_kwargs()))
    assert len(result["user_summary"]["voice_script"].split()) == 60


def test_metrics_record_is_noop_without_database():
    metrics.record("test", "event", detail="x")  # не падает без пула и без цикла событий


def test_requests_leave_room_for_reasoning_and_set_low_effort(no_sleep, events):
    """gpt-oss тратит токены ответа на рассуждение: малый лимит давал 400 json_validate_failed."""
    from src.config import REPLY_MAX_TOKENS, SUMMARY_MAX_TOKENS

    fake = FakeClient(['{"category": "none", "counts_as_answer": true, "reply": "Tell me more."}'])
    asyncio.run(_client(fake).generate_reply("hello", **_reply_kwargs()))
    request = fake.requests[0]
    assert request["max_tokens"] == REPLY_MAX_TOKENS >= 1000
    assert request["extra_body"] == {"include_reasoning": False, "reasoning_effort": "low"}

    fake = FakeClient([GOOD_SUMMARY])
    asyncio.run(_client(fake).generate_session_summary(**_summary_kwargs()))
    assert fake.requests[0]["max_tokens"] == SUMMARY_MAX_TOKENS >= 3000


def test_reasoning_effort_can_be_disabled_for_non_reasoning_models(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "GROQ_REASONING_EFFORT", "")
    assert GroqClient._extra_body() == {"include_reasoning": False}


def test_server_side_json_mode_is_off_by_default(no_sleep, events):
    """response_format=json_object у Groq с gpt-oss давал 400 json_validate_failed."""
    fake = FakeClient(['{"category": "none", "counts_as_answer": true, "reply": "Tell me more."}'])
    asyncio.run(_client(fake).generate_reply("hello", **_reply_kwargs()))
    assert "response_format" not in fake.requests[0]


def test_json_mode_can_be_enabled(no_sleep, events, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "GROQ_JSON_MODE", True)
    fake = FakeClient(['{"category": "none", "counts_as_answer": true, "reply": "Tell me more."}'])
    asyncio.run(_client(fake).generate_reply("hello", **_reply_kwargs()))
    assert fake.requests[0]["response_format"] == {"type": "json_object"}


def test_prose_and_fenced_replies_are_still_parsed_without_json_mode(no_sleep, events):
    answer = 'Sure! ```json\n{"category": "none", "counts_as_answer": true, "reply": "Tell me more."}\n```'
    fake = FakeClient([answer])
    result = asyncio.run(_client(fake).generate_reply("hello", **_reply_kwargs()))
    assert result["reply"] == "Tell me more."


# ─── Ответ из failed_generation и обычный текст вместо JSON ──────────────────

def _bad_request(code, failed_generation):
    """Исключение как от Groq: 400 с failed_generation в теле."""
    import httpx
    from openai import BadRequestError

    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(400, request=request)
    body = {"message": "failed", "type": "invalid_request_error", "code": code,
            "failed_generation": failed_generation}
    return BadRequestError("Error code: 400", response=response, body=body)


class RaisingClient(FakeClient):
    """Первые n вызовов падают исключением, дальше отвечает по очереди."""

    def __init__(self, errors, answers=("{}",)):
        super().__init__(answers)
        self.errors = list(errors)

    async def _create(self, **kwargs):
        if self.errors:
            self.requests.append(kwargs)
            raise self.errors.pop(0)
        return await super()._create(**kwargs)


TOOL_WRAPPED = (
    '{"name": "assistant", "arguments": {"category": "none", "mistake": "", "corrected": "", '
    '"counts_as_answer": false, "reply": "Let us keep exploring the book. What scene stuck with you?"}}'
)


def test_reply_is_taken_from_failed_generation_of_a_tool_call(no_sleep, events):
    fake = RaisingClient([_bad_request("tool_use_failed", TOOL_WRAPPED)])
    result = asyncio.run(_client(fake).generate_reply("hello", **_reply_kwargs()))
    assert result["reply"].startswith("Let us keep exploring")
    assert result["counts_as_answer"] is False
    assert len(fake.requests) == 1  # без лишних повторов
    assert ("generate_reply", "failed_generation_used") in events


def test_empty_failed_generation_is_a_normal_api_error(no_sleep, events):
    fake = RaisingClient([_bad_request("json_validate_failed", "")] * 20)
    assert asyncio.run(_client(fake).generate_reply("hello", **_reply_kwargs())) is None
    assert ("generate_reply", "error") in events


def test_plain_text_answer_is_used_as_the_reply(no_sleep, events):
    fake = FakeClient(["I hear you have had a long journey. Tell me what you noticed first."])
    result = asyncio.run(_client(fake).generate_reply("hello", **_reply_kwargs()))
    assert result["reply"] == "I hear you have had a long journey. Tell me what you noticed first."
    assert result["counts_as_answer"] is True and result["category"] == "none"
    assert ("generate_reply", "prose_fallback") in events


def test_broken_json_is_not_shown_to_the_user_as_a_reply(no_sleep, events):
    fake = FakeClient(['{"reply": "cut off here', "{broken"])
    assert asyncio.run(_client(fake).generate_reply("hello", **_reply_kwargs())) is None


def test_summary_never_falls_back_to_plain_text(no_sleep, events):
    fake = FakeClient(["Here is a nice summary in plain words."])
    assert asyncio.run(_client(fake).generate_session_summary(**_summary_kwargs())) is None


def test_unwrap_tool_call_variants():
    from src.groq_client import failed_generation_from, unwrap_tool_call

    assert unwrap_tool_call({"name": "x", "arguments": {"reply": "hi"}}) == {"reply": "hi"}
    assert unwrap_tool_call({"name": "x", "arguments": '{"reply": "hi"}'}) == {"reply": "hi"}
    assert unwrap_tool_call({"reply": "hi", "name": "Anna"}) == {"reply": "hi", "name": "Anna"}  # не обёртка
    assert failed_generation_from(_bad_request("x", "abc")) == "abc"
    assert failed_generation_from(_bad_request("x", "  ")) is None
