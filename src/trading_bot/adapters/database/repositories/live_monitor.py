"""Truy vấn chỉ đọc cho màn hình theo dõi bot trực tiếp (LIVE-MON, CRY-429).

Mọi câu lệnh là SELECT có bộ lọc ``workspace_id`` + ``bot_id = ANY(...)`` tường minh; RLS
``bot_member_isolation`` của ``crypidian_admin_web`` là lớp phạm vi bổ sung. Repository chạy trong
transaction READ ONLY do ``UnitOfWork(snapshot_read_only=True)`` quản lý nên không thể ghi hay
khóa hàng của runtime. Mỗi nguồn sự kiện có ``LIMIT`` để một lượt đọc luôn bị chặn trên.

Nguồn sự kiện lấy từ ledger PAPER (đủ symbol/giá/PnL); outbox chỉ dùng cho lệnh hết hạn
(``paper.order_intent.expired`` không có cột thời điểm trên ``order_intent``) và chỉ số độ trễ.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Uuid, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

EVENT_SOURCE_LIMIT = 200
INITIAL_EVENT_LIMIT = 60
# Lượt đầu chỉ nhìn lại một cửa sổ gần đây để dùng chỉ mục thời gian, không quét toàn lịch sử
# (bot_decision tăng ~10k hàng/ngày với 20 coin × nhiều bot).
INITIAL_LOOKBACK = {"decision": "2 hours"}
INITIAL_LOOKBACK_DEFAULT = "7 days"
EQUITY_CURVE_LIMIT = 200
# Feed giá: nguồn live trước, REST backfill sau (cùng thứ tự với chính sách mark của ledger).
FEED_SOURCES = ("BINANCE_USDM", "BINANCE_USDM_REST_BACKFILL")
FEED_TIMEFRAME = "1m"
# Loại sự kiện outbox đang có consumer claim (signal fan-out, rights invalidation). Các loại
# khác (``paper.*``, ``bot.config.*``) hiện chỉ là bản ghi, không có consumer → không tính lag.
CONSUMED_OUTBOX_EVENT_TYPES = ("signal.evidence.recorded.v1", "research.rights_changed.v1")

_BOT_IDS = bindparam("bot_ids", type_=ARRAY(Uuid(as_uuid=True)))

# Mỗi nguồn trả cùng bộ cột: id, bot_id, recorded_at, occurred_at, symbol, side + cột riêng.
_EVENT_QUERIES: dict[str, str] = {
    "decision": (
        "SELECT d.id, d.bot_id, d.created_at AS recorded_at, d.evaluated_at AS occurred_at, "
        "coalesce(d.payload->'candidate'->>'symbol', e.symbol) AS symbol, "
        "upper(d.payload->'candidate'->>'direction') AS side, d.status, d.reason_code, "
        "coalesce(d.payload->'candidate'->>'detail_vi', d.payload->>'detail_vi') AS detail_vi, "
        "e.observed_at AS bar_close_at "
        "FROM bot_decision d LEFT JOIN shared_signal_evidence e "
        "ON e.workspace_id = d.workspace_id AND e.id = d.evidence_id "
        "WHERE d.workspace_id = :workspace_id AND d.bot_id = ANY(:bot_ids) {window}"
    ),
    "reservation": (
        "SELECT r.id, r.bot_id, r.created_at AS recorded_at, r.created_at AS occurred_at, "
        "r.symbol, NULL AS side, r.reserved_risk, r.status "
        "FROM risk_reservation r "
        "WHERE r.workspace_id = :workspace_id AND r.bot_id = ANY(:bot_ids) {window}"
    ),
    "order": (
        "SELECT o.id, o.bot_id, o.created_at AS recorded_at, o.created_at AS occurred_at, "
        "o.symbol, o.side, o.quantity, o.status "
        "FROM order_intent o "
        "WHERE o.workspace_id = :workspace_id AND o.bot_id = ANY(:bot_ids) AND o.mode = 'PAPER' "
        "{window}"
    ),
    "expired": (
        "SELECT x.id, o.bot_id, x.created_at AS recorded_at, x.occurred_at, o.symbol, o.side "
        "FROM outbox_event x JOIN order_intent o "
        "ON o.workspace_id = x.workspace_id AND o.id = x.aggregate_id "
        "WHERE x.workspace_id = :workspace_id AND x.event_type = 'paper.order_intent.expired' "
        "AND o.bot_id = ANY(:bot_ids) {window}"
    ),
    "fill": (
        "SELECT f.id, f.bot_id, f.created_at AS recorded_at, f.occurred_at, o.symbol, o.side, "
        "f.fill_role, f.price, f.quantity "
        "FROM fill f JOIN order_intent o "
        "ON o.workspace_id = f.workspace_id AND o.account_id = f.account_id AND o.id = f.intent_id "
        "WHERE f.workspace_id = :workspace_id AND f.bot_id = ANY(:bot_ids) AND f.mode = 'PAPER' "
        "{window}"
    ),
    "position": (
        "SELECT p.id, p.bot_id, p.created_at AS recorded_at, p.occurred_at, o.symbol, o.side, "
        "p.event_type, p.payload->>'stop_price' AS stop_price, "
        "p.payload->>'exit_reason' AS exit_reason, t.net_pnl, t.funding_total, t.quote_asset, "
        "coalesce(pl.payload->>'estimated_loss', pl.payload->>'risk_budget') AS planned_risk "
        "FROM position_event p JOIN order_intent o "
        "ON o.workspace_id = p.workspace_id AND o.account_id = p.account_id AND o.id = p.intent_id "
        "LEFT JOIN trade t ON t.workspace_id = p.workspace_id AND t.account_id = p.account_id "
        "AND t.intent_id = p.intent_id AND p.event_type = 'CLOSED' "
        "LEFT JOIN trade_plan pl ON pl.workspace_id = t.workspace_id AND pl.bot_id = t.bot_id "
        "AND pl.plan_hash = t.lineage->>'plan_hash' "
        "WHERE p.workspace_id = :workspace_id AND p.bot_id = ANY(:bot_ids) AND p.mode = 'PAPER' "
        "{window}"
    ),
    "funding": (
        "SELECT c.id, c.bot_id, c.created_at AS recorded_at, c.occurred_at, t.symbol, t.side, "
        "c.amount, c.currency "
        "FROM trade_cost c JOIN trade t "
        "ON t.workspace_id = c.workspace_id AND t.account_id = c.account_id AND t.id = c.trade_id "
        "WHERE c.workspace_id = :workspace_id AND c.bot_id = ANY(:bot_ids) AND c.mode = 'PAPER' "
        "AND c.cost_type = 'FUNDING' {window}"
    ),
}
_ALIAS = {
    "decision": "d",
    "reservation": "r",
    "order": "o",
    "expired": "x",
    "fill": "f",
    "position": "p",
    "funding": "c",
}


_FEED_CLOCK_SQL = (
    "SELECT (SELECT max(b.close_time) FROM "
    "(SELECT DISTINCT symbol FROM market_instrument WHERE workspace_id = :workspace_id) i "
    "CROSS JOIN LATERAL (SELECT close_time FROM market_bar mb "
    "WHERE mb.workspace_id = :workspace_id AND mb.symbol = i.symbol "
    "AND mb.timeframe = :timeframe AND mb.is_closed AND mb.source = ANY(:sources) "
    "ORDER BY mb.open_time DESC LIMIT 1) b) AS latest_bar_close_at, "
    "(SELECT max(m.received_at) FROM market_instrument i CROSS JOIN LATERAL "
    "(SELECT received_at FROM market_mark_price mm "
    "WHERE mm.workspace_id = i.workspace_id AND mm.instrument_id = i.id "
    "ORDER BY mm.event_at DESC LIMIT 1) m WHERE i.workspace_id = :workspace_id) "
    "AS latest_mark_at"
)


class LiveMonitorRepository:
    """Đọc trong transaction READ ONLY do caller (UnitOfWork) quản lý."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _rows(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        statement = text(sql)
        if "bot_ids" in params:
            statement = statement.bindparams(_BOT_IDS)
        result = await self._session.execute(statement, params)
        return [dict(row) for row in result.mappings().all()]

    async def database_now(self) -> datetime:
        """Giờ thực của DB (``clock_timestamp``, không phải giờ bắt đầu transaction)."""
        return (await self._session.execute(text("SELECT clock_timestamp()"))).scalar_one()

    async def events(
        self, *, workspace_id: UUID, bot_ids: Sequence[UUID], since: datetime | None
    ) -> list[dict[str, Any]]:
        """Hàng sự kiện mọi nguồn; ``since=None`` → các hàng gần nhất của mỗi nguồn."""
        if not bot_ids:
            return []
        rows: list[dict[str, Any]] = []
        for source, template in _EVENT_QUERIES.items():
            alias = _ALIAS[source]
            if since is None:
                lookback = INITIAL_LOOKBACK.get(source, INITIAL_LOOKBACK_DEFAULT)
                window = (
                    f"AND {alias}.created_at > clock_timestamp() - interval '{lookback}' "
                    f"ORDER BY {alias}.created_at DESC, {alias}.id LIMIT {INITIAL_EVENT_LIMIT}"
                )
            else:
                window = (
                    f"AND {alias}.created_at > :since "
                    f"ORDER BY {alias}.created_at, {alias}.id LIMIT {EVENT_SOURCE_LIMIT}"
                )
            params: dict[str, Any] = {"workspace_id": workspace_id, "bot_ids": list(bot_ids)}
            if since is not None:
                params["since"] = since
            for row in await self._rows(template.format(window=window), params):
                rows.append(row | {"source": source})
        return rows

    async def open_positions(
        self, *, workspace_id: UUID, bot_ids: Sequence[UUID]
    ) -> list[dict[str, Any]]:
        """Trade PAPER đang mở kèm fill vào/ra, SL hiện hành (STOP_UPDATED mới nhất) và kế hoạch."""
        if not bot_ids:
            return []
        trades = await self._rows(
            "SELECT t.id AS trade_id, t.bot_id, t.account_id, t.intent_id, t.symbol, t.side, "
            "t.quote_asset, t.opened_at, t.funding_total, t.fee_total, "
            "pl.payload->>'stop_price' AS initial_stop, "
            "pl.payload->>'target_price' AS target "
            "FROM trade t LEFT JOIN trade_plan pl ON pl.workspace_id = t.workspace_id "
            "AND pl.bot_id = t.bot_id AND pl.plan_hash = t.lineage->>'plan_hash' "
            "WHERE t.workspace_id = :workspace_id AND t.bot_id = ANY(:bot_ids) "
            "AND t.mode = 'PAPER' AND t.status = 'OPEN' ORDER BY t.opened_at, t.id LIMIT 500",
            {"workspace_id": workspace_id, "bot_ids": list(bot_ids)},
        )
        if not trades:
            return []
        intent_ids = [row["intent_id"] for row in trades]
        statement = text(
            "SELECT f.intent_id, f.fill_role, f.price, f.quantity FROM fill f "
            "WHERE f.workspace_id = :workspace_id AND f.bot_id = ANY(:bot_ids) "
            "AND f.intent_id = ANY(:intent_ids) ORDER BY f.occurred_at, f.id"
        ).bindparams(_BOT_IDS, bindparam("intent_ids", type_=ARRAY(Uuid(as_uuid=True))))
        fills = (
            await self._session.execute(
                statement,
                {"workspace_id": workspace_id, "bot_ids": list(bot_ids), "intent_ids": intent_ids},
            )
        ).mappings()
        stops_statement = text(
            "SELECT DISTINCT ON (p.intent_id) p.intent_id, p.payload->>'stop_price' AS stop_price "
            "FROM position_event p WHERE p.workspace_id = :workspace_id "
            "AND p.bot_id = ANY(:bot_ids) AND p.intent_id = ANY(:intent_ids) "
            "AND p.event_type = 'STOP_UPDATED' ORDER BY p.intent_id, p.occurred_at DESC, p.id DESC"
        ).bindparams(_BOT_IDS, bindparam("intent_ids", type_=ARRAY(Uuid(as_uuid=True))))
        stops = {
            row["intent_id"]: row["stop_price"]
            for row in (
                await self._session.execute(
                    stops_statement,
                    {
                        "workspace_id": workspace_id,
                        "bot_ids": list(bot_ids),
                        "intent_ids": intent_ids,
                    },
                )
            ).mappings()
        }
        entries: dict[UUID, list[dict[str, Any]]] = {}
        exits: dict[UUID, Decimal] = {}
        for fill in fills:
            if fill["fill_role"] == "ENTRY":
                entries.setdefault(fill["intent_id"], []).append(
                    {"price": fill["price"], "quantity": fill["quantity"]}
                )
            elif fill["fill_role"] == "EXIT":
                exits[fill["intent_id"]] = exits.get(fill["intent_id"], Decimal(0)) + Decimal(
                    str(fill["quantity"])
                )
        return [
            trade
            | {
                "entry_fills": entries.get(trade["intent_id"], []),
                "exit_quantity": exits.get(trade["intent_id"], Decimal(0)),
                "current_stop": stops.get(trade["intent_id"]),
            }
            for trade in trades
        ]

    async def latest_prices(
        self, *, workspace_id: UUID, symbols: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Giá tham chiếu mới nhất theo symbol: mark price nếu có, không thì giá đóng nến 1m."""
        if not symbols:
            return {}
        params = {
            "workspace_id": workspace_id,
            "symbols": sorted(set(symbols)),
            "sources": list(FEED_SOURCES),
            "timeframe": FEED_TIMEFRAME,
        }
        marks = await self._session.execute(
            text(
                "SELECT DISTINCT ON (i.symbol) i.symbol, m.mark_price, m.received_at "
                "FROM market_instrument i CROSS JOIN LATERAL (SELECT mark_price, received_at, "
                "event_at FROM market_mark_price mm WHERE mm.workspace_id = i.workspace_id "
                "AND mm.instrument_id = i.id AND mm.available_at <= clock_timestamp() "
                "ORDER BY mm.event_at DESC LIMIT 1) m "
                "WHERE i.workspace_id = :workspace_id AND i.symbol = ANY(:symbols) "
                "ORDER BY i.symbol, m.event_at DESC"
            ),
            params,
        )
        prices = {
            row["symbol"]: {"price": row["mark_price"], "at": row["received_at"], "source": "MARK"}
            for row in marks.mappings()
        }
        wanted: list[str] = sorted(set(symbols))
        missing = [symbol for symbol in wanted if symbol not in prices]
        if missing:
            bars = await self._session.execute(
                text(
                    "SELECT s.symbol, b.close, b.close_time "
                    "FROM unnest(CAST(:symbols AS text[])) AS s(symbol) "
                    "CROSS JOIN LATERAL (SELECT close, close_time FROM market_bar mb "
                    "WHERE mb.workspace_id = :workspace_id AND mb.symbol = s.symbol "
                    "AND mb.timeframe = :timeframe AND mb.is_closed "
                    "AND mb.source = ANY(:sources) AND mb.available_at <= clock_timestamp() "
                    "ORDER BY mb.open_time DESC, (mb.source = 'BINANCE_USDM') DESC, "
                    "mb.revision DESC LIMIT 1) b"
                ),
                params | {"symbols": missing},
            )
            for row in bars.mappings():
                prices[row["symbol"]] = {
                    "price": row["close"],
                    "at": row["close_time"],
                    "source": "BAR_CLOSE_1M",
                }
        return prices

    async def equity_inputs(
        self, *, workspace_id: UUID, bot_ids: Sequence[UUID], day_start: datetime
    ) -> dict[str, Any]:
        """Vốn ban đầu, PnL đã chốt (tổng + đỉnh lũy kế + đoạn cuối đường vốn) và lỗ hôm nay."""
        if not bot_ids:
            return {"seeds": {}, "per_bot": {}, "curve": [], "before": None, "today": {}}
        params = {"workspace_id": workspace_id, "bot_ids": list(bot_ids)}
        seeds = {
            row["bot_id"]: row
            for row in await self._rows(
                "SELECT DISTINCT ON (s.bot_id) s.bot_id, s.initial_equity, s.quote_asset "
                "FROM paper_account_seed s WHERE s.workspace_id = :workspace_id "
                "AND s.bot_id = ANY(:bot_ids) ORDER BY s.bot_id, s.created_at DESC",
                params,
            )
        }
        closed = (
            "SELECT t.bot_id, t.closed_at, t.id, t.net_pnl FROM trade t "
            "WHERE t.workspace_id = :workspace_id AND t.bot_id = ANY(:bot_ids) "
            "AND t.mode = 'PAPER' AND t.status = 'CLOSED' AND t.closed_at IS NOT NULL"
        )
        per_bot = {
            row["bot_id"]: row
            for row in await self._rows(
                "SELECT bot_id, sum(net_pnl) AS realized, greatest(0, max(cum)) AS peak_realized, "
                "count(*) AS closed_count FROM (SELECT bot_id, net_pnl, sum(net_pnl) OVER "
                "(PARTITION BY bot_id ORDER BY closed_at, id) AS cum FROM (" + closed + ") c) w "
                "GROUP BY bot_id",
                params,
            )
        }
        # Đường vốn gộp: EQUITY_CURVE_LIMIT điểm cuối, phần trước đó gộp thành tổng + đỉnh.
        curve_rows = await self._rows(
            "SELECT closed_at, net_pnl, cum, rn FROM (SELECT closed_at, net_pnl, "
            "sum(net_pnl) OVER (ORDER BY closed_at, id) AS cum, "
            "row_number() OVER (ORDER BY closed_at DESC, id DESC) AS rn FROM (" + closed + ") c) w "
            f"WHERE rn <= {EQUITY_CURVE_LIMIT + 1} ORDER BY closed_at, rn DESC",
            params,
        )
        before: dict[str, Any] | None = None
        if len(curve_rows) > EQUITY_CURVE_LIMIT:
            first = curve_rows.pop(0)
            peak_row = await self._rows(
                "SELECT greatest(0, max(cum)) AS peak FROM (SELECT sum(net_pnl) OVER "
                "(ORDER BY closed_at, id) AS cum, row_number() OVER "
                "(ORDER BY closed_at DESC, id DESC) AS rn FROM (" + closed + ") c) w "
                f"WHERE rn > {EQUITY_CURVE_LIMIT}",
                params,
            )
            before = {"realized": first["cum"], "peak": peak_row[0]["peak"] if peak_row else 0}
        today = {
            row["bot_id"]: row["net"]
            for row in await self._rows(
                "SELECT t.bot_id, sum(t.net_pnl) AS net FROM trade t "
                "WHERE t.workspace_id = :workspace_id AND t.bot_id = ANY(:bot_ids) "
                "AND t.mode = 'PAPER' AND t.status = 'CLOSED' AND t.closed_at >= :day_start "
                "GROUP BY t.bot_id",
                params | {"day_start": day_start},
            )
        }
        return {
            "seeds": seeds,
            "per_bot": per_bot,
            "curve": [(row["closed_at"], row["net_pnl"]) for row in curve_rows],
            "before": before,
            "today": today,
        }

    async def feed_clock(self, *, workspace_id: UUID) -> dict[str, Any]:
        """Thời điểm nến 1m đóng gần nhất, mark price gần nhất và giờ DB của workspace."""
        params = {
            "workspace_id": workspace_id,
            "sources": list(FEED_SOURCES),
            "timeframe": FEED_TIMEFRAME,
        }
        result = await self._session.execute(text(_FEED_CLOCK_SQL), params)
        row = result.mappings().one()
        return dict(row)

    async def outbox_stats(self, *, workspace_id: UUID) -> dict[str, Any]:
        """Sự kiện outbox đang chờ: loại có consumer (tính lag) và loại chưa có consumer."""
        rows = await self._rows(
            "SELECT (event_type = ANY(:consumed)) AS consumed, count(*) AS pending, "
            "min(available_at) AS oldest FROM outbox_event "
            "WHERE workspace_id = :workspace_id AND status = 'PENDING' GROUP BY 1",
            {"workspace_id": workspace_id, "consumed": list(CONSUMED_OUTBOX_EVENT_TYPES)},
        )
        stats: dict[str, Any] = {"pending": 0, "oldest": None, "unconsumed_pending": 0}
        for row in rows:
            if row["consumed"]:
                stats["pending"] = int(row["pending"])
                stats["oldest"] = row["oldest"]
            else:
                stats["unconsumed_pending"] = int(row["pending"])
        return stats

    async def runtime_heartbeats(
        self, *, workspace_id: UUID, bot_ids: Sequence[UUID]
    ) -> dict[UUID, datetime]:
        """Nhịp tim runtime gần nhất của từng bot (bảng ``bot_runtime_state``)."""
        if not bot_ids:
            return {}
        rows = await self._rows(
            "SELECT bot_id, max(heartbeat_at) AS heartbeat_at FROM bot_runtime_state "
            "WHERE workspace_id = :workspace_id AND bot_id = ANY(:bot_ids) GROUP BY bot_id",
            {"workspace_id": workspace_id, "bot_ids": list(bot_ids)},
        )
        return {row["bot_id"]: row["heartbeat_at"] for row in rows}
