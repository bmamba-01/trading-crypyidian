"""Repository PostgreSQL cho báo cáo kiểm định scalping SIG-5."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.scalping_validation_entities import (
    ScalpingValidationReportRecord,
)


def _record_to_dict(row: ScalpingValidationReportRecord) -> dict[str, Any]:
    payload = dict(row.report_payload or {})
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "release_id": row.release_id,
        "release_content_hash": row.release_content_hash,
        "symbol": row.symbol,
        "costs_version": row.costs_version,
        "evaluation_start": row.evaluation_start.isoformat(),
        "range_end": row.range_end.isoformat(),
        "recommend_paper": bool(row.recommend_paper),
        "out_of_sample_n": int(row.out_of_sample_n),
        "stress_expectancy_r": row.stress_expectancy_r,
        "validation_hash": row.validation_hash,
        "report_payload": payload,
        "created_by_user_id": (
            str(row.created_by_user_id) if row.created_by_user_id is not None else None
        ),
        "created_at": row.created_at.isoformat(),
    }


class ScalpingValidationReportRepository:
    """Truy vấn và lưu báo cáo kiểm định SIG-5 theo workspace (idempotent theo validation_hash)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_report(
        self,
        *,
        workspace_id: UUID,
        report_body: dict[str, Any],
        actor_user_id: UUID | None,
        now: datetime,
    ) -> dict[str, Any]:
        validation_hash = str(report_body["validation_hash"])
        existing_stmt = select(ScalpingValidationReportRecord).where(
            ScalpingValidationReportRecord.workspace_id == workspace_id,
            ScalpingValidationReportRecord.validation_hash == validation_hash,
        )
        existing = (await self._session.execute(existing_stmt)).scalar_one_or_none()
        if existing is not None:
            return _record_to_dict(existing)

        reports = report_body.get("reports", {})
        stress = reports.get("stress-slippage-x2-taker") or reports.get("base") or {}
        verdict = report_body.get("verdict", {})
        raw_exp = stress.get("expectancy_r")
        record = ScalpingValidationReportRecord(
            id=uuid4(),
            workspace_id=workspace_id,
            release_id=str(report_body["release_id"]),
            release_content_hash=str(report_body["release_content_hash"]),
            symbol=str(report_body["symbol"]),
            costs_version=str(report_body["costs_version"]),
            evaluation_start=datetime.fromisoformat(str(report_body["evaluation_start"])),
            range_end=datetime.fromisoformat(str(report_body["range_end"])),
            recommend_paper=bool(verdict.get("recommend_paper", False)),
            out_of_sample_n=int(stress.get("out_of_sample_n", 0)),
            stress_expectancy_r=None if raw_exp is None else str(raw_exp),
            validation_hash=validation_hash,
            report_payload=dict(report_body),
            created_by_user_id=actor_user_id,
            created_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        return _record_to_dict(record)

    async def list_reports(
        self,
        *,
        workspace_id: UUID,
        release_id: str | None = None,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(ScalpingValidationReportRecord).where(
            ScalpingValidationReportRecord.workspace_id == workspace_id
        )
        if release_id is not None and release_id.strip():
            stmt = stmt.where(ScalpingValidationReportRecord.release_id == release_id.strip())
        if symbol is not None and symbol.strip():
            stmt = stmt.where(ScalpingValidationReportRecord.symbol == symbol.strip())
        stmt = stmt.order_by(ScalpingValidationReportRecord.created_at.desc()).limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_record_to_dict(row) for row in rows]
