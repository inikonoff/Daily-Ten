import asyncio

from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault, MenuButtonCommands

from src.bot_menu import ADMIN_COMMANDS, USER_COMMANDS, setup_bot_menu


class FakeBot:
    def __init__(self, fail_for_chat=None, fail_all=False, fail_menu_button=False):
        self.commands, self.calls = [], []
        self.fail_for_chat, self.fail_all = fail_for_chat, fail_all
        self.fail_menu_button = fail_menu_button

    async def set_my_commands(self, commands, scope=None):
        if self.fail_all:
            raise RuntimeError("telegram is down")
        if isinstance(scope, BotCommandScopeChat) and scope.chat_id == self.fail_for_chat:
            raise RuntimeError("chat not found")
        self.commands.append((scope, [c.command for c in commands]))

    async def set_chat_menu_button(self, menu_button=None, **kw):
        if self.fail_menu_button:
            raise RuntimeError("menu button failed")
        assert isinstance(menu_button, MenuButtonCommands)
        self.calls.append("menu_button")

    async def set_my_short_description(self, short_description=None, **kw):
        assert 0 < len(short_description) <= 120
        self.calls.append("short")

    async def set_my_description(self, description=None, **kw):
        assert 0 < len(description) <= 512
        self.calls.append("description")


def test_default_menu_has_no_admin_command_but_admins_get_it():
    bot = FakeBot()
    asyncio.run(setup_bot_menu(bot, [111, 222]))
    default = [c for scope, c in bot.commands if isinstance(scope, BotCommandScopeDefault)][0]
    assert "admin" not in default and {"ten", "summary", "finish", "start"} <= set(default)
    admin_scopes = {scope.chat_id: c for scope, c in bot.commands if isinstance(scope, BotCommandScopeChat)}
    assert set(admin_scopes) == {111, 222} and all("admin" in c for c in admin_scopes.values())
    assert bot.calls == ["menu_button", "short", "description"]


def test_unknown_admin_chat_does_not_break_the_rest():
    bot = FakeBot(fail_for_chat=111)
    asyncio.run(setup_bot_menu(bot, [111, 222]))
    assert {s.chat_id for s, _ in bot.commands if isinstance(s, BotCommandScopeChat)} == {222}


def test_telegram_failure_does_not_crash_startup():
    asyncio.run(setup_bot_menu(FakeBot(fail_all=True), [111]))  # не бросает


def test_command_descriptions_fit_telegram_limits():
    for command in ADMIN_COMMANDS:
        assert 1 <= len(command.description) <= 256 and command.command.islower()
    assert [c.command for c in ADMIN_COMMANDS[: len(USER_COMMANDS)]] == [c.command for c in USER_COMMANDS]


def test_failed_menu_button_does_not_skip_the_descriptions():
    bot = FakeBot(fail_menu_button=True)
    asyncio.run(setup_bot_menu(bot, [111]))
    assert "short" in bot.calls and "description" in bot.calls  # описание всё равно задано
    assert any(isinstance(scope, BotCommandScopeChat) for scope, _ in bot.commands)
