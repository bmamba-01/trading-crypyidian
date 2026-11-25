"""Trang Admin xem lượt AI Analyst và backtest đề xuất (CRY-383, AI-2/admin-page), chỉ đọc.

Đọc ``analyst_run`` và ``run_report`` qua report-reader theo workspace; backtest đề xuất tiếp
tục chịu RLS ``REPORT_READ`` theo từng bot. Chỉ chọn cột giao diện cần và không đọc ``raw_stdout``.
Đầu ra AI chỉ là gợi ý: trang hiển thị kèm trạng thái validator, không tự áp vào bot.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import load_only

from trading_bot.adapters.database.models import (
    AnalystRunRecord,
    ProposalBacktestRecord,
    RunReportRecord,
)
from trading_bot.application.bot_reporting import AdminReportReadUnitOfWork
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, Principal, Role, authorize

VERDICT_VI = {
    "BETTER": "Tốt hơn",
    "WORSE": "Kém hơn",
    "INCONCLUSIVE": "Chưa kết luận",
    "UNSUPPORTED": "Không backtest được",
}
CONFIDENCE_VI = {"low": "Thấp", "medium": "Trung bình", "high": "Cao"}
MAX_LIST = 100
_WORKSPACE_REPORT_ROLES = frozenset({Role.OWNER, Role.STRATEGY_ADMIN, Role.RISK_ADMIN})
_ANALYST_RUN_VIEW_FIELDS = (
    "id",
    "workspace_id",
    "created_at",
    "topic_key",
    "engine",
    "model",
    "cli_version",
    "session_id",
    "resumed",
    "prompt_version",
    "output_version",
    "run_report_hash",
    "input_sha256",
    "valid",
    "error",
    "exit_code",
    "cost_usd",
    "usage",
    "validation_errors",
    "structured_output",
    "context",
)


class AnalystReviewError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value or () if isinstance(item, Mapping)]


def _output_view(output: Mapping[str, Any] | None) -> dict[str, Any]:
    """Đầu ra AI đã lưu → cấu trúc hiển thị; trường lạ/thiếu thành rỗng (không ném lỗi)."""
    output = output or {}
    return {
        "summary": str(output.get("summary") or ""),
        "confidence": CONFIDENCE_VI.get(str(output.get("confidence")), "—"),
        "findings": [
            {
                "statement": str(item.get("statement") or ""),
                "refs": [str(ref) for ref in item.get("evidence_refs") or ()],
            }
            for item in _items(output.get("findings"))
        ],
        "hypotheses": [
            {
                "statement": str(item.get("statement") or ""),
                "how_to_test": str(item.get("how_to_test") or ""),
                "refs": [str(ref) for ref in item.get("evidence_refs") or ()],
            }
            for item in _items(output.get("hypotheses"))
        ],
        "proposals": [
            {
                "bot_name": str(item.get("bot_name") or ""),
                "changes": [
                    f"{change.get('field')} → {change.get('to')}"
                    for change in _items(item.get("changes"))
                ],
                "rationale": str(item.get("rationale") or ""),
                "risk": str(item.get("risk") or ""),
                "refs": [str(ref) for ref in item.get("evidence_refs") or ()],
            }
            for item in _items(output.get("proposals"))
        ],
    }


def _context_view(context: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Tóm tắt bối cảnh đã đưa AI (lịch vĩ mô, phái sinh, niêm yết); None với lượt cũ."""
    if not context:
        return None
    macro = context.get("macro_calendar") or {}
    derivatives = context.get("derivatives") or {}
    listings = context.get("listings") or {}
    return {
        "macro_events": [
            f"{item.get('vn_date')} {item.get('vn_time')} · {item.get('label_vi')}"
            for item in _items(macro.get("upcoming"))
        ],
        "macro_blackout": (macro.get("state") or {}).get("blackout") is True,
        "derivatives_count": derivatives.get("symbol_count"),
        "derivatives_rows": [
            {
                "symbol": row.get("symbol"),
                "funding": row.get("funding_rate_pct"),
                "oi_change": row.get("oi_change_24h_pct"),
                "long_short": row.get("long_short_account_ratio"),
                "taker": row.get("taker_buy_sell_ratio"),
            }
            for row in _items(derivatives.get("symbols"))[:8]
        ],
        "listing_risks": [
            f"{item.get('symbol')}: {item.get('status')}"
            + (f" — gỡ {item.get('delist_at_vn')} (giờ VN)" if item.get("delist_at_vn") else "")
            for item in _items(listings.get("watched_at_risk"))
        ],
    }


def _backtest_view(row: SimpleNamespace) -> dict[str, Any]:
    result = row.result or {}
    return {
        "id": str(row.id),
        "created_at": row.created_at,
        "proposal_index": row.proposal_index,
        "bot_id": str(row.bot_id) if row.bot_id else None,
        "bot_name": row.bot_name,
        "verdict": row.verdict,
        "verdict_vi": VERDICT_VI.get(row.verdict, row.verdict),
        "changes": [
            f"{change.get('field')} → {change.get('to')}"
            for change in _items((row.proposal or {}).get("changes"))
        ],
        "baseline": dict(result.get("baseline") or {}),
        "variant": dict(result.get("variant") or {}),
        "notes": [str(note) for note in result.get("notes") or ()],
    }


class AnalystReviewService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        report_reader_sessions: async_sessionmaker[AsyncSession] | None = None,
        unit_of_work: Callable[..., Any] = UnitOfWork,
    ) -> None:
        self._sessions = sessions
        self._report_reader_sessions = report_reader_sessions
        self._unit_of_work = unit_of_work

    def _uow(self, principal: Principal) -> Any:
        if not authorize(principal, Permission.WORKSPACE_READ):
            raise AnalystReviewError(403, "Bạn không có quyền xem phân tích AI.")
        if not principal.roles.intersection(_WORKSPACE_REPORT_ROLES):
            raise AnalystReviewError(403, "Vai trò của bạn không được xem báo cáo toàn workspace.")
        if self._report_reader_sessions is None:
            raise AnalystReviewError(503, "Kho dữ liệu báo cáo chưa sẵn sàng.")
        return AdminReportReadUnitOfWork(
            self._sessions,
            self._report_reader_sessions,
            unit_of_work=self._unit_of_work,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        )

    @staticmethod
    def _analyst_run_query() -> Any:
        return select(*(getattr(AnalystRunRecord, name) for name in _ANALYST_RUN_VIEW_FIELDS))

    @staticmethod
    def _analyst_run_rows(result: Any) -> list[SimpleNamespace]:
        return [SimpleNamespace(**dict(row)) for row in result.mappings().all()]

    async def list_runs(
        self,
        principal: Principal,
        *,
        topic: str | None = None,
        valid: bool | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Lượt mới nhất trước + danh sách chủ đề + số backtest đề xuất mỗi lượt."""
        limit = max(1, min(int(limit), MAX_LIST))
        async with self._uow(principal) as unit:
            session = unit.session
            query = self._analyst_run_query().where(
                AnalystRunRecord.workspace_id == principal.workspace_id
            )
            if topic:
                query = query.where(AnalystRunRecord.topic_key == topic)
            if valid is not None:
                query = query.where(AnalystRunRecord.valid.is_(valid))
            rows = self._analyst_run_rows(
                await session.execute(
                    query.order_by(AnalystRunRecord.created_at.desc()).limit(limit)
                )
            )
            topics = list(
                await session.scalars(
                    select(AnalystRunRecord.topic_key)
                    .where(AnalystRunRecord.workspace_id == principal.workspace_id)
                    .distinct()
                    .order_by(AnalystRunRecord.topic_key)
                )
            )
            counts: dict[UUID, int] = {}
            if rows:
                result = await session.execute(
                    select(ProposalBacktestRecord.analyst_run_id, func.count())
                    .where(
                        ProposalBacktestRecord.workspace_id == principal.workspace_id,
                        ProposalBacktestRecord.analyst_run_id.in_([row.id for row in rows]),
                    )
                    .group_by(ProposalBacktestRecord.analyst_run_id)
                )
                counts = {run_id: int(count) for run_id, count in result.all()}
        return {
            "topics": topics,
            "runs": [_run_row(row, counts.get(row.id, 0)) for row in rows],
        }

    async def get_report(self, principal: Principal, report_hash: str) -> dict[str, Any]:
        """Run Report đã lưu (CRY-380) + các lượt AI đã đọc report này."""
        async with self._uow(principal) as unit:
            row = await unit.session.scalar(
                select(RunReportRecord)
                .options(
                    load_only(
                        RunReportRecord.id,
                        RunReportRecord.workspace_id,
                        RunReportRecord.report_hash,
                        RunReportRecord.report_version,
                        RunReportRecord.run_label,
                        RunReportRecord.window_start,
                        RunReportRecord.window_end,
                        RunReportRecord.bot_count,
                        RunReportRecord.payload,
                        RunReportRecord.created_at,
                    )
                )
                .where(
                    RunReportRecord.workspace_id == principal.workspace_id,
                    RunReportRecord.report_hash == report_hash,
                )
            )
            if row is None:
                raise AnalystReviewError(404, "Không tìm thấy Run Report.")
            runs = self._analyst_run_rows(
                await unit.session.execute(
                    self._analyst_run_query()
                    .where(
                        AnalystRunRecord.workspace_id == principal.workspace_id,
                        AnalystRunRecord.run_report_hash == report_hash,
                    )
                    .order_by(AnalystRunRecord.created_at)
                )
            )
            payload = dict(row.payload or {})
        return {
            **_report_view(row),
            "payload": payload,
            "runs": [_run_row(item, 0) for item in runs],
        }

    async def get_run(self, principal: Principal, run_id: UUID) -> dict[str, Any]:
        async with self._uow(principal) as unit:
            result = await unit.session.execute(
                self._analyst_run_query().where(
                    AnalystRunRecord.workspace_id == principal.workspace_id,
                    AnalystRunRecord.id == run_id,
                )
            )
            row = next(iter(self._analyst_run_rows(result)), None)
            if row is None:
                raise AnalystReviewError(404, "Không tìm thấy lượt phân tích.")
            backtest_result = await unit.session.execute(
                select(
                    ProposalBacktestRecord.id,
                    ProposalBacktestRecord.created_at,
                    ProposalBacktestRecord.proposal_index,
                    ProposalBacktestRecord.bot_id,
                    ProposalBacktestRecord.bot_name,
                    ProposalBacktestRecord.verdict,
                    ProposalBacktestRecord.proposal,
                    ProposalBacktestRecord.result,
                )
                .where(
                    ProposalBacktestRecord.workspace_id == principal.workspace_id,
                    ProposalBacktestRecord.analyst_run_id == run_id,
                )
                .order_by(ProposalBacktestRecord.proposal_index)
            )
            backtests = [
                SimpleNamespace(**dict(backtest)) for backtest in backtest_result.mappings().all()
            ]
        usage = row.usage or {}
        return {
            "id": str(row.id),
            "created_at": row.created_at,
            "topic_key": row.topic_key,
            "engine": row.engine,
            "model": row.model,
            "cli_version": row.cli_version,
            "session_id": row.session_id,
            "resumed": row.resumed,
            "prompt_version": row.prompt_version,
            "output_version": row.output_version,
            "run_report_hash": row.run_report_hash,
            "input_sha256": row.input_sha256,
            "valid": row.valid,
            "error": row.error,
            "exit_code": row.exit_code,
            "cost_usd": _money(row.cost_usd),
            "tokens_in": usage.get("input_tokens"),
            "tokens_out": usage.get("output_tokens"),
            "validation_errors": [str(error) for error in row.validation_errors or ()],
            "output": _output_view(row.structured_output),
            "has_output": row.structured_output is not None,
            "context": _context_view(row.context),
            "backtests": [_backtest_view(item) for item in backtests],
        }


def _report_view(row: RunReportRecord) -> dict[str, Any]:
    payload = row.payload or {}
    bots = []
    for bot in _items(payload.get("bots")):
        config = bot.get("config") or {}
        performance = bot.get("performance") or {}
        funnel = bot.get("funnel") or {}
        bots.append(
            {
                "bot_name": bot.get("bot_name"),
                "family": config.get("strategy_family"),
                "timeframe": config.get("timeframe"),
                "candidates": funnel.get("candidates"),
                "closed_trades": performance.get("closed_trades"),
                "net_pnl": performance.get("net_pnl_usdt"),
                "win_rate": performance.get("win_rate_percent"),
            }
        )
    quality = payload.get("data_quality") or {}
    return {
        "report_hash": row.report_hash,
        "report_version": row.report_version,
        "run_label": row.run_label,
        "window_start": row.window_start,
        "window_end": row.window_end,
        "created_at": row.created_at,
        "bots": bots,
        "warnings": [str(item) for item in quality.get("warnings") or ()],
    }


def _run_row(row: SimpleNamespace, backtest_count: int) -> dict[str, Any]:
    output = _output_view(row.structured_output)
    return {
        "id": str(row.id),
        "created_at": row.created_at,
        "topic_key": row.topic_key,
        "engine": row.engine,
        "model": row.model,
        "valid": row.valid,
        "error": row.error,
        "resumed": row.resumed,
        "error_count": len(row.validation_errors or ()),
        "cost_usd": _money(row.cost_usd),
        "summary": output["summary"],
        "proposal_count": len(output["proposals"]),
        "backtest_count": backtest_count,
    }


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.4f}"


__all__ = ["AnalystReviewError", "AnalystReviewService", "VERDICT_VI"]
