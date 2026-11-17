"""Unit test TG-2: chống spam tin tín hiệu Telegram (gộp trùng, cooldown, trần tin/giờ)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_telegram_signal_delivery import _candidate

from trading_bot.adapters.database.repositories.telegram_signals import _payload_dedup_key
from trading_bot.application.telegram_signals import (
    SUPPRESSED_COOLDOWN,
    SUPPRESSED_HOURLY_CAP,
    RecentSignalDelivery,
    SignalThrottlePolicy,
    candidate_dedup_key,
    throttle_reason,
)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
POLICY = SignalThrottlePolicy(cooldown=timedelta(minutes=90), max_per_hour=3)
KEY = "BTCUSDT|LONG|H03|h03-donchian-v1"


def _recent(key: str, minutes_ago: float) -> RecentSignalDelivery:
    return RecentSignalDelivery(key, NOW - timedelta(minutes=minutes_ago))


def test_dedup_key_uses_symbol_direction_strategy_and_release() -> None:
    assert candidate_dedup_key(_candidate()) == KEY
    assert candidate_dedup_key(_candidate(direction="SHORT")) != KEY


def test_first_signal_is_allowed() -> None:
    assert throttle_reason(_candidate(), [], now=NOW, policy=POLICY) is None


def test_same_signal_within_cooldown_is_suppressed_and_allowed_after() -> None:
    assert throttle_reason(_candidate(), [_recent(KEY, 89)], now=NOW, policy=POLICY) == (
        SUPPRESSED_COOLDOWN
    )
    assert throttle_reason(_candidate(), [_recent(KEY, 90)], now=NOW, policy=POLICY) is None


def test_opposite_direction_or_other_symbol_is_not_a_duplicate() -> None:
    history = [_recent(KEY, 5)]
    assert throttle_reason(_candidate(direction="SHORT"), history, now=NOW, policy=POLICY) is None
    assert throttle_reason(_candidate(symbol="ETHUSDT"), history, now=NOW, policy=POLICY) is None


def test_hourly_cap_counts_only_last_sixty_minutes() -> None:
    others = [_recent(f"X{i}USDT|LONG|H03|h03-donchian-v1", 10 + i) for i in range(3)]
    assert throttle_reason(_candidate(), others, now=NOW, policy=POLICY) == SUPPRESSED_HOURLY_CAP
    older = [_recent(f"X{i}USDT|LONG|H03|h03-donchian-v1", 61 + i) for i in range(3)]
    assert throttle_reason(_candidate(), older, now=NOW, policy=POLICY) is None


def test_duplicate_reason_wins_over_hourly_cap() -> None:
    history = [_recent(KEY, 1), *[_recent(f"Y{i}|LONG|H|r", 2) for i in range(5)]]
    assert throttle_reason(_candidate(), history, now=NOW, policy=POLICY) == SUPPRESSED_COOLDOWN


def test_zero_cooldown_disables_dedup_but_keeps_cap() -> None:
    policy = SignalThrottlePolicy(cooldown=timedelta(0), max_per_hour=1)
    assert throttle_reason(_candidate(), [_recent(KEY, 1)], now=NOW, policy=policy) == (
        SUPPRESSED_HOURLY_CAP
    )
    assert policy.lookback == timedelta(hours=1)


@pytest.mark.parametrize(
    "kwargs",
    [{"cooldown": timedelta(minutes=-1)}, {"cooldown": timedelta(hours=25)}, {"max_per_hour": 0}],
)
def test_policy_rejects_out_of_range_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="phải trong"):
        SignalThrottlePolicy(**kwargs)  # type: ignore[arg-type]


def test_payload_dedup_key_matches_candidate_and_ignores_unknown_shapes() -> None:
    payload = {
        "candidate": {
            "symbol": "BTCUSDT",
            "direction": "long",
            "strategy_id": "H03",
            "strategy_release_id": "h03-donchian-v1",
        }
    }
    assert _payload_dedup_key(payload) == KEY
    assert _payload_dedup_key({"candidate": None}) is None
    assert _payload_dedup_key({"candidate": {"symbol": "BTCUSDT"}}) is None


def test_settings_defaults_and_migration_allow_suppressed_status() -> None:
    from trading_bot.config.settings import AppSettings

    fields = AppSettings.model_fields
    assert fields["signal_worker_cooldown_minutes"].default == 90
    assert fields["signal_worker_max_messages_per_hour"].default == 6
    migration = (
        Path(__file__).parents[2]
        / "migrations/versions/c3e5a7b9d1f4_tg2_signal_delivery_suppressed.py"
    ).read_text(encoding="utf-8")
    assert "'SUPPRESSED'" in migration
    assert 'down_revision: str | None = "b2d4f6a8c0e3"' in migration
