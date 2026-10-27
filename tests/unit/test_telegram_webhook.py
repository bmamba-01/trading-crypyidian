"""MB07a.3: webhook Telegram — secret hằng thời gian, dedup `update_id`, binding, chặn update
giả mạo, throttle và không lộ bí mật (SEC-36/37/38, SECT-38..43; ruling 37–39, 41).

Không có gọi mạng: trả lời đi qua `RecordingTelegramSender`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from tests.unit.bot_operations_fakes import (
    BOT_A,
    OTHER_WORKSPACE,
    WORKSPACE,
    Store,
    build_operations,
    member,
    seeded_store,
)

from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.app import create_app
from trading_bot.config.settings import AppSettings
from trading_bot.telegram import webhook as webhook_module
from trading_bot.telegram.bot_commands import BotCommandHandler
from trading_bot.telegram.webhook import (
    SECRET_HEADER,
    TelegramBindingIdentity,
    TelegramCommandThrottle,
    TelegramWebhookService,
    load_webhook_secret,
)

SECRET = "tg-webhook-secret_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
OPERATOR_TG = 5550001
OWNER_TG = 5550002
W2_TG = 5550003
UNBOUND_TG = 5550004


class FakeTelegramStore:
    """Receipt/binding/audit giả; `calls` chứng minh request bị từ chối không chạm DB."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.receipts: set[tuple[UUID, int]] = set()
        self.bindings: dict[tuple[UUID, str], TelegramBindingIdentity] = {}
        self.audits: list[dict[str, Any]] = []

    async def claim_update(self, workspace_id: UUID, update_id: int) -> bool:
        self.calls.append("claim")
        key = (workspace_id, update_id)
        if key in self.receipts:
            return False
        self.receipts.add(key)
        return True

    async def resolve_binding(
        self, workspace_id: UUID, telegram_user_id: str
    ) -> TelegramBindingIdentity | None:
        self.calls.append("binding")
        return self.bindings.get((workspace_id, telegram_user_id))

    async def record_audit(self, workspace_id: UUID, **values: Any) -> None:
        self.calls.append("audit")
        self.audits.append(values | {"workspace_id": workspace_id})


def _bind(store: Store, telegram: FakeTelegramStore, kind: str, tg_user: int, ws: UUID) -> None:
    web = member(store, kind)
    telegram.bindings[(ws, str(tg_user))] = TelegramBindingIdentity(
        user_id=web.user_id,
        membership_id=web.membership_id,
        workspace_id=ws,
        roles=web.roles,
    )


def _setup(
    *, configured: bool = True, throttle: TelegramCommandThrottle | None = None
) -> tuple[TestClient, Store, FakeTelegramStore, RecordingTelegramSender]:
    store = seeded_store()
    telegram = FakeTelegramStore()
    sender = RecordingTelegramSender()
    _bind(store, telegram, "operator", OPERATOR_TG, WORKSPACE)
    _bind(store, telegram, "owner", OWNER_TG, WORKSPACE)
    _bind(store, telegram, "owner", W2_TG, OTHER_WORKSPACE)
    app = create_app(AppSettings(app_env="test"))
    app.state.telegram_webhook = (
        TelegramWebhookService(
            workspace_id=WORKSPACE,
            secret=SECRET,
            store=telegram,
            handler=BotCommandHandler(build_operations(store)),
            sender=sender,
            throttle=throttle or TelegramCommandThrottle(),
        )
        if configured
        else None
    )
    return TestClient(app), store, telegram, sender


def _message(
    text: str, *, user: int = OPERATOR_TG, update_id: int = 1, **overrides: Any
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id,
        "date": 1_790_000_000,
        "from": {"id": user, "is_bot": False, "first_name": "QA"},
        "chat": {"id": user, "type": "private"},
        "text": text,
    }
    for key, value in overrides.items():
        if value is None:
            message.pop(key, None)
        else:
            message[key] = value
    return {"update_id": update_id, "message": message}


def _send(
    client: TestClient, update: Any, *, secret: str | None = SECRET, raw: bytes | None = None
) -> Any:
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers[SECRET_HEADER] = secret
    if raw is not None:
        return client.post("/api/v1/telegram/webhook", content=raw, headers=headers)
    return client.post("/api/v1/telegram/webhook", json=update, headers=headers)


# --- SECT-38: secret ------------------------------------------------------------------------
def test_webhook_not_configured_is_404_without_touching_anything() -> None:
    client, store, telegram, _ = _setup(configured=False)

    response = _send(client, _message("/bots"))

    assert response.status_code == 404
    assert telegram.calls == []
    assert store.events == []


@pytest.mark.parametrize(
    "secret",
    [None, "", "wrong", SECRET[:-1] + "X", SECRET + "X", SECRET.lower()],
    ids=["missing", "empty", "short", "same-length", "longer", "case"],
)
def test_missing_or_wrong_secret_is_401_and_never_touches_database(secret: str | None) -> None:
    client, store, telegram, sender = _setup()

    response = _send(client, _message(f"/bot pause {BOT_A}"), secret=secret)

    assert response.status_code == 401
    assert telegram.calls == []
    assert store.events == []
    assert sender.messages == []
    assert SECRET not in response.text


def test_secret_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    client, *_ = _setup()
    seen: list[tuple[bytes, bytes]] = []
    original = webhook_module.hmac.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        seen.append((left, right))
        return original(left, right)

    monkeypatch.setattr(webhook_module.hmac, "compare_digest", spy)

    _send(client, _message("/bots"), secret="wrong")

    assert seen
    assert all(isinstance(item, bytes) for pair in seen for item in pair)


def test_oversized_body_is_rejected_before_database() -> None:
    client, _, telegram, _ = _setup()
    raw = b'{"update_id": 1, "pad": "' + b"x" * (70 * 1024) + b'"}'

    response = _send(client, None, raw=raw)

    assert response.status_code == 413
    assert telegram.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"message": {}}',
        b'{"update_id": "7"}',
        b'{"update_id": true}',
        b'{"update_id": 0}',
    ],
)
def test_malformed_update_is_400_without_claim(raw: bytes) -> None:
    client, _, telegram, _ = _setup()

    response = _send(client, None, raw=raw)

    assert response.status_code == 400
    assert telegram.calls == []


# --- SECT-39: replay và edited_message ------------------------------------------------------
def test_duplicate_update_id_is_noop() -> None:
    client, store, telegram, sender = _setup()
    update = _message(f"/bot pause {BOT_A}", update_id=42)

    first = _send(client, update)
    second = _send(client, update)

    assert first.json() == {"ok": True, "status": "processed"}
    assert second.json() == {"ok": True, "status": "duplicate"}
    assert len(store.bot_rows[BOT_A]) == 1
    assert len(sender.messages) == 1
    assert [audit["action"] for audit in store.audits] == ["bot.runtime.paused"]


def test_edited_message_is_claimed_and_ignored() -> None:
    client, store, telegram, sender = _setup()
    update = {"update_id": 9, "edited_message": _message(f"/bot pause {BOT_A}")["message"]}

    response = _send(client, update)

    assert response.json() == {"ok": True, "status": "ignored"}
    assert (WORKSPACE, 9) in telegram.receipts
    assert store.bot_rows == {}
    assert sender.messages == []


def test_non_command_text_is_ignored() -> None:
    client, store, _, sender = _setup()

    response = _send(client, _message("xin chào"))

    assert response.json()["status"] == "ignored"
    assert sender.messages == []


# --- SECT-40: update giả mạo ----------------------------------------------------------------
FORGED = {
    "group": {"chat": {"id": -100123, "type": "group"}},
    "supergroup": {"chat": {"id": -100124, "type": "supergroup"}},
    "channel": {"chat": {"id": -100125, "type": "channel"}},
    "chat-mismatch": {"chat": {"id": OPERATOR_TG + 1, "type": "private"}},
    "from-bot": {"from": {"id": OPERATOR_TG, "is_bot": True, "first_name": "B"}},
    "sender-chat": {"sender_chat": {"id": -100126, "type": "supergroup"}},
    "via-bot": {"via_bot": {"id": 9, "is_bot": True, "first_name": "Inline"}},
    "forward": {"forward_origin": {"type": "user", "date": 1, "sender_user": {"id": 1}}},
    "guest-bot": {"guest_bot_caller_user": {"id": 2, "is_bot": False, "first_name": "G"}},
    "no-from": {"from": None},
}


@pytest.mark.parametrize("variant", FORGED.values(), ids=FORGED.keys())
def test_forged_or_non_private_updates_are_rejected_and_audited(variant: dict[str, Any]) -> None:
    client, store, telegram, sender = _setup()

    response = _send(client, _message(f"/bot pause {BOT_A}", **variant))

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "rejected"}
    assert store.bot_rows == {}
    assert sender.messages == []
    assert "binding" not in telegram.calls
    assert telegram.audits[-1]["action"] == "telegram.update.rejected"
    assert telegram.audits[-1]["reason"].startswith("TELEGRAM_")
    assert telegram.audits[-1]["actor_membership_id"] is None


# --- SECT-41 (phần binding): unbound, workspace khác -----------------------------------------
@pytest.mark.parametrize("user", [UNBOUND_TG, W2_TG], ids=["unbound", "bound-in-w2"])
def test_unbound_or_other_workspace_user_cannot_control(user: int) -> None:
    client, store, telegram, sender = _setup()

    response = _send(client, _message(f"/bot pause {BOT_A}", user=user))

    assert response.json() == {"ok": True, "status": "unbound"}
    assert store.bot_rows == {}
    assert telegram.audits[-1]["reason"] == "TELEGRAM_NOT_BOUND"
    assert "chưa được liên kết" in sender.messages[-1][1]


# --- lệnh qua binding -----------------------------------------------------------------------
def test_bound_operator_pauses_bot_a_and_command_is_audited() -> None:
    client, store, telegram, sender = _setup()

    response = _send(client, _message(f"/bot pause {BOT_A}", update_id=77))

    assert response.json() == {"ok": True, "status": "processed"}
    assert store.bot_rows[BOT_A][-1].value == "PAUSED"
    accepted = telegram.audits[-1]
    assert accepted["action"] == "telegram.command.accepted"
    assert accepted["target_id"] == BOT_A
    assert accepted["reason"] == "pause:OK"
    assert accepted["actor_membership_id"] is not None
    assert accepted["request_id"] == webhook_module.update_request_id(WORKSPACE, 77)
    assert sender.messages[-1][0] == OPERATOR_TG


@pytest.mark.parametrize("text", ["/flatten", "/resume workspace", "/kill", "/publish"])
def test_forbidden_telegram_commands_are_rejected_and_audited(text: str) -> None:
    client, store, telegram, sender = _setup()

    response = _send(client, _message(text, user=OWNER_TG))

    assert response.json()["status"] == "processed"
    assert store.events == []
    assert store.workspace_rows == []
    assert telegram.audits[-1]["action"] == "telegram.command.rejected"
    assert telegram.audits[-1]["reason"] == "TELEGRAM_COMMAND_NOT_ALLOWED"
    assert "không được phép" in sender.messages[-1][1]


def test_ambiguous_pause_from_owner_lists_ids_and_changes_nothing() -> None:
    client, store, telegram, sender = _setup()

    _send(client, _message("/bot pause", user=OWNER_TG))

    assert store.bot_rows == {}
    assert telegram.audits[-1]["reason"] == "pause:BOT_TARGET_AMBIGUOUS"
    assert str(BOT_A) in sender.messages[-1][1]


def test_commands_are_throttled_per_user() -> None:
    client, _, _, _ = _setup(throttle=TelegramCommandThrottle(limit=2, window_seconds=60))

    statuses = [
        _send(client, _message("/bots", update_id=index)).json()["status"] for index in (1, 2, 3)
    ]

    assert statuses == ["processed", "processed", "throttled"]


# --- SEC-35: không lộ secret ----------------------------------------------------------------
def test_secret_never_appears_in_responses_logs_audits_or_replies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    client, store, telegram, sender = _setup()

    responses = [
        _send(client, _message("/bots", update_id=1)),
        _send(client, _message(f"/bot pause {BOT_A}", update_id=2)),
        _send(client, _message("/bots", update_id=3), secret="bad"),
        _send(client, _message("/flatten", update_id=4)),
    ]

    dumped = (
        " ".join(response.text for response in responses)
        + caplog.text
        + repr(telegram.audits)
        + repr(store.audits)
        + repr(sender.messages)
    )
    assert SECRET not in dumped


def test_load_webhook_secret_validates_charset_and_length(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.write_text(SECRET + "\n", encoding="utf-8")
    assert load_webhook_secret(good) == SECRET

    for bad in ("short_secret", "x" * 31, "has space " + "x" * 40, "ü" * 40, "x" * 257):
        path = tmp_path / "bad"
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError, match="32–256") as error:
            load_webhook_secret(path)
        assert bad not in str(error.value)


def test_telegram_settings_only_for_admin_web(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text(SECRET, encoding="utf-8")

    settings = AppSettings(telegram_workspace_id=WORKSPACE, telegram_webhook_secret_file=secret)
    assert settings.telegram_workspace_id == WORKSPACE

    with pytest.raises(ValueError, match="cần cả"):
        AppSettings(telegram_workspace_id=WORKSPACE)  # thiếu secret
    with pytest.raises(ValueError, match="chỉ cấu hình cho process_role admin-web"):
        AppSettings(
            process_role="trading-runtime",
            runtime_workspace_id=WORKSPACE,
            telegram_workspace_id=WORKSPACE,
            telegram_webhook_secret_file=secret,
        )
