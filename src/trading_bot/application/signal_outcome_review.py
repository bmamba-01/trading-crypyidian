"""Trang Admin "Kết quả tín hiệu" (CRY-430), chỉ đọc.

Đọc ``signal_outcome`` (đã chấm bởi worker) qua role ``admin-web`` (RLS theo workspace), ghép tên
bot và trạng thái gửi Telegram của tín hiệu để so tín hiệu **đã gửi** với tín hiệu **không gửi**
(bị lọc/SUPPRESSED/hết hạn hoặc bot không có đăng ký). Quyền ``WORKSPACE_READ``; không ghi gì.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    BotInstance,
    SignalOutcomeRecord,
    TelegramSignalDelivery,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.domain.strategy.signal_outcome import HORIZON_MULTIPLES, METHOD_VERSION
from trading_bot.domain.strategy.signal_outcome_summary import (
    OutcomeRow,
    bucket_labels,
    group_summaries,
    repeated_wrong,
    summarize,
)

MAX_ROWS = 5000
RECENT_ROWS = 50
PERIOD_DAYS = {"7": 7, "30": 30, "90": 90}
DELIVERY_VI = {"sent": "Đã gửi", "held": "Bị lọc/giữ lại", "none": "Không gửi"}
TOUCH_VI = {"TP1": "TP1 trước", "SL": "SL trước", "NONE": "Chưa chạm"}
LABEL_VI = {1: "Đúng", 0: "Hòa", -1: "Sai"}
STATUS_VI = {"SCORED": "Đã chấm", "DATA_GAP": "Thiếu nến", "INVALID_SIGNAL": "Tín hiệu lỗi"}


class SignalOutcomeReviewError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _row(record: SignalOutcomeRecord) -> OutcomeRow:
    return OutcomeRow(
        status=record.status,
        label=record.label,
        outcome_r=record.outcome_r,
        mfe_r=record.mfe_r,
        mae_r=record.mae_r,
        first_touch=record.first_touch,
        same_bar_conflict=record.same_bar_conflict,
        seconds_to_first_touch=record.seconds_to_first_touch,
    )


def _r(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:+.2f}R"


class SignalOutcomeReviewService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        unit_of_work: Callable[..., Any] = UnitOfWork,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._unit_of_work = unit_of_work
        self._now = now or (lambda: datetime.now(UTC))

    def _uow(self, principal: Principal) -> Any:
        if not authorize(principal, Permission.WORKSPACE_READ):
            raise SignalOutcomeReviewError(403, "Bạn không có quyền xem kết quả tín hiệu.")
        return self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        )

    async def overview(
        self,
        principal: Principal,
        *,
        bot_id: UUID | None = None,
        release: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        horizon: int = 4,
        period: str = "30",
    ) -> dict[str, Any]:
        if horizon not in HORIZON_MULTIPLES:
            horizon = 4
        days = PERIOD_DAYS.get(period, 30)
        since = self._now() - timedelta(days=days)
        workspace = principal.workspace_id
        async with self._uow(principal) as unit:
            session = unit.session
            bots: dict[UUID, str] = dict(
                (
                    await session.execute(
                        select(BotInstance.id, BotInstance.name)
                        .where(BotInstance.workspace_id == workspace)
                        .order_by(BotInstance.name)
                    )
                )
                .tuples()
                .all()
            )
            base = (
                SignalOutcomeRecord.workspace_id == workspace,
                SignalOutcomeRecord.method_version == METHOD_VERSION,
                SignalOutcomeRecord.horizon_bars == horizon,
                SignalOutcomeRecord.signal_at >= since,
            )
            options = (
                await session.execute(
                    select(
                        SignalOutcomeRecord.strategy_release_id,
                        SignalOutcomeRecord.symbol,
                        SignalOutcomeRecord.timeframe,
                    )
                    .where(*base)
                    .distinct()
                )
            ).all()
            filters = list(base)
            if bot_id is not None:
                filters.append(SignalOutcomeRecord.bot_id == bot_id)
            if release:
                filters.append(SignalOutcomeRecord.strategy_release_id == release)
            if symbol:
                filters.append(SignalOutcomeRecord.symbol == symbol)
            if timeframe:
                filters.append(SignalOutcomeRecord.timeframe == timeframe)
            delivered = exists().where(
                TelegramSignalDelivery.workspace_id == SignalOutcomeRecord.workspace_id,
                TelegramSignalDelivery.decision_id == SignalOutcomeRecord.decision_id,
                TelegramSignalDelivery.status == "SENT",
            )
            held = exists().where(
                TelegramSignalDelivery.workspace_id == SignalOutcomeRecord.workspace_id,
                TelegramSignalDelivery.decision_id == SignalOutcomeRecord.decision_id,
            )
            result = await session.execute(
                select(SignalOutcomeRecord, delivered.label("sent"), held.label("queued"))
                .where(*filters)
                .order_by(SignalOutcomeRecord.signal_at.desc(), SignalOutcomeRecord.id)
                .limit(MAX_ROWS)
            )
            records = [(record, bool(sent), bool(queued)) for record, sent, queued in result.all()]
        delivery_of = {
            record.id: ("sent" if sent else "held" if queued else "none")
            for record, sent, queued in records
        }
        rows = [(record, _row(record)) for record, _sent, _queued in records]
        return {
            "filters": {
                "bot_id": str(bot_id) if bot_id else "",
                "release": release or "",
                "symbol": symbol or "",
                "timeframe": timeframe or "",
                "horizon": horizon,
                "period": str(days),
            },
            "options": {
                "bots": [{"id": str(key), "name": name} for key, name in bots.items()],
                "releases": sorted({item[0] for item in options}),
                "symbols": sorted({item[1] for item in options}),
                "timeframes": sorted({item[2] for item in options}),
                "horizons": list(HORIZON_MULTIPLES),
                "periods": list(PERIOD_DAYS),
            },
            "method_version": METHOD_VERSION,
            "truncated": len(records) >= MAX_ROWS,
            "summary": summarize(row for _record, row in rows),
            "bucket_labels": bucket_labels(),
            "by_setup": group_summaries(
                (((record.strategy_release_id, record.timeframe), row) for record, row in rows),
                key_text=lambda key: f"{key[0]} · {key[1]}",
            ),
            "by_delivery": group_summaries(
                ((DELIVERY_VI[delivery_of[record.id]]), row) for record, row in rows
            ),
            "by_symbol": group_summaries(((record.symbol, row) for record, row in rows), limit=20),
            "repeated_wrong": repeated_wrong(
                (
                    (f"{record.strategy_release_id} · {record.symbol} · {record.direction}", row)
                    for record, row in rows
                ),
                min_wrong=2,
            ),
            "recent": [
                {
                    "signal_at": record.signal_at,
                    "bot_name": bots.get(record.bot_id, str(record.bot_id)[:8]),
                    "release": record.strategy_release_id,
                    "symbol": record.symbol,
                    "timeframe": record.timeframe,
                    "direction": "Mua" if record.direction == "long" else "Bán",
                    "status": STATUS_VI.get(record.status, record.status),
                    "touch": TOUCH_VI.get(record.first_touch or "", "—"),
                    "conflict": bool(record.same_bar_conflict),
                    "outcome_r": _r(record.outcome_r),
                    "mfe_r": _r(record.mfe_r),
                    "mae_r": _r(record.mae_r),
                    "label": LABEL_VI.get(record.label, "—") if record.label is not None else "—",
                    "label_value": record.label,
                    "delivery": DELIVERY_VI[delivery_of[record.id]],
                }
                for record, _row_ in rows[:RECENT_ROWS]
            ],
        }


__all__ = ["SignalOutcomeReviewError", "SignalOutcomeReviewService"]
