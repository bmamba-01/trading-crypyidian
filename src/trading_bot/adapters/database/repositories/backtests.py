"""Lưu replay, trial và evaluation report bất biến theo workspace."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypeVar, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.backtest_entities import (
    BacktestReportRecord,
    BacktestRunRecord,
    BacktestTrialRecord,
)
from trading_bot.application.backtest import (
    EvaluationReport,
    ReplayManifest,
    ReplayRejected,
    ReplayResult,
    TrialRecord,
)


class BacktestRepositoryConflict(RuntimeError):
    """Một khóa idempotency đã gắn với payload khác hoặc lineage sai."""


@dataclass(frozen=True, slots=True)
class BacktestAppendResult:
    """Định danh các bản ghi đã append hoặc đã được nhận diện từ lần trước."""

    run_id: UUID
    trial_record_id: UUID
    report_id: UUID | None
    duplicate: bool


_Record = TypeVar("_Record", BacktestRunRecord, BacktestTrialRecord, BacktestReportRecord)


class BacktestRepository:
    """Transaction do caller quản lý; mọi truy vấn đều mang workspace rõ ràng."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_result(
        self,
        *,
        workspace_id: UUID,
        manifest: ReplayManifest,
        result: ReplayResult,
    ) -> BacktestAppendResult:
        """Append manifest, trial và report; retry cùng nội dung là idempotent."""
        trial = result.trial_record
        if (
            result.manifest_hash != manifest.content_hash
            or trial.manifest_hash != manifest.content_hash
        ):
            raise BacktestRepositoryConflict("Manifest hash không khớp kết quả replay.")
        expected_trial_id = hashlib.sha256(f"trial:{manifest.content_hash}".encode()).hexdigest()
        if trial.trial_id != expected_trial_id:
            raise BacktestRepositoryConflict("Trial ID không khớp manifest.")

        manifest_payload = _plain(asdict(manifest))
        canonical_manifest = _canonical(manifest_payload)
        run, run_duplicate = await self._append_unique(
            model=BacktestRunRecord,
            lookup=(
                BacktestRunRecord.workspace_id == workspace_id,
                BacktestRunRecord.manifest_hash == manifest.content_hash,
            ),
            row=BacktestRunRecord(
                id=uuid4(),
                workspace_id=workspace_id,
                manifest_hash=manifest.content_hash,
                manifest=manifest_payload,
                canonical_manifest=canonical_manifest,
            ),
            canonical_attribute="canonical_manifest",
            canonical_payload=canonical_manifest,
        )

        trial_payload = _trial_payload(trial)
        canonical_trial = _canonical(trial_payload)
        trial_hash = hashlib.sha256(canonical_trial.encode("utf-8")).hexdigest()
        trial_row, trial_duplicate = await self._append_unique(
            model=BacktestTrialRecord,
            lookup=(
                BacktestTrialRecord.workspace_id == workspace_id,
                BacktestTrialRecord.trial_id == trial.trial_id,
            ),
            row=BacktestTrialRecord(
                id=uuid4(),
                workspace_id=workspace_id,
                run_id=run.id,
                trial_id=trial.trial_id,
                manifest_hash=trial.manifest_hash,
                parameter_family_hash=trial.parameter_family_hash,
                trial_index=trial.trial_index,
                trial_count=trial.trial_count,
                status=trial.status,
                observation_count=trial.observation_count,
                completed_observation_count=trial.completed_observation_count,
                trade_count=trial.trade_count,
                error_type=trial.error_type,
                error_detail=_error_detail(result),
                content_hash=trial_hash,
                canonical_trial=canonical_trial,
            ),
            canonical_attribute="canonical_trial",
            canonical_payload=canonical_trial,
        )

        report_id: UUID | None = None
        report_duplicate = True
        if isinstance(result, EvaluationReport):
            report_payload = _report_payload(result)
            canonical_report = _canonical(report_payload)
            report_row, report_duplicate = await self._append_unique(
                model=BacktestReportRecord,
                lookup=(
                    BacktestReportRecord.workspace_id == workspace_id,
                    BacktestReportRecord.report_hash == result.report_hash,
                ),
                row=BacktestReportRecord(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    run_id=run.id,
                    trial_record_id=trial_row.id,
                    manifest_hash=result.manifest_hash,
                    report_hash=result.report_hash,
                    observation_count=result.observation_count,
                    usable_observation_count=result.usable_observation_count,
                    trade_count=result.trade_count,
                    coverage=result.coverage,
                    report=report_payload,
                    canonical_json=canonical_report,
                ),
                canonical_attribute="canonical_json",
                canonical_payload=canonical_report,
            )
            report_id = report_row.id

        return BacktestAppendResult(
            run_id=run.id,
            trial_record_id=trial_row.id,
            report_id=report_id,
            duplicate=run_duplicate and trial_duplicate and report_duplicate,
        )

    async def _append_unique(
        self,
        *,
        model: type[_Record],
        lookup: tuple[Any, ...],
        row: _Record,
        canonical_attribute: str,
        canonical_payload: str,
    ) -> tuple[_Record, bool]:
        existing = await self._session.scalar(select(model).where(*lookup))
        if existing is not None:
            return _validate_existing(existing, canonical_attribute, canonical_payload), True
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as error:
            existing = await self._session.scalar(select(model).where(*lookup))
            if existing is None:
                raise BacktestRepositoryConflict(
                    "Không thể append bản ghi do xung đột unique."
                ) from error
            return _validate_existing(existing, canonical_attribute, canonical_payload), True
        return row, False


def _validate_existing[T: (BacktestRunRecord, BacktestTrialRecord, BacktestReportRecord)](
    row: T, attribute: str, payload: str
) -> T:
    if getattr(row, attribute) != payload:
        raise BacktestRepositoryConflict("Khóa idempotency đã tồn tại với payload khác.")
    if isinstance(row, BacktestTrialRecord):
        stored_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if row.content_hash != stored_hash:
            raise BacktestRepositoryConflict("Hash trial đã lưu không khớp payload.")
    return row


def _trial_payload(trial: TrialRecord) -> dict[str, Any]:
    return cast(dict[str, Any], _plain(asdict(trial)))


def _report_payload(report: EvaluationReport) -> dict[str, Any]:
    return {
        "manifest_hash": report.manifest_hash,
        "report_hash": report.report_hash,
        "observation_count": report.observation_count,
        "usable_observation_count": report.usable_observation_count,
        "trade_count": report.trade_count,
        "coverage": report.coverage,
        "trade_frequency": report.trade_frequency,
        "gross_pnl": str(report.gross_pnl),
        "net_pnl": str(report.net_pnl),
        "fees": str(report.fees),
        "funding": str(report.funding),
        "slippage": str(report.slippage),
        "expectancy": str(report.expectancy),
        "max_drawdown": str(report.max_drawdown),
        "average_exposure": str(report.average_exposure),
        "confidence_interval": (
            [str(item) for item in report.confidence_interval]
            if report.confidence_interval is not None
            else None
        ),
        "caveat_vi": report.caveat_vi,
    }


def _error_detail(result: ReplayResult) -> str | None:
    if isinstance(result, ReplayRejected):
        return f"{result.reason_code}: {result.detail_vi}"
    return None


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
