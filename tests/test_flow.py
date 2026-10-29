"""
Сценарные тесты сессии «десятки» на реальном Postgres. LLM, TTS и Telegram
подменены заглушками. Запуск:

    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/daily_ten_test pytest tests/test_flow.py

Тесты очищают таблицы, поэтому имя базы обязано содержать "test".
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pytest

TEST_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_URL:
    pytest.skip("TEST_DATABASE_URL is not set", allow_module_level=True)
assert "test" in urlparse(TEST_URL).path.lower(), "TEST_DATABASE_URL must point to a database with 'test' in its name"

from src import handlers, metrics, summary, background  # noqa: E402
from src import keyboards as kbm  # noqa: E402
from src.config import MAX_USER_MESSAGE_CHARS  # noqa: E402
from src.db import db  # noqa: E402
from src.groq_client import groq_client  # noqa: E402
from src.session import CLOSING, EARLY, NORMAL, OPENING, PREFINAL, minsk_date  # noqa: E402

U = 42
MIGRATIONS = sorted((Path(__file__).resolve().parent.parent / "migrations").glob("*.sql"))


class Sent:
    _n = 1000

    def __init__(self, text, kw, world):
        Sent._n += 1
        self.message_id = Sent._n
        self.text, self.kw, self.world = text, kw, world

    async def edit_reply_markup(self, **kw):
        self.world.marks.append(kw.get("reply_markup"))


class World:
    """Заглушки Telegram + LLM и журнал отправленного."""

    def __init__(self):
        self.out, self.marks, self.calls = [], [], []
        self.next_counts, self.fail_reply, self.fail_summary = [], False, False

    # Telegram: сообщение и бот
    def message(self):
        world = self

        class Msg:
            async def answer(self, text, **kw):
                world.out.append(("text", text, kw))
                return Sent(text, kw, world)

            async def answer_voice(self, f, **kw):
                world.out.append(("voice", kw.get("caption"), kw))
                return Sent("", kw, world)

        return Msg()

    def bot(self):
        world = self

        class Bot:
            async def send_message(self, chat, text, **kw):
                world.out.append(("bot_text", text, kw))
                return Sent(text, kw, world)

            async def send_voice(self, chat, f, **kw):
                world.out.append(("bot_voice", kw.get("caption"), kw))
                return Sent("", kw, world)

            async def edit_message_reply_markup(self, **kw):
                world.marks.append(kw.get("reply_markup"))

        return Bot()

    def texts(self):
        return [o[1] for o in self.out if o[0] in ("text", "bot_text")]


@asynccontextmanager
async def world(monkeypatch):
    w = World()

    async def fake_reply(text, **kw):
        w.calls.append({"text": text, **kw})
        if w.fail_reply:
            return None
        counts = w.next_counts.pop(0) if w.next_counts else True
        return {
            "category": "grammar" if text else "none",
            "mistake": "I has" if text else "",
            "corrected": "I have" if text else "",
            "counts_as_answer": counts if text else True,
            "reply": f"reply to {text}",
        }

    async def fake_summary(**kw):
        if w.fail_summary:
            return None
        return {
            "user_summary": {"voice_script": "word " * 50, "best_moment": "b",
                             "mistakes": [{"was": "I has", "correct": "I have", "why": "w"}],
                             "level_cefr": "B1", "focus": ["f"]},
            "teacher_summary": {"errors": [], "topics": ["t"], "interests": ["i"],
                                "level_estimate": "B1", "pick_up_next": ["p"]},
        }

    async def no_tts(*a, **k):
        return None

    monkeypatch.setattr(groq_client, "generate_reply", fake_reply)
    monkeypatch.setattr(groq_client, "generate_session_summary", fake_summary)
    monkeypatch.setattr(groq_client, "text_to_speech", no_tts)

    conn = await asyncpg.connect(TEST_URL)
    try:
        for path in MIGRATIONS:
            await conn.execute(path.read_text(encoding="utf-8"))
        await conn.execute("TRUNCATE users, generation_events CASCADE")
    finally:
        await conn.close()
    await db.connect()
    await db.get_or_create_user(U)
    await db.set_name(U, "Anna")
    await db.set_level(U, "beginner")
    summary.generating.clear()
    try:
        yield w
    finally:
        summary.generating.clear()
        await metrics.flush()
        await db.close()


async def say(w, text, bot=None):
    w.out.clear()
    await handlers._dispatch(w.message(), bot or w.bot(), U, text=text, as_voice=False)


def run(scenario, monkeypatch):
    async def main():
        async with world(monkeypatch) as w:
            await scenario(w)

    asyncio.run(main())


async def backdate(sql, *args):
    async with db.pool.acquire() as c:
        await c.execute(sql, *args)


# ─── Сценарии ────────────────────────────────────────────────────────────────

def test_full_session_ten_answers_eleven_replies(monkeypatch):
    async def scenario(w):
        await say(w, None)  # /ten: МС открывает сессию
        session = await db.get_active_session(U)
        assert session["pairs_done"] == 0
        assert w.out[0][1].endswith("●○○○○○○○○○")
        assert w.calls[-1]["mode"] == OPENING

        await say(w, None)  # /ten при активной сессии = статус
        assert "Reply 1 of 10" in w.texts()[-1]

        bars = []
        for i in range(1, 10):
            await say(w, f"answer {i} " + "w " * i)
            bars.append(w.texts()[-1].splitlines()[-1])
        assert bars[0] == "●●○○○○○○○○" and bars[-1] == "●" * 10
        assert [c["mode"] for c in w.calls[1:]] == [NORMAL] * 8 + [PREFINAL]

        await say(w, "last one")  # 10-й ответ: прощание без полоски + Summary
        texts = w.texts()
        assert w.calls[-1]["mode"] == CLOSING and w.calls[-1]["reply_no"] == 11
        assert texts[0] == "reply to last one"
        assert any("Session Summary" in t for t in texts)
        assert not summary.generating

        last = await db.get_last_session(U)
        assert (last["status"], last["completion_reason"], last["pairs_done"]) == ("completed", "finished", 10)
        assert json.loads(last["user_summary"])["level_cefr"] == "B1"
        assert last["teacher_summary"]["answer_style"]["avg_words"] > 0
        async with db.pool.acquire() as c:
            replies = await c.fetchval("select count(*) from messages where session_id=$1 and role='assistant'", last["id"])
            answers = await c.fetchval("select count(*) from messages where session_id=$1 and role='user' and counted", last["id"])
            errors = await c.fetchval("select count(*) from error_logs where session_id=$1", last["id"])
        assert (replies, answers, errors) == (11, 10, 10)

    run(scenario, monkeypatch)


def test_done_today_blocks_then_next_day_opens_with_memory(monkeypatch):
    async def scenario(w):
        await say(w, None)
        await backdate("update sessions set pairs_done = 9")
        await say(w, "tenth")
        await say(w, "hello again")
        assert "ten for today" in w.texts()[-1] and w.out[0][2].get("reply_markup") is not None

        await backdate("update sessions set completed_at = now() - interval '2 days'")
        await say(w, "hi")  # новые сутки: сообщение открывает сессию и ответом не считается
        new = await db.get_active_session(U)
        assert new and w.calls[-1]["mode"] == OPENING and len(w.calls[-1]["teacher_summaries"]) == 1
        async with db.pool.acquire() as c:
            assert await c.fetchval("select count(*) from messages where session_id=$1 and role='user'", new["id"]) == 0

    run(scenario, monkeypatch)


def test_two_non_answers_then_third_is_forced(monkeypatch):
    async def scenario(w):
        await say(w, None)
        w.next_counts[:] = [False, False, False]
        await say(w, "what does X mean?")
        await say(w, "and Y?")
        s = await db.get_active_session(U)
        assert (s["pairs_done"], s["non_answer_streak"]) == (0, 2)
        assert w.texts()[-1].splitlines()[-1] == "●○○○○○○○○○"
        await say(w, "and Z?")
        s = await db.get_active_session(U)
        assert (s["pairs_done"], s["non_answer_streak"]) == (1, 0)
        assert w.calls[-1]["force_answer"] is True

    run(scenario, monkeypatch)


def test_llm_failure_leaves_the_session_untouched(monkeypatch):
    async def scenario(w):
        await say(w, None)
        w.fail_reply = True
        await say(w, "x y z")
        assert (await db.get_active_session(U))["pairs_done"] == 0
        async with db.pool.acquire() as c:
            assert await c.fetchval("select count(*) from messages where text='x y z'") == 0

    run(scenario, monkeypatch)


def test_long_message_is_truncated_but_word_count_is_real(monkeypatch):
    async def scenario(w):
        await say(w, None)
        long_text = "word " * 1000  # 5000 символов, 1000 слов
        await say(w, long_text)
        async with db.pool.acquire() as c:
            row = await c.fetchrow("select text, word_count from messages where role='user'")
        assert len(row["text"]) <= MAX_USER_MESSAGE_CHARS and row["word_count"] == 1000
        assert len(w.calls[-1]["text"]) <= MAX_USER_MESSAGE_CHARS

    run(scenario, monkeypatch)


def test_timeout_is_silent_below_three_pairs_and_sends_summary_from_three(monkeypatch):
    async def scenario(w):
        bot = w.bot()
        await say(w, None)
        await say(w, "one")
        await backdate("update sessions set last_activity_at = now() - interval '49 hours' where status='active'")
        w.out.clear()
        assert await background.close_stale_sessions_once(bot) == 1
        last = await db.get_last_session(U)
        assert last["completion_reason"] == "timeout" and not w.out and last["user_summary"] is None

        s = await db.create_session(U, minsk_date())
        await backdate("update sessions set pairs_done=4, last_activity_at=now()-interval '49 hours' where id=$1", s["id"])
        w.out.clear()
        await background.close_stale_sessions_once(bot)
        assert any("Session Summary" in t for t in w.texts())
        assert (await db.get_session(s["id"]))["user_summary"]
        assert await background.close_stale_sessions_once(bot) == 0  # повторно не обрабатывается

    run(scenario, monkeypatch)


def test_summary_failure_is_retried_on_next_message(monkeypatch):
    async def scenario(w):
        await say(w, None)
        await backdate("update sessions set pairs_done = 9")
        w.fail_summary = True
        await say(w, "tenth")
        s = await db.get_last_session(U)
        assert s["status"] == "completed" and s["user_summary"] is None and not summary.generating
        assert "couldn't finish my notes" in w.texts()[-1]

        w.fail_summary = False
        await say(w, "anything")
        assert (await db.get_session(s["id"]))["user_summary"]

        summary.generating.add(U)
        await say(w, "hey")
        assert "gathering my notes" in w.texts()[-1]

    run(scenario, monkeypatch)


def test_early_finish(monkeypatch):
    async def scenario(w):
        await say(w, None)
        session = await db.get_active_session(U)

        await handlers._request_finish(w.message(), w.bot(), U, confirmed=True)  # меньше 3 пар
        assert (await db.get_active_session(U)) is not None
        await say(w, "a b")
        await say(w, "c d")
        w.out.clear()
        await handlers._request_finish(w.message(), w.bot(), U, confirmed=False)
        assert "at least" in w.texts()[0] and not kbm.has_finish_button(w.marks[-1])

        await say(w, "e f")  # третья пара: появляется кнопка
        assert kbm.has_finish_button(w.marks[-1])

        w.out.clear()
        await handlers._request_finish(w.message(), w.bot(), U, confirmed=False)  # только подтверждение
        assert "wrap up" in w.texts()[0] and (await db.get_active_session(U)) is not None

        calls_before = len(w.calls)
        w.out.clear()
        await handlers._request_finish(w.message(), w.bot(), U, confirmed=True)
        assert w.calls[calls_before]["mode"] == EARLY
        done = await db.get_session(session["id"])
        assert (done["status"], done["completion_reason"], done["pairs_done"]) == ("completed", "early", 3)
        assert done["user_summary"] and any("Session Summary" in t for t in w.texts())
        async with db.pool.acquire() as c:  # запрос на завершение ответом не считается и не пишется
            assert await c.fetchval("select count(*) from messages where text like '[The student%'") == 0

        await say(w, "hi again")
        assert "ten for today" in w.texts()[-1]  # early тоже блокирует до завтра

    run(scenario, monkeypatch)


def test_admin_queries_run(monkeypatch):
    async def scenario(w):
        await say(w, None)
        await say(w, "one two three")
        stats = await db.get_session_stats()
        assert stats["total"] == 1 and stats["active"] == 1
        rows = await db.get_avg_words_by_session_ordinal()
        assert rows and rows[0]["ordinal"] == 1 and rows[0]["avg_words"] == 3
        card = await db.get_user_card(U)
        assert card["session_total"] == 1
        await db.get_admin_stats()
        await db.get_error_category_breakdown()

    run(scenario, monkeypatch)


def test_generation_events_are_stored_and_aggregated(monkeypatch):
    async def scenario(w):
        await say(w, None)
        w.next_counts[:] = [False]
        await say(w, "what does X mean?")  # не-ответ пишется событием
        metrics.record("tts", "tts_ok", model="m")
        metrics.record("tts", "tts_fail", model="m", detail="limit")
        await metrics.flush()
        stats = {(r["label"], r["event"]): r["n"] for r in await db.get_generation_stats(24)}
        assert stats[("generate_reply", "non_answer")] == 1
        assert stats[("tts", "tts_ok")] == 1 and stats[("tts", "tts_fail")] == 1
        await backdate("update generation_events set created_at = now() - interval '40 days' where event = 'tts_ok'")
        assert await db.delete_old_generation_events(30) == 1

    run(scenario, monkeypatch)

