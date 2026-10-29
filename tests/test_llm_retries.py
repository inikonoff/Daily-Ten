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
    fake = FakeClient(["garbage"])
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
