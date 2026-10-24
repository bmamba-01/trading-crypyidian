"""Serialization canonical cho checkpoint PAPER phiên bản hóa."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_bot.domain.execution.paper import (
    OrderStatus,
    PaperExecutionPolicy,
    PaperFill,
    PaperOrderIntent,
    PaperPosition,
    ReduceOnlyReceipt,
)
from trading_bot.domain.risk.planner import (
    ExchangeRules,
    TradePlan,
    calculate_plan_hash,
)
from trading_bot.domain.strategy.recipes import ExitRecipe

PAPER_STATE_SCHEMA_VERSION = 2


class PaperSnapshotError(ValueError):
    """Snapshot PAPER sai schema, hash hoặc bất biến domain."""


@dataclass(frozen=True, slots=True)
class PaperExecutionSnapshot:
    """Payload bất biến/hash-bound để lưu trạng thái đầy đủ gateway PAPER."""

    schema_version: int
    policy_hash: str
    payload: str
    content_hash: str


def create_snapshot(
    *,
    policy: PaperExecutionPolicy,
    orders: dict[str, PaperOrderIntent],
    keys: dict[str, str],
    positions: dict[str, PaperPosition],
    plans: dict[str, TradePlan],
    close_keys: dict[str, ReduceOnlyReceipt],
    entries_paused: bool,
    market_event_hashes: dict[tuple[str, str, str], str],
    last_market_event: dict[tuple[str, str], tuple[datetime, str]],
) -> PaperExecutionSnapshot:
    """Chuẩn hóa toàn bộ state gateway thành JSON deterministic."""
    body = {
        "orders": [_order_to_json(order) for _, order in sorted(orders.items())],
        "keys": dict(sorted(keys.items())),
        "positions": [_position_to_json(position) for _, position in sorted(positions.items())],
        "plans": {key: _plan_to_json(value) for key, value in sorted(plans.items())},
        "close_keys": {key: _receipt_to_json(value) for key, value in sorted(close_keys.items())},
        "entries_paused": entries_paused,
        "market_event_hashes": [
            [scope, symbol, bar_key, fingerprint]
            for (scope, symbol, bar_key), fingerprint in sorted(market_event_hashes.items())
        ],
        "last_market_event": {
            f"{scope}\u0000{symbol}": [occurred_at.isoformat(), bar_key]
            for (scope, symbol), (occurred_at, bar_key) in sorted(last_market_event.items())
        },
    }
    _validate_body(body, policy)
    payload = _canonical_json(body)
    policy_hash = _policy_hash(policy)
    return PaperExecutionSnapshot(
        schema_version=PAPER_STATE_SCHEMA_VERSION,
        policy_hash=policy_hash,
        payload=payload,
        content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def restore_snapshot(
    policy: PaperExecutionPolicy, snapshot: PaperExecutionSnapshot
) -> tuple[
    dict[str, PaperOrderIntent],
    dict[str, str],
    dict[str, PaperPosition],
    dict[str, TradePlan],
    dict[str, ReduceOnlyReceipt],
    bool,
    dict[tuple[str, str, str], str],
    dict[tuple[str, str], tuple[datetime, str]],
]:
    """Giải mã snapshot sau khi kiểm tra schema, policy, hash và liên kết."""
    if snapshot.schema_version != PAPER_STATE_SCHEMA_VERSION:
        raise PaperSnapshotError("Paper snapshot schema version không được hỗ trợ.")
    if hashlib.sha256(snapshot.payload.encode("utf-8")).hexdigest() != snapshot.content_hash:
        raise PaperSnapshotError("Paper snapshot content hash không khớp.")
    try:
        body = json.loads(snapshot.payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise PaperSnapshotError("Paper snapshot payload không phải JSON hợp lệ.") from error
    if not isinstance(body, dict) or set(body) != {
        "orders",
        "keys",
        "positions",
        "plans",
        "close_keys",
        "entries_paused",
        "market_event_hashes",
        "last_market_event",
    }:
        raise PaperSnapshotError("Paper snapshot có cấu trúc trường không hợp lệ.")
    try:
        orders = {_order_from_json(row).intent_id: _order_from_json(row) for row in body["orders"]}
        keys = {str(key): str(value) for key, value in body["keys"].items()}
        positions = {
            _position_from_json(row).intent_id: _position_from_json(row)
            for row in body["positions"]
        }
        plans = {str(key): _plan_from_json(value) for key, value in body["plans"].items()}
        close_keys = {
            str(key): _receipt_from_json(value) for key, value in body["close_keys"].items()
        }
        paused = body["entries_paused"]
        if not isinstance(paused, bool):
            raise TypeError("entries_paused must be boolean")
        event_hashes = {
            (str(scope), str(symbol), str(bar_key)): str(fingerprint)
            for scope, symbol, bar_key, fingerprint in body["market_event_hashes"]
        }
        cursors = {
            _split_cursor_key(str(key)): (_datetime(value[0]), str(value[1]))
            for key, value in body["last_market_event"].items()
        }
        if len(event_hashes) != len(body["market_event_hashes"]):
            raise PaperSnapshotError("Snapshot có market event key trùng.")
        if any(
            (scope, symbol, key) not in event_hashes
            for (scope, symbol), (_, key) in cursors.items()
        ):
            raise PaperSnapshotError("Cursor market event không có event fingerprint.")
        active_policy_state = any(
            order.status
            in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN}
            for order in orders.values()
        ) or any(not position.is_closed for position in positions.values())
        if active_policy_state and snapshot.policy_hash != _policy_hash(policy):
            raise PaperSnapshotError("Paper snapshot fill policy hash không khớp state đang mở.")
        _validate_body(body, policy)
        _validate_restored(orders, keys, positions, plans, close_keys, policy)
    except PaperSnapshotError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise PaperSnapshotError("Paper snapshot chứa state domain không hợp lệ.") from error
    return orders, keys, positions, plans, close_keys, paused, event_hashes, cursors


def _validate_body(body: dict[str, Any], policy: PaperExecutionPolicy) -> None:
    del policy  # Policy lineage is checked by the snapshot envelope.
    if not isinstance(body["orders"], list) or not isinstance(body["positions"], list):
        raise PaperSnapshotError("Danh sách order/position trong snapshot không hợp lệ.")
    if not isinstance(body["keys"], dict) or not isinstance(body["plans"], dict):
        raise PaperSnapshotError("Bản đồ order/plan trong snapshot không hợp lệ.")
    if (
        not isinstance(body["close_keys"], dict)
        or not isinstance(body["entries_paused"], bool)
        or not isinstance(body["market_event_hashes"], list)
        or not isinstance(body["last_market_event"], dict)
    ):
        raise PaperSnapshotError("State close key hoặc pause trong snapshot không hợp lệ.")


def _validate_restored(
    orders: dict[str, PaperOrderIntent],
    keys: dict[str, str],
    positions: dict[str, PaperPosition],
    plans: dict[str, TradePlan],
    close_keys: dict[str, ReduceOnlyReceipt],
    policy: PaperExecutionPolicy,
) -> None:
    if len(keys) != len(orders):
        raise PaperSnapshotError("Idempotency key và order không cân bằng.")
    for intent_id, order in orders.items():
        if (
            order.intent_id != intent_id
            or keys.get(order.idempotency_key) != intent_id
            or intent_id not in plans
            or calculate_plan_hash(plans[intent_id]) != plans[intent_id].plan_hash
            or order.plan_hash != plans[intent_id].plan_hash
            or (
                order.status
                in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN}
                and order.fill_policy_version != policy.version_id
            )
            or sum((fill.quantity for fill in order.fills), Decimal(0)) != order.filled_quantity
            or order.remaining_quantity != order.requested_quantity - order.filled_quantity
            or order.requested_quantity != plans[intent_id].quantity
        ):
            raise PaperSnapshotError("Order/plan/fill lineage trong snapshot không nhất quán.")
    if set(plans) != set(orders) or not set(positions).issubset(orders):
        raise PaperSnapshotError("Plan hoặc position không gắn với order hiện hữu.")
    for intent_id, position in positions.items():
        if (
            position.intent_id != intent_id
            or position.position_id != intent_id
            or position.recipe != plans[intent_id].exit_recipe
            or position.contract_multiplier != plans[intent_id].exchange_rules.contract_multiplier
            or (
                not position.is_closed
                and orders[intent_id].fill_policy_version != policy.version_id
            )
            or position.quantity < 0
            or position.protective_stop_quantity != position.quantity
            or position.is_closed != (position.quantity == 0)
        ):
            raise PaperSnapshotError("Position/plan invariant trong snapshot không hợp lệ.")
    for key, receipt in close_keys.items():
        if key != receipt.idempotency_key or receipt.position.intent_id not in positions:
            raise PaperSnapshotError("Close receipt không thuộc position trong snapshot.")


def _policy_hash(policy: PaperExecutionPolicy) -> str:
    material = {
        "version_id": policy.version_id,
        "latency_microseconds": int(policy.latency.total_seconds() * 1_000_000),
        "spread_rate": str(policy.spread_rate),
        "slippage_rate": str(policy.slippage_rate),
        "fee_rate": str(policy.fee_rate),
        "max_fill_quantity_per_event": (
            str(policy.max_fill_quantity_per_event)
            if policy.max_fill_quantity_per_event is not None
            else None
        ),
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _split_cursor_key(value: str) -> tuple[str, str]:
    scope, separator, symbol = value.partition("\u0000")
    if not separator or not scope or not symbol:
        raise PaperSnapshotError("Cursor market event có khóa không hợp lệ.")
    return scope, symbol


def _fill_to_json(fill: PaperFill) -> dict[str, Any]:
    return {
        "fill_id": fill.fill_id,
        "fill_policy_version": fill.fill_policy_version,
        "bar_key": fill.bar_key,
        "quantity": str(fill.quantity),
        "reference_price": str(fill.reference_price),
        "execution_price": str(fill.execution_price),
        "fee_cost": str(fill.fee_cost),
        "slippage_cost": str(fill.slippage_cost),
        "filled_at": fill.filled_at.isoformat(),
    }


def _fill_from_json(row: dict[str, Any]) -> PaperFill:
    return PaperFill(
        str(row["fill_id"]),
        str(row["fill_policy_version"]),
        str(row["bar_key"]),
        Decimal(row["quantity"]),
        Decimal(row["reference_price"]),
        Decimal(row["execution_price"]),
        Decimal(row["fee_cost"]),
        Decimal(row["slippage_cost"]),
        _datetime(row["filled_at"]),
    )


def _order_to_json(order: PaperOrderIntent) -> dict[str, Any]:
    return {
        "intent_id": order.intent_id,
        "idempotency_key": order.idempotency_key,
        "plan_hash": order.plan_hash,
        "fill_policy_version": order.fill_policy_version,
        "symbol": order.symbol,
        "direction": order.direction,
        "requested_quantity": str(order.requested_quantity),
        "filled_quantity": str(order.filled_quantity),
        "remaining_quantity": str(order.remaining_quantity),
        "eligible_at": order.eligible_at.isoformat(),
        "expires_at": order.expires_at.isoformat(),
        "stop_price": str(order.stop_price),
        "target_price": str(order.target_price) if order.target_price is not None else None,
        "status": order.status.value,
        "fills": [_fill_to_json(fill) for fill in order.fills],
        "seen_bar_keys": list(order.seen_bar_keys),
    }


def _order_from_json(row: dict[str, Any]) -> PaperOrderIntent:
    return PaperOrderIntent(
        intent_id=str(row["intent_id"]),
        idempotency_key=str(row["idempotency_key"]),
        plan_hash=str(row["plan_hash"]),
        fill_policy_version=str(row["fill_policy_version"]),
        symbol=str(row["symbol"]),
        direction=str(row["direction"]),
        requested_quantity=Decimal(row["requested_quantity"]),
        filled_quantity=Decimal(row["filled_quantity"]),
        remaining_quantity=Decimal(row["remaining_quantity"]),
        eligible_at=_datetime(row["eligible_at"]),
        expires_at=_datetime(row["expires_at"]),
        stop_price=Decimal(row["stop_price"]),
        target_price=Decimal(row["target_price"]) if row["target_price"] is not None else None,
        status=OrderStatus(row["status"]),
        fills=tuple(_fill_from_json(item) for item in row["fills"]),
        seen_bar_keys=tuple(str(value) for value in row["seen_bar_keys"]),
    )


def _position_to_json(position: PaperPosition) -> dict[str, Any]:
    return {
        "position_id": position.position_id,
        "intent_id": position.intent_id,
        "symbol": position.symbol,
        "direction": position.direction,
        "quantity": str(position.quantity),
        "entry_reference_price": str(position.entry_reference_price),
        "entry_execution_price": str(position.entry_execution_price),
        "stop_price": str(position.stop_price),
        "target_price": str(position.target_price) if position.target_price is not None else None,
        "recipe": asdict(position.recipe),
        "contract_multiplier": str(position.contract_multiplier),
        "protective_stop_quantity": str(position.protective_stop_quantity),
        "entry_bar_key": position.entry_bar_key,
        "fill_policy_version": position.fill_policy_version,
        "bars_held": position.bars_held,
        "fee_cost": str(position.fee_cost),
        "slippage_cost": str(position.slippage_cost),
        "funding_cost": str(position.funding_cost),
        "funding_event_ids": list(position.funding_event_ids),
        "gross_realized_pnl": str(position.gross_realized_pnl),
        "is_closed": position.is_closed,
        "seen_bar_keys": list(position.seen_bar_keys),
        "exit_reason": position.exit_reason,
        "exit_reference_price": (
            str(position.exit_reference_price)
            if position.exit_reference_price is not None
            else None
        ),
    }


def _position_from_json(row: dict[str, Any]) -> PaperPosition:
    recipe = row["recipe"]
    return PaperPosition(
        position_id=str(row["position_id"]),
        intent_id=str(row["intent_id"]),
        symbol=str(row["symbol"]),
        direction=str(row["direction"]),
        quantity=Decimal(row["quantity"]),
        entry_reference_price=Decimal(row["entry_reference_price"]),
        entry_execution_price=Decimal(row["entry_execution_price"]),
        stop_price=Decimal(row["stop_price"]),
        target_price=Decimal(row["target_price"]) if row["target_price"] is not None else None,
        recipe=ExitRecipe(**recipe),
        contract_multiplier=Decimal(row["contract_multiplier"]),
        protective_stop_quantity=Decimal(row["protective_stop_quantity"]),
        entry_bar_key=str(row["entry_bar_key"]),
        fill_policy_version=str(row["fill_policy_version"]),
        bars_held=int(row["bars_held"]),
        fee_cost=Decimal(row["fee_cost"]),
        slippage_cost=Decimal(row["slippage_cost"]),
        funding_cost=Decimal(row["funding_cost"]),
        funding_event_ids=tuple(str(value) for value in row["funding_event_ids"]),
        gross_realized_pnl=Decimal(row["gross_realized_pnl"]),
        is_closed=bool(row["is_closed"]),
        seen_bar_keys=tuple(str(value) for value in row["seen_bar_keys"]),
        exit_reason=str(row["exit_reason"]) if row["exit_reason"] is not None else None,
        exit_reference_price=(
            Decimal(row["exit_reference_price"])
            if row["exit_reference_price"] is not None
            else None
        ),
    )


def _plan_to_json(plan: TradePlan) -> dict[str, Any]:
    value: dict[str, Any] = asdict(plan)
    for key in _PLAN_DECIMALS:
        value[key] = str(getattr(plan, key))
    value["target_price"] = str(plan.target_price) if plan.target_price is not None else None
    for key in ("expires_at", "source_signal_at"):
        value[key] = getattr(plan, key).isoformat()
    for key in ("evidence_feature_ids", "evidence_bar_keys", "macro_evidence_ids"):
        value[key] = list(getattr(plan, key))
    value["exchange_rules"] = {key: str(item) for key, item in asdict(plan.exchange_rules).items()}
    value["exit_recipe"] = asdict(plan.exit_recipe)
    return value


_PLAN_DECIMALS = (
    "entry_reference_price",
    "entry_price",
    "stop_price",
    "quantity",
    "notional",
    "leverage",
    "initial_margin",
    "risk_budget",
    "estimated_loss",
    "entry_fee_cost",
    "exit_fee_cost",
    "adverse_funding_cost",
    "entry_slippage_cost",
)


def _plan_from_json(value: dict[str, Any]) -> TradePlan:
    data = dict(value)
    for key in _PLAN_DECIMALS:
        data[key] = Decimal(data[key])
    if data["target_price"] is not None:
        data["target_price"] = Decimal(data["target_price"])
    for key in ("expires_at", "source_signal_at"):
        data[key] = _datetime(data[key])
    for key in ("evidence_feature_ids", "evidence_bar_keys", "macro_evidence_ids"):
        data[key] = tuple(str(item) for item in data[key])
    data["exchange_rules"] = ExchangeRules(
        **{key: Decimal(item) for key, item in data["exchange_rules"].items()}
    )
    data["exit_recipe"] = ExitRecipe(**data["exit_recipe"])
    return TradePlan(**data)


def _receipt_to_json(receipt: ReduceOnlyReceipt) -> dict[str, Any]:
    return {
        "idempotency_key": receipt.idempotency_key,
        "filled_quantity": str(receipt.filled_quantity),
        "reduce_only": receipt.reduce_only,
        "position": _position_to_json(receipt.position),
        "fill": _fill_to_json(receipt.fill) if receipt.fill is not None else None,
        "request_fingerprint": list(receipt.request_fingerprint),
    }


def _receipt_from_json(row: dict[str, Any]) -> ReduceOnlyReceipt:
    return ReduceOnlyReceipt(
        idempotency_key=str(row["idempotency_key"]),
        filled_quantity=Decimal(row["filled_quantity"]),
        reduce_only=bool(row["reduce_only"]),
        position=_position_from_json(row["position"]),
        fill=_fill_from_json(row["fill"]) if row["fill"] is not None else None,
        request_fingerprint=tuple(str(value) for value in row["request_fingerprint"]),
    )


def _datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("datetime value must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime trong snapshot phải có timezone")
    return parsed
