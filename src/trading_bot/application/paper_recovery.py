"""Kiểm tra chéo checkpoint PAPER với ledger bất biến trước khi hydrate."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from typing import Any


class PaperRecoveryConflict(RuntimeError):
    """Checkpoint và ledger PAPER không mô tả cùng một trạng thái."""


def verify_paper_ledger_consistency(
    snapshot: Any,
    *,
    intents: Iterable[Any],
    plans: Iterable[Any],
    fills: Iterable[Any],
    position_events: Iterable[Any],
    trades: Iterable[Any],
    costs: Iterable[Any],
    reservations: Iterable[Any],
) -> None:
    """Từ chối hydrate nếu snapshot không thể tái dựng từ ledger trong cùng account."""
    try:
        body = json.loads(snapshot.payload)
    except (AttributeError, TypeError, json.JSONDecodeError) as error:
        raise PaperRecoveryConflict("Checkpoint payload không thể đối soát ledger.") from error

    intent_rows = tuple(intents)
    plan_rows = tuple(plans)
    fill_rows = tuple(fills)
    position_rows = tuple(position_events)
    trade_rows = tuple(trades)
    cost_rows = tuple(costs)
    reservation_rows = tuple(reservations)
    order_rows = tuple(body.get("orders", ()))
    snapshot_keys = [row.get("idempotency_key") for row in order_rows]
    ledger_keys = [row.idempotency_key for row in intent_rows]
    if len(set(snapshot_keys)) != len(snapshot_keys) or set(snapshot_keys) != set(ledger_keys):
        raise PaperRecoveryConflict("Checkpoint thiếu hoặc có intent ledger thừa trong account.")

    plans_by_hash = {row.plan_hash: row for row in plan_rows}
    positions_by_intent = {row["intent_id"]: row for row in body.get("positions", ())}
    snapshot_plans = body.get("plans", {})
    intents_by_key = {row.idempotency_key: row for row in intent_rows}
    fills_by_intent: dict[Any, list[Any]] = defaultdict(list)
    events_by_intent: dict[Any, list[Any]] = defaultdict(list)
    trades_by_intent: dict[Any, list[Any]] = defaultdict(list)
    costs_by_trade: dict[Any, list[Any]] = defaultdict(list)
    for row in fill_rows:
        fills_by_intent[row.intent_id].append(row)
    for row in position_rows:
        events_by_intent[row.intent_id].append(row)
    for row in trade_rows:
        trades_by_intent[row.intent_id].append(row)
    for row in cost_rows:
        costs_by_trade[row.trade_id].append(row)

    known_intent_ids = {row.id for row in intent_rows}
    if any(row.intent_id not in known_intent_ids for row in fill_rows + position_rows + trade_rows):
        raise PaperRecoveryConflict("Ledger có fill, position event hoặc trade mồ côi.")
    if any(row.trade_id not in {trade.id for trade in trade_rows} for row in cost_rows):
        raise PaperRecoveryConflict("Ledger có trade cost không gắn với trade trong account.")

    snapshot_fill_keys: set[str] = set()
    for order in order_rows:
        intent = intents_by_key[order["idempotency_key"]]
        if (
            order["plan_hash"] != intent.plan_hash
            or order["symbol"] != intent.symbol
            or order["direction"].upper() != intent.side
            or Decimal(order["requested_quantity"]) != intent.quantity
            or Decimal(order["filled_quantity"]) != intent.filled_quantity
            or order["status"] != intent.status
        ):
            raise PaperRecoveryConflict("Checkpoint order không khớp intent ledger.")

        plan_payload = snapshot_plans.get(order["intent_id"])
        plan_record = plans_by_hash.get(intent.plan_hash)
        if plan_record is None or plan_payload is None:
            raise PaperRecoveryConflict("Checkpoint thiếu TradePlan ledger hoặc plan hash.")
        if (
            plan_payload.get("plan_hash") != intent.plan_hash
            or plan_payload.get("settings_version_id") != str(intent.settings_version_id)
            or plan_payload.get("symbol") != intent.symbol
            or Decimal(plan_payload["quantity"]) != intent.quantity
            or plan_payload.get("settings_hash") != intent.evidence.get("settings_hash")
            or plan_record.payload != plan_payload
        ):
            raise PaperRecoveryConflict("Checkpoint TradePlan không khớp payload ledger.")

        reservation = next(
            (row for row in reservation_rows if row.id == intent.risk_reservation_id), None
        )
        if (
            reservation is None
            or reservation.risk_decision_id != intent.risk_decision_id
            or reservation.plan_hash != intent.plan_hash
            or reservation.account_id != intent.account_id
            or reservation.workspace_id != intent.workspace_id
            or reservation.status not in {"RESERVED", "UNKNOWN", "RELEASED", "TRANSFERRED"}
        ):
            raise PaperRecoveryConflict("Risk reservation không khớp intent/plan lineage.")

        order_fills = tuple(order.get("fills", ()))
        paper_position = positions_by_intent.get(order["intent_id"])
        close_receipts = body.get("close_keys", {}).values()
        exit_fills = tuple(
            sorted(
                (
                    receipt["fill"]
                    for receipt in close_receipts
                    if receipt["position"]["intent_id"] == order["intent_id"]
                    and receipt.get("fill") is not None
                ),
                key=lambda row: (row["filled_at"], row["fill_id"]),
            )
        )
        expected_fills = {("ENTRY", row["fill_id"]): row for row in order_fills} | {
            ("EXIT", row["fill_id"]): row for row in exit_fills
        }
        actual_fills = list(fills_by_intent[intent.id])
        if {(row.fill_role, row.fill_key) for row in actual_fills} != set(expected_fills):
            raise PaperRecoveryConflict("Checkpoint fill set không khớp fill ledger.")
        for ledger_fill in actual_fills:
            paper_fill = expected_fills[(ledger_fill.fill_role, ledger_fill.fill_key)]
            expected_values = (
                paper_fill["bar_key"],
                paper_fill["fill_policy_version"],
                Decimal(paper_fill["quantity"]),
                Decimal(paper_fill["execution_price"]),
                Decimal(paper_fill["fee_cost"]),
                Decimal(paper_fill["slippage_cost"]),
            )
            actual_values = (
                ledger_fill.bar_key,
                ledger_fill.fill_policy_version,
                ledger_fill.quantity,
                ledger_fill.price,
                ledger_fill.fee_amount,
                ledger_fill.slippage_amount,
            )
            if actual_values != expected_values:
                raise PaperRecoveryConflict("Checkpoint ENTRY/EXIT fill payload lệch ledger.")
            if ledger_fill.lineage != _intent_lineage(intent):
                raise PaperRecoveryConflict("Fill profile/settings lineage lệch intent ledger.")
            snapshot_fill_keys.add(ledger_fill.fill_key)

        entry_quantity = sum(
            (row.quantity for row in actual_fills if row.fill_role == "ENTRY"), Decimal(0)
        )
        exit_quantity = sum(
            (row.quantity for row in actual_fills if row.fill_role == "EXIT"), Decimal(0)
        )
        trade_group = trades_by_intent[intent.id]
        if entry_quantity == 0:
            if paper_position is not None or trade_group or events_by_intent[intent.id]:
                raise PaperRecoveryConflict("Position/trade tồn tại nhưng chưa có ENTRY fill.")
            if reservation.status == "RELEASED" and intent.status not in {"CANCELLED", "EXPIRED"}:
                raise PaperRecoveryConflict("Reservation đã giải phóng khi order còn active.")
            continue

        if paper_position is None or len(trade_group) != 1:
            raise PaperRecoveryConflict("Checkpoint position/trade không khớp ENTRY fills.")
        position = paper_position
        trade = trade_group[0]
        if trade.lineage != _intent_lineage(intent):
            raise PaperRecoveryConflict("Trade profile/settings lineage lệch intent ledger.")
        remaining = entry_quantity - exit_quantity
        if (
            remaining < 0
            or Decimal(position["quantity"]) != remaining
            or bool(position["is_closed"]) != (remaining == 0)
            or trade.status != ("CLOSED" if remaining == 0 else "OPEN")
        ):
            raise PaperRecoveryConflict("Position quantity/open-close không khớp fill ledger.")
        if reservation.status == "RELEASED" and remaining > 0:
            raise PaperRecoveryConflict("Reservation đã giải phóng khi vị thế vẫn mở.")

        fees = sum((row.fee_amount for row in actual_fills), Decimal(0))
        slippage = sum((row.slippage_amount for row in actual_fills), Decimal(0))
        trade_costs = costs_by_trade[trade.id]
        fee_costs = sum((row.amount for row in trade_costs if row.cost_type == "FEE"), Decimal(0))
        funding_costs = sum(
            (row.amount for row in trade_costs if row.cost_type == "FUNDING"), Decimal(0)
        )
        slip_costs = sum(
            (row.amount for row in trade_costs if row.cost_type == "SLIPPAGE"), Decimal(0)
        )
        if (
            fees != fee_costs
            or slippage != slip_costs
            or Decimal(position["fee_cost"]) != fees
            or Decimal(position["slippage_cost"]) != slippage
            or Decimal(position["funding_cost"]) != funding_costs
            or {row.source_event_id for row in trade_costs if row.cost_type == "FUNDING"}
            != set(position["funding_event_ids"])
        ):
            raise PaperRecoveryConflict("Position/trade costs không khớp fill và cost ledger.")

        expected_events: dict[str, tuple[str, Decimal]] = {}
        cumulative = Decimal(0)
        for index, fill in enumerate(order_fills):
            cumulative += Decimal(fill["quantity"])
            expected_events[f"entry-fill:{fill['fill_id']}"] = (
                "OPENED" if index == 0 else "INCREASED",
                cumulative,
            )
        remaining_after = entry_quantity
        for fill in exit_fills:
            remaining_after -= Decimal(fill["quantity"])
            expected_events[f"exit-fill:{fill['fill_id']}"] = (
                "CLOSED" if remaining_after == 0 else "REDUCED",
                remaining_after,
            )
        actual_events = {row.event_key: row for row in events_by_intent[intent.id]}
        stop_events = {
            row.event_key: row
            for row in events_by_intent[intent.id]
            if row.event_type == "STOP_UPDATED"
        }
        if set(actual_events) != set(expected_events) | set(stop_events):
            raise PaperRecoveryConflict("Position event ledger có sự kiện thiếu hoặc thừa.")
        for event_key, (event_type, quantity) in expected_events.items():
            event = actual_events[event_key]
            if event.event_type != event_type or event.quantity != quantity:
                raise PaperRecoveryConflict("Position event quantity/type lệch fill ledger.")
        for event in stop_events.values():
            if (
                event.quantity != Decimal(position["quantity"])
                or event.payload.get("bar_key") not in position["seen_bar_keys"]
                or not event.payload.get("stop_price")
            ):
                raise PaperRecoveryConflict("STOP_UPDATED event không khớp position snapshot.")
        if stop_events:
            latest_stop = max(stop_events.values(), key=lambda row: row.occurred_at)
            if Decimal(latest_stop.payload["stop_price"]) != Decimal(position["stop_price"]):
                raise PaperRecoveryConflict("Stop price hiện hành không khớp position event.")
        if trade.status == "OPEN" and exit_quantity:
            raise PaperRecoveryConflict("Partial EXIT của OPEN trade chưa có gross PnL ledger.")
        if trade.status == "OPEN":
            expected_fee = expected_funding = expected_slippage = expected_total = Decimal(0)
            expected_gross = Decimal(0)
            expected_net = Decimal(0)
        else:
            expected_fee = fee_costs
            expected_funding = funding_costs
            expected_slippage = slip_costs
            expected_total = fee_costs + funding_costs + slip_costs
            expected_gross = Decimal(position["gross_realized_pnl"])
            expected_net = expected_gross - expected_total
        if (
            trade.fee_total != expected_fee
            or trade.funding_total != expected_funding
            or trade.slippage_total != expected_slippage
            or trade.total_cost != expected_total
            or trade.gross_pnl != expected_gross
            or trade.net_pnl != expected_net
        ):
            raise PaperRecoveryConflict(
                "Trade totals không khớp quy ước OPEN/CLOSED và cost ledger."
            )

    if snapshot_fill_keys != {row.fill_key for row in fill_rows}:
        raise PaperRecoveryConflict("Ledger có fill thừa ngoài checkpoint account state.")


def _intent_lineage(intent: Any) -> dict[str, Any]:
    lineage: dict[str, Any] = {
        "settings_version_id": str(intent.settings_version_id),
        "strategy_release_id": str(intent.strategy_release_id),
        "strategy_release_hash": intent.strategy_release_hash,
        "feature_snapshot_id": intent.feature_snapshot_id,
        "candidate_id": intent.candidate_id,
        "quote_asset": intent.quote_asset,
        "risk_decision_id": str(intent.risk_decision_id),
        "risk_reservation_id": str(intent.risk_reservation_id),
        "plan_hash": intent.plan_hash,
    }
    profile_hash = intent.evidence.get("paper_execution_profile_hash")
    if isinstance(profile_hash, str) and profile_hash:
        lineage["paper_execution_profile_hash"] = profile_hash
    return lineage
