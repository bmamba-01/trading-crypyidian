from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_bot.domain.learning.evaluation import EvaluationTrade

AT = datetime(2026, 11, 1, tzinfo=UTC)
SCHEMA = "c" * 64
FEATURES = (("1h:atr", 1.0), ("4h:trend", -2.0))
CANONICAL = json.dumps(
    {
        "entry_decision_snapshot_id": "snapshot-entry",
        "entry_decision_at": "2026-11-01T00:00:00+00:00",
        "entry_feature_schema_hash": SCHEMA,
        "entry_features": [["1h:atr", 1.0], ["4h:trend", -2.0]],
    },
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
)
HASH = hashlib.sha256(CANONICAL.encode()).hexdigest()


def trade_with_entry(**changes):
    values = {
        "entry_decision_snapshot_id": "snapshot-entry",
        "entry_decision_at": AT,
        "entry_feature_schema_hash": SCHEMA,
        "entry_features": FEATURES,
        "entry_features_hash": HASH,
        "entry_symbol": "BTCUSDT",
    }
    values.update(changes)
    return EvaluationTrade(
        "trade-1",
        AT + timedelta(seconds=1),
        AT + timedelta(hours=1),
        Decimal("10"),
        Decimal("1"),
        Decimal("0"),
        Decimal("0.1"),
        Decimal("100"),
        **values,
    )


def test_entry_lineage_normalizes_timezone_order_and_numeric_values():
    trade = trade_with_entry(
        entry_decision_at=AT.astimezone(timezone(timedelta(hours=7))),
        entry_features=((" 4h:trend ", -2), ("1h:atr", 1)),
        entry_symbol=" btcusdt ",
    )
    assert trade.entry_features == FEATURES
    assert trade.entry_decision_at == AT
    assert trade.entry_decision_at.tzinfo is UTC
    assert trade.entry_features_hash == HASH
    assert trade.entry_symbol == "BTCUSDT"
    assert trade.net_pnl == Decimal("8.9")


def test_entry_lineage_hash_binds_snapshot_time_schema_and_values():
    from trading_bot.domain.learning.evaluation import calculate_entry_features_hash

    assert calculate_entry_features_hash("snapshot-entry", AT, SCHEMA, FEATURES) == HASH
    for values in (
        ("other", AT, SCHEMA, FEATURES),
        ("snapshot-entry", AT + timedelta(seconds=1), SCHEMA, FEATURES),
        ("snapshot-entry", AT, "d" * 64, FEATURES),
        ("snapshot-entry", AT, SCHEMA, (("1h:atr", 2.0), ("4h:trend", -2.0))),
    ):
        assert calculate_entry_features_hash(*values) != HASH


@pytest.mark.parametrize(
    "changes",
    [
        {"entry_decision_snapshot_id": None},
        {"entry_decision_snapshot_id": " "},
        {"entry_decision_at": None},
        {"entry_decision_at": AT.replace(tzinfo=None)},
        {"entry_feature_schema_hash": None},
        {"entry_feature_schema_hash": "C" * 64},
        {"entry_features": ()},
        {"entry_features": (("1h:atr", 1), (" 1h:atr ", 2))},
        {"entry_features": ((" ", 1),)},
        {"entry_features": (("1h:atr", float("nan")),)},
        {"entry_features": (("1h:atr", float("inf")),)},
        {"entry_features": (("1h:atr", True),)},
        {"entry_features_hash": None},
        {"entry_features_hash": "A" * 64},
        {"entry_features_hash": "0" * 64},
        {"entry_symbol": None},
        {"entry_symbol": ""},
        {"entry_symbol": "BTC/USDT"},
        {"entry_symbol": 1},
    ],
)
def test_entry_lineage_rejects_incomplete_malformed_or_conflicting_metadata(changes):
    with pytest.raises(ValueError, match="entry|Entry|Khóa|Giá trị"):
        trade_with_entry(**changes)


def test_legacy_trade_has_no_learning_provenance():
    legacy = replace(
        trade_with_entry(),
        entry_decision_snapshot_id=None,
        entry_decision_at=None,
        entry_feature_schema_hash=None,
        entry_features=(),
        entry_features_hash=None,
        entry_symbol=None,
    )
    assert not legacy.has_entry_provenance
    assert trade_with_entry().has_entry_provenance


def test_entry_opportunity_linkage_is_paired_and_never_uses_exit_metadata():
    trade = trade_with_entry(
        entry_opportunity_event_id="entry-event",
        entry_candidate_id="a" * 64,
        entry_vector_hash="b" * 64,
    )
    assert trade.entry_opportunity_event_id == "entry-event"
    assert trade.entry_candidate_id == "a" * 64
    assert trade.net_pnl == trade_with_entry().net_pnl
    for changes in (
        {"entry_opportunity_event_id": None},
        {"entry_candidate_id": None},
        {"entry_opportunity_event_id": " "},
        {"entry_candidate_id": "not-a-hash"},
    ):
        with pytest.raises(ValueError, match="opportunity|candidate"):
            replace(trade, **changes)
    with pytest.raises(ValueError, match="provenance|Entry"):
        replace(
            trade,
            entry_decision_snapshot_id=None,
            entry_decision_at=None,
            entry_feature_schema_hash=None,
            entry_features=(),
            entry_features_hash=None,
            entry_symbol=None,
        )


def test_entry_opportunity_vector_hash_is_part_of_the_immutable_entry_link():
    trade = trade_with_entry(
        entry_opportunity_event_id="event", entry_candidate_id="a" * 64, entry_vector_hash="b" * 64
    )
    assert trade.entry_vector_hash == "b" * 64
    with pytest.raises(ValueError, match="vector|opportunity"):
        replace(trade, entry_vector_hash=None)
