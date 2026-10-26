"""MB05.1: snapshot activity account, classifier thuần và adapter PAPER/giả lập/venue."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.ports.account_activity import (
    AccountActivityError,
    AccountActivityPort,
    AccountActivitySnapshot,
    ActivityClassification,
    ActivityVerdict,
    KnownAccountState,
    ObservedOrder,
    ObservedPosition,
    classify_account_activity,
)

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
WORKSPACE = uuid4()
ACCOUNT = uuid4()
MAX_AGE = timedelta(seconds=60)


def _order(key: str = "k1", **overrides: object) -> ObservedOrder:
    values: dict[str, object] = {
        "client_order_id": f"paper:{key}",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "quantity": Decimal("0.5"),
        "status": "ACCEPTED",
    }
    values.update(overrides)
    return ObservedOrder(**values)  # type: ignore[arg-type]


def _position(**overrides: object) -> ObservedPosition:
    values: dict[str, object] = {"symbol": "BTCUSDT", "side": "LONG", "quantity": Decimal("0.5")}
    values.update(overrides)
    return ObservedPosition(**values)  # type: ignore[arg-type]


def _snapshot(
    *,
    orders: tuple[ObservedOrder, ...] = (),
    positions: tuple[ObservedPosition, ...] = (),
    **overrides: object,
) -> AccountActivitySnapshot:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "account_id": ACCOUNT,
        "source": "SCRIPTED",
        "cursor": "c-1",
        "observed_at": NOW,
        "complete": True,
        "orders": orders,
        "positions": positions,
    }
    values.update(overrides)
    return AccountActivitySnapshot(**values)  # type: ignore[arg-type]


KNOWN = KnownAccountState(
    workspace_id=WORKSPACE,
    account_id=ACCOUNT,
    known_client_order_ids=frozenset({"paper:k1"}),
    open_positions=(_position(),),
)


def _classify(snapshot: AccountActivitySnapshot | None) -> ActivityClassification:
    return classify_account_activity(snapshot, KNOWN, now=NOW, max_snapshot_age=MAX_AGE)


def test_snapshot_is_immutable_utc_and_hash_is_order_independent() -> None:
    first = _snapshot(orders=(_order("a"), _order("b")), positions=(_position(),))
    second = _snapshot(orders=(_order("b"), _order("a")), positions=(_position(),))

    assert first.snapshot_hash == second.snapshot_hash
    assert len(first.snapshot_hash) == 64
    assert first.orders == second.orders
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.cursor = "other"  # type: ignore[misc]
    with pytest.raises(AccountActivityError) as error:
        _snapshot(observed_at=NOW.replace(tzinfo=None))
    assert error.value.reason_code == "ACTIVITY_TIME_NOT_UTC"
    with pytest.raises(AccountActivityError):
        _snapshot(source="BINANCE_LIVE")


def test_observed_values_reject_float_and_non_positive_quantity() -> None:
    with pytest.raises(AccountActivityError):
        _order(quantity=0.5)
    with pytest.raises(AccountActivityError):
        _position(quantity=Decimal("0"))
    with pytest.raises(AccountActivityError):
        _order(client_order_id=" ")


def test_known_orders_and_positions_pass() -> None:
    result = _classify(_snapshot(orders=(_order("k1"),), positions=(_position(),)))

    assert result.verdict is ActivityVerdict.CLEAN
    assert result.reason_code is None
    assert result.foreign_orders == ()
    assert result.foreign_positions == ()
    assert result.fingerprint is None


@pytest.mark.parametrize(
    ("orders", "positions"),
    [
        ((_order("manual"),), (_position(),)),
        ((_order("k1"),), (_position(), _position(symbol="ETHUSDT"))),
        ((_order("k1"),), (_position(quantity=Decimal("0.7")),)),
        ((_order("k1"),), (_position(side="SHORT"),)),
        ((_order("k1"),), (_position(symbol="ETHUSDT"),)),
    ],
    ids=["foreign-order", "foreign-position", "quantity-diff", "side-diff", "symbol-diff"],
)
def test_foreign_state_is_external_with_stable_fingerprint(
    orders: tuple[ObservedOrder, ...], positions: tuple[ObservedPosition, ...]
) -> None:
    first = _classify(_snapshot(orders=orders, positions=positions))
    again = _classify(
        _snapshot(orders=tuple(reversed(orders)), positions=tuple(reversed(positions)))
    )

    assert first.verdict is ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED
    assert first.reason_code == "EXTERNAL_ACTIVITY_UNRECONCILED"
    assert first.fingerprint is not None
    assert len(first.fingerprint) == 64
    assert first.fingerprint == again.fingerprint
    assert first.foreign_orders or first.foreign_positions


def test_paper_prefixed_order_from_other_intent_is_still_foreign() -> None:
    result = _classify(_snapshot(orders=(_order("not-ours"),), positions=(_position(),)))

    assert result.verdict is ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED
    assert [order.client_order_id for order in result.foreign_orders] == ["paper:not-ours"]


def test_missing_incomplete_stale_future_or_wrong_scope_snapshot_is_unverified() -> None:
    cases = (
        (None, "ACTIVITY_SNAPSHOT_MISSING"),
        (_snapshot(complete=False), "ACTIVITY_SNAPSHOT_INCOMPLETE"),
        (_snapshot(observed_at=NOW - MAX_AGE - timedelta(seconds=1)), "ACTIVITY_SNAPSHOT_STALE"),
        (_snapshot(observed_at=NOW + timedelta(seconds=1)), "ACTIVITY_SNAPSHOT_FUTURE"),
        (_snapshot(account_id=uuid4()), "ACTIVITY_SCOPE_MISMATCH"),
        (_snapshot(workspace_id=uuid4()), "ACTIVITY_SCOPE_MISMATCH"),
    )
    for snapshot, code in cases:
        result = _classify(snapshot)
        assert result.verdict is ActivityVerdict.ACCOUNT_ACTIVITY_UNVERIFIED, code
        assert result.reason_code == code
        assert result.fingerprint is None


def test_classifier_exposes_no_adopt_or_close_action() -> None:
    public = {name for name in dir(ActivityClassification) if not name.startswith("_")}

    assert not {name for name in public if "adopt" in name or "close" in name or "cancel" in name}
    fields = {field.name for field in dataclasses.fields(ActivityClassification)}
    assert fields == {
        "verdict",
        "reason_code",
        "foreign_orders",
        "foreign_positions",
        "fingerprint",
        "snapshot_hash",
    }


def test_scripted_adapter_returns_scripted_snapshot_and_errors() -> None:
    from trading_bot.adapters.paper.account_activity import ScriptedAccountActivityAdapter

    snapshot = _snapshot(orders=(_order("manual"),))
    adapter = ScriptedAccountActivityAdapter({ACCOUNT: snapshot})

    assert isinstance(adapter, AccountActivityPort)
    assert asyncio.run(adapter.inspect(ACCOUNT, None)) is snapshot
    with pytest.raises(AccountActivityError) as error:
        asyncio.run(adapter.inspect(uuid4(), None))
    assert error.value.reason_code == "ACTIVITY_ADAPTER_UNAVAILABLE"
    adapter.fail_with(ACCOUNT, "ACTIVITY_ADAPTER_UNAVAILABLE")
    with pytest.raises(AccountActivityError):
        asyncio.run(adapter.inspect(ACCOUNT, "c-1"))


def test_venue_adapter_factory_is_disabled_until_wp03() -> None:
    from trading_bot.adapters.paper.account_activity import venue_account_activity_adapter

    with pytest.raises(AccountActivityError) as error:
        venue_account_activity_adapter()
    assert error.value.reason_code == "VENUE_ACTIVITY_ADAPTER_DISABLED"


def test_paper_ledger_adapter_derives_snapshot_from_bot_gateway_and_is_clean() -> None:
    from trading_bot.adapters.paper.account_activity import PaperLedgerAccountActivityAdapter

    class _Gateway:
        def open_orders(self) -> tuple[object, ...]:
            return (
                dataclasses.make_dataclass(
                    "O", ["intent_id", "symbol", "direction", "requested_quantity", "status"]
                )("paper:k1", "BTCUSDT", "long", Decimal("0.5"), "ACCEPTED"),
            )

        def open_positions(self) -> tuple[object, ...]:
            return (
                dataclasses.make_dataclass("P", ["symbol", "direction", "quantity"])(
                    "BTCUSDT", "long", Decimal("0.5")
                ),
            )

    adapter = PaperLedgerAccountActivityAdapter(
        workspace_id=WORKSPACE, gateways={ACCOUNT: _Gateway()}, clock=lambda: NOW
    )
    snapshot = asyncio.run(adapter.inspect(ACCOUNT, None))
    known = adapter.known_state(ACCOUNT)

    assert snapshot.source == "PAPER_LEDGER"
    assert snapshot.complete is True
    assert snapshot.observed_at == NOW
    assert classify_account_activity(
        snapshot, known, now=NOW, max_snapshot_age=MAX_AGE
    ).verdict is (ActivityVerdict.CLEAN)
    with pytest.raises(AccountActivityError):
        asyncio.run(adapter.inspect(uuid4(), None))


def test_real_gateway_exposes_read_only_open_orders_and_positions() -> None:
    from trading_bot.adapters.paper.account_activity import PaperLedgerAccountActivityAdapter
    from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperExecutionPolicy

    gateway = PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="paper-fill-v1",
            latency=timedelta(0),
            spread_rate=Decimal("0.001"),
            slippage_rate=Decimal("0.002"),
            fee_rate=Decimal("0.001"),
        )
    )
    assert gateway.open_orders() == ()
    assert gateway.open_positions() == ()
    adapter = PaperLedgerAccountActivityAdapter(
        workspace_id=WORKSPACE, gateways={ACCOUNT: gateway}, clock=lambda: NOW
    )
    snapshot = asyncio.run(adapter.inspect(ACCOUNT, "rev-1"))
    assert snapshot.orders == ()
    assert snapshot.positions == ()
    assert snapshot.cursor == "rev-1"


def test_app_wiring_cannot_import_or_select_scripted_adapter() -> None:
    import trading_bot.app as app_module

    source = inspect.getsource(app_module)
    assert "ScriptedAccountActivityAdapter" not in source
    assert "account_activity" not in source or "Scripted" not in source
