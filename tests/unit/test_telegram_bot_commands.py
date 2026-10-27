"""MB07a.3: lệnh Telegram điều khiển bot (SEC-38, SECT-42/43; ruling 35, 36).

Handler gọi đúng `BotOperationsService` thật (cùng service với Admin API); DB là fake.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest
from tests.unit.bot_operations_fakes import (
    BOT_A,
    BOT_B,
    FOREIGN_BOT,
    build_operations,
    member,
    seeded_store,
)

from trading_bot.telegram.bot_commands import (
    BotCommand,
    BotCommandHandler,
    BotCommandParseError,
    parse_bot_command,
    telegram_principal,
)

ROOT = Path(__file__).parents[2]


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


# --- parser ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "command", "ref"),
    [
        ("/bots", BotCommand.LIST, None),
        ("/bots@CrypidianBot", BotCommand.LIST, None),
        (f"/bot status {BOT_A}", BotCommand.STATUS, str(BOT_A)),
        ("/bot status", BotCommand.STATUS, None),
        (f"/bot pause {BOT_A}", BotCommand.PAUSE, str(BOT_A)),
        (f"/bot@CrypidianBot   PAUSE   {BOT_A}  ", BotCommand.PAUSE, str(BOT_A)),
        ("/bot pause", BotCommand.PAUSE, None),
        (f"/bot resume {BOT_A}", BotCommand.RESUME, str(BOT_A)),
        ("/bot pause not-a-uuid", BotCommand.PAUSE, "not-a-uuid"),
    ],
)
def test_parse_supported_commands(text: str, command: BotCommand, ref: str | None) -> None:
    parsed = parse_bot_command(text)

    assert (parsed.command, parsed.bot_ref) == (command, ref)


@pytest.mark.parametrize(
    "text",
    [
        "/flatten",
        "/flatten_all",
        f"/bot flatten {BOT_A}",
        "/kill",
        "/bot kill",
        "/resume workspace",
        "/workspace resume",
        "/release",
        "/publish",
        f"/bot disable {BOT_A}",
        f"/bot publish {BOT_A}",
        "/start",
    ],
)
def test_high_risk_and_unknown_commands_are_not_allowed(text: str) -> None:
    with pytest.raises(BotCommandParseError) as error:
        parse_bot_command(text)

    assert error.value.code == "TELEGRAM_COMMAND_NOT_ALLOWED"


@pytest.mark.parametrize(
    "text", ["", "hello", "/bot", f"/bot pause {BOT_A} {BOT_B}", "/bots extra", "/" + "x" * 600]
)
def test_malformed_commands_are_invalid(text: str) -> None:
    with pytest.raises(BotCommandParseError) as error:
        parse_bot_command(text)

    assert error.value.code in {"TELEGRAM_COMMAND_INVALID", "TELEGRAM_COMMAND_NOT_ALLOWED"}
    if text in {"", "hello", "/bot", "/bots extra"} or text.startswith("/bot pause"):
        assert error.value.code == "TELEGRAM_COMMAND_INVALID"


# --- handler --------------------------------------------------------------------------------
def _handler(store: Any) -> BotCommandHandler:
    return BotCommandHandler(build_operations(store))


def _telegram(store: Any, kind: str) -> Any:
    web = member(store, kind)
    return telegram_principal(
        user_id=web.user_id,
        membership_id=web.membership_id,
        workspace_id=web.workspace_id,
        roles=web.roles,
    )


def test_telegram_principal_never_carries_recent_mfa() -> None:
    store = seeded_store()
    actor = _telegram(store, "owner")

    assert actor.mfa_verified_at.year == 1


def test_list_shows_only_bots_the_member_can_read() -> None:
    store = seeded_store()
    result = _run(
        _handler(store).handle_bot_command(_telegram(store, "operator"), BotCommand.LIST, None)
    )

    assert result.ok is True
    assert result.bot_ids == (str(BOT_A),)
    assert str(BOT_B) not in result.message


def test_pause_without_target_with_two_bots_is_ambiguous_and_lists_ids() -> None:
    store = seeded_store()
    result = _run(
        _handler(store).handle_bot_command(_telegram(store, "owner"), BotCommand.PAUSE, None)
    )

    assert (result.ok, result.code) == (False, "BOT_TARGET_AMBIGUOUS")
    assert result.bot_ids == (str(BOT_A), str(BOT_B))
    assert str(BOT_A) in result.message
    assert str(BOT_B) in result.message
    assert store.bot_rows == {}
    assert store.events == []


def test_pause_without_target_with_single_bot_still_requires_explicit_id() -> None:
    store = seeded_store()
    result = _run(
        _handler(store).handle_bot_command(_telegram(store, "operator"), BotCommand.PAUSE, None)
    )

    assert (result.ok, result.code, result.bot_ids) == (False, "BOT_TARGET_REQUIRED", (str(BOT_A),))
    assert store.bot_rows == {}


def test_status_without_target_resolves_only_a_single_visible_bot() -> None:
    store = seeded_store()
    handler = _handler(store)

    single = _run(handler.handle_bot_command(_telegram(store, "operator"), BotCommand.STATUS, None))
    many = _run(handler.handle_bot_command(_telegram(store, "owner"), BotCommand.STATUS, None))

    assert (single.ok, single.target_bot_id) == (True, BOT_A)
    assert (many.ok, many.code) == (False, "BOT_TARGET_AMBIGUOUS")


@pytest.mark.parametrize("ref", [str(FOREIGN_BOT), "not-a-uuid", str(BOT_A)[:8]])
def test_unknown_or_malformed_id_never_falls_back(ref: str) -> None:
    store = seeded_store()
    result = _run(
        _handler(store).handle_bot_command(_telegram(store, "operator"), BotCommand.PAUSE, ref)
    )

    assert (result.ok, result.code) == (False, "BOT_NOT_FOUND")
    assert store.bot_rows == {}
    assert str(BOT_A) not in result.message


def test_operator_cannot_pause_ungranted_bot() -> None:
    store = seeded_store()
    result = _run(
        _handler(store).handle_bot_command(
            _telegram(store, "operator"), BotCommand.PAUSE, str(BOT_B)
        )
    )

    assert (result.ok, result.code) == (False, "BOT_NOT_FOUND")
    assert store.audits[-1]["action"] == "bot.runtime.control_denied"


def test_operator_pauses_granted_bot_through_shared_control_service() -> None:
    store = seeded_store()
    actor = _telegram(store, "operator")
    result = _run(_handler(store).handle_bot_command(actor, BotCommand.PAUSE, BOT_A))

    assert (result.ok, result.target_bot_id) == (True, BOT_A)
    assert store.bot_rows[BOT_A][-1].value == "PAUSED"
    assert store.audits[-1]["action"] == "bot.runtime.paused"
    assert store.audits[-1]["actor"] == actor.membership_id
    assert "Telegram" in store.audits[-1]["reason"]


def test_status_reports_entry_state() -> None:
    store = seeded_store()
    actor = _telegram(store, "operator")
    handler = _handler(store)
    _run(handler.handle_bot_command(actor, BotCommand.PAUSE, BOT_A))

    result = _run(handler.handle_bot_command(actor, BotCommand.STATUS, str(BOT_A)))

    assert result.ok is True
    assert "PAUSED" in result.message


def test_resume_via_telegram_is_rejected_for_missing_step_up() -> None:
    store = seeded_store()
    actor = _telegram(store, "owner")
    handler = _handler(store)
    _run(handler.handle_bot_command(actor, BotCommand.PAUSE, BOT_A))

    result = _run(handler.handle_bot_command(actor, BotCommand.RESUME, BOT_A))

    assert (result.ok, result.code) == (False, "RECENT_MFA_REQUIRED")
    assert "Admin Web" in result.message
    assert store.bot_rows[BOT_A][-1].value == "PAUSED"


def test_command_module_has_no_direct_database_access() -> None:
    source = (ROOT / "src/trading_bot/telegram/bot_commands.py").read_text("utf-8")
    imported = {
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert not any(name.startswith(("sqlalchemy", "trading_bot.adapters")) for name in imported)
    assert ".session" not in source
    assert "UnitOfWork" not in source
