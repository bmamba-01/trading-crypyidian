from __future__ import annotations

import json
from typing import Any

import pytest
from scripts.telegram_local_relay import (
    SECRET_HEADER,
    Relay,
    RelayError,
    next_offset,
    require_loopback_url,
)

WEBHOOK = "http://127.0.0.1:8010/api/v1/telegram/webhook"


def _relay(responses: list[dict[str, Any]], statuses: list[int]) -> tuple[Relay, list[Any]]:
    calls: list[Any] = []

    def fetch(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        calls.append(("fetch", url.rsplit("/", 1)[-1], payload))
        return responses.pop(0)

    def forward(url: str, data: bytes, headers: dict[str, str]) -> int:
        calls.append(("forward", url, json.loads(data), headers))
        return statuses.pop(0)

    relay = Relay(
        token="123:abc",
        webhook_secret="s" * 40,
        webhook_url=WEBHOOK,
        fetch=fetch,
        forward=forward,
    )
    return relay, calls


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8010/api/v1/telegram/webhook",
        "https://example.com/api/v1/telegram/webhook",
        "http://127.0.0.1:8010/admin",
        "ftp://127.0.0.1/api/v1/telegram/webhook",
    ],
)
def test_relay_only_targets_the_local_admin_webhook(url: str) -> None:
    with pytest.raises(RelayError):
        require_loopback_url(url)
    assert require_loopback_url(WEBHOOK) == WEBHOOK


def test_poll_forwards_each_update_with_secret_and_advances_offset() -> None:
    updates = [{"update_id": 7, "message": {}}, {"update_id": 9, "message": {}}]
    relay, calls = _relay([{"ok": True, "result": updates}], [200, 200])

    offset, statuses = relay.poll_once(None)

    assert offset == 10
    assert statuses == [200, 200]
    forwarded = [call for call in calls if call[0] == "forward"]
    assert [call[2]["update_id"] for call in forwarded] == [7, 9]
    assert all(call[3][SECRET_HEADER] == "s" * 40 for call in forwarded)
    assert calls[0][2]["allowed_updates"] == ["message", "callback_query"]


def test_server_error_keeps_offset_so_telegram_redelivers() -> None:
    updates = [{"update_id": 7, "message": {}}, {"update_id": 9, "message": {}}]
    relay, _ = _relay([{"ok": True, "result": updates}], [503])

    offset, statuses = relay.poll_once(5)

    assert offset == 5
    assert statuses == [503]


def test_relay_refuses_to_run_beside_a_public_webhook() -> None:
    relay, _ = _relay([{"ok": True, "result": {"url": "https://example.com/hook"}}], [])

    with pytest.raises(RelayError, match="webhook"):
        relay.ensure_no_webhook()


def test_failed_get_updates_is_an_error_and_offset_helper_ignores_bad_ids() -> None:
    relay, _ = _relay([{"ok": False, "description": "Conflict"}], [])

    with pytest.raises(RelayError, match="Conflict"):
        relay.poll_once(None)
    assert next_offset([{"update_id": "x"}], 3) == 3
    assert next_offset([], None) is None
