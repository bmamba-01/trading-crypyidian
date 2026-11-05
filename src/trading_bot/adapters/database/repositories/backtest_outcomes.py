"""Lưu tham số scenario và giao dịch replay theo hợp đồng bất biến."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.backtest_entities import BacktestTrialRecord
from trading_bot.adapters.database.models.backtest_outcome_entities import (
    BacktestRunDatasetRightsRecord,
    BacktestScenarioRecord,
    BacktestTradeOutcomeRecord,
)
from trading_bot.adapters.database.repositories.backtests import (
    BacktestAppendResult,
    BacktestRepository,
    BacktestRepositoryConflict,
)
from trading_bot.application.backtest import EvaluationReport, ReplayRejected
from trading_bot.application.backtest_dataset_replay_bundle import BacktestDatasetReplayBundle
from trading_bot.application.backtest_sensitivity import SensitivityRun
from trading_bot.application.binance_rest_backfill import BackfillRightsEvidence
from trading_bot.domain.learning.evaluation import EvaluationTrade


@dataclass(frozen=True, slots=True)
class BacktestOutcomeAppendResult:
    """Định danh và hash nội dung đã lưu cho một lượt sensitivity."""

    run_id: UUID
    trial_record_id: UUID
    report_id: UUID | None
    scenario_id: str
    scenario_hash: str
    outcomes_hash: str
    trade_count: int
    duplicate: bool


@dataclass(frozen=True, slots=True)
class PersistedBacktestOutcomes:
    """Kết quả chuẩn hóa đọc lại từ kho cùng hash tổng hợp tính lại."""

    trades: tuple[EvaluationTrade, ...]
    outcomes_hash: str


@dataclass(frozen=True, slots=True)
class PersistedBacktestTradeOutcome:
    """Một trade row đã đọc lại và xác minh hash/canonical payload."""

    record_id: UUID
    workspace_id: UUID
    run_id: UUID
    trial_record_id: UUID
    manifest_hash: str
    ordinal: int
    trade_id: str
    outcome_hash: str
    canonical_json: str
    trade: EvaluationTrade


@dataclass(frozen=True, slots=True)
class PersistedBacktestTradeOutcomeRows:
    """Các trade row của một trial theo ordinal cùng hash tổng hợp."""

    rows: tuple[PersistedBacktestTradeOutcome, ...]
    outcomes_hash: str


class BacktestOutcomeRepository:
    """Transaction do caller quản lý; từng truy vấn đều mang workspace cụ thể."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_sensitivity_run(
        self,
        *,
        workspace_id: UUID,
        sensitivity_run: SensitivityRun,
        trade_outcomes: tuple[EvaluationTrade, ...],
        dataset_bundle: BacktestDatasetReplayBundle | None = None,
    ) -> BacktestOutcomeAppendResult:
        """Lưu sensitivity values và outcomes sau khi kiểm tra report/lineage."""
        manifest = sensitivity_run.manifest
        if dataset_bundle is not None:
            _validate_dataset_bundle(workspace_id, manifest.dataset_hash, dataset_bundle)
        result = sensitivity_run.result
        if result.manifest_hash != manifest.content_hash:
            raise BacktestRepositoryConflict("Manifest sensitivity không khớp kết quả.")
        if isinstance(result, ReplayRejected):
            if trade_outcomes:
                raise ValueError("Replay bị từ chối không được có trade outcome.")
        else:
            _validate_outcomes_against_report(result, trade_outcomes)

        appended: BacktestAppendResult = await BacktestRepository(self._session).append_result(
            workspace_id=workspace_id,
            manifest=manifest,
            result=result,
        )
        trial = await self._session.scalar(
            select(BacktestTrialRecord).where(
                BacktestTrialRecord.workspace_id == workspace_id,
                BacktestTrialRecord.id == appended.trial_record_id,
            )
        )
        if trial is None or trial.trial_id != sensitivity_run.trial_id:
            raise BacktestRepositoryConflict("Trial đã lưu không khớp sensitivity run.")

        scenario_payload = _plain(asdict(sensitivity_run.scenario))
        canonical_scenario = _canonical(scenario_payload)
        scenario_hash = _sha256(canonical_scenario)
        scenario_row, scenario_duplicate = await _append_unique(
            session=self._session,
            model=BacktestScenarioRecord,
            lookup=(
                BacktestScenarioRecord.workspace_id == workspace_id,
                BacktestScenarioRecord.trial_record_id == appended.trial_record_id,
            ),
            row=BacktestScenarioRecord(
                id=uuid4(),
                workspace_id=workspace_id,
                run_id=appended.run_id,
                trial_record_id=appended.trial_record_id,
                manifest_hash=manifest.content_hash,
                scenario_id=sensitivity_run.scenario.scenario_id,
                kind=sensitivity_run.scenario.kind,
                scenario_hash=scenario_hash,
                scenario=scenario_payload,
                canonical_scenario=canonical_scenario,
            ),
            canonical_attribute="canonical_scenario",
            canonical_payload=canonical_scenario,
        )

        ordered_trades = _canonical_trade_order(trade_outcomes)
        outcome_payloads = tuple(_trade_payload(trade) for trade in ordered_trades)
        canonical_outcomes = tuple(_canonical(payload) for payload in outcome_payloads)
        outcomes_hash = _sha256(_canonical(list(canonical_outcomes)))
        outcome_duplicates: list[bool] = []
        for ordinal, (trade, payload, canonical) in enumerate(
            zip(ordered_trades, outcome_payloads, canonical_outcomes, strict=True)
        ):
            row, duplicate = await _append_unique(
                session=self._session,
                model=BacktestTradeOutcomeRecord,
                lookup=(
                    BacktestTradeOutcomeRecord.workspace_id == workspace_id,
                    BacktestTradeOutcomeRecord.trial_record_id == appended.trial_record_id,
                    BacktestTradeOutcomeRecord.ordinal == ordinal,
                ),
                row=BacktestTradeOutcomeRecord(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    run_id=appended.run_id,
                    trial_record_id=appended.trial_record_id,
                    manifest_hash=manifest.content_hash,
                    ordinal=ordinal,
                    trade_id=trade.trade_id,
                    outcome_hash=_sha256(canonical),
                    outcome=payload,
                    canonical_json=canonical,
                ),
                canonical_attribute="canonical_json",
                canonical_payload=canonical,
            )
            if row.trade_id != trade.trade_id:
                raise BacktestRepositoryConflict("Ordinal outcome đã gắn với trade khác.")
            outcome_duplicates.append(duplicate)

        if dataset_bundle is not None:
            await self._append_dataset_rights(
                workspace_id=workspace_id,
                appended=appended,
                bundle=dataset_bundle,
            )

        return BacktestOutcomeAppendResult(
            run_id=appended.run_id,
            trial_record_id=appended.trial_record_id,
            report_id=appended.report_id,
            scenario_id=scenario_row.scenario_id,
            scenario_hash=scenario_row.scenario_hash,
            outcomes_hash=outcomes_hash,
            trade_count=len(ordered_trades),
            duplicate=(appended.duplicate and scenario_duplicate and all(outcome_duplicates)),
        )

    async def _append_dataset_rights(
        self,
        *,
        workspace_id: UUID,
        appended: BacktestAppendResult,
        bundle: BacktestDatasetReplayBundle,
    ) -> None:
        """Ghi đủ biên nhận mới hoặc so toàn bộ bằng chứng cũ, không ghi hồi tố."""
        expected = tuple(
            BacktestRunDatasetRightsRecord.from_bundle_component(
                workspace_id=workspace_id,
                run_id=appended.run_id,
                bundle_hash=bundle.dataset_hash,
                component=component,
            )
            for component in bundle.components
        )
        result = await self._session.scalars(
            select(BacktestRunDatasetRightsRecord).where(
                BacktestRunDatasetRightsRecord.workspace_id == workspace_id,
                BacktestRunDatasetRightsRecord.run_id == appended.run_id,
            )
        )
        existing = tuple(result.all())
        if appended.run_duplicate and not existing:
            # Run cũ không có bằng chứng: giữ nguyên lịch sử thiếu biên nhận.
            return
        if existing:
            expected_by_child = {row.child_dataset_hash: row for row in expected}
            if len(existing) != len(expected) or {
                row.child_dataset_hash for row in existing
            } != set(expected_by_child):
                raise BacktestRepositoryConflict(
                    "Tập biên nhận quyền đã lưu không đầy đủ hoặc khác."
                )
            for row in existing:
                expected_receipt = expected_by_child[row.child_dataset_hash]
                if appended.run_duplicate:
                    _validate_rights_receipt_retry(row, expected_receipt)
                else:
                    _validate_rights_receipt(row, expected_receipt)
            return
        for row in expected:
            self._session.add(row)
        await self._session.flush()

    async def load_trade_outcomes(
        self, *, workspace_id: UUID, trial_record_id: UUID
    ) -> PersistedBacktestOutcomes:
        """Đọc lại outcomes theo thứ tự và tái xác minh hash từng dòng/toàn bộ."""
        persisted = await self.load_trade_outcome_rows(
            workspace_id=workspace_id,
            trial_record_id=trial_record_id,
        )
        return PersistedBacktestOutcomes(
            trades=tuple(row.trade for row in persisted.rows),
            outcomes_hash=persisted.outcomes_hash,
        )

    async def load_trade_outcome_rows(
        self, *, workspace_id: UUID, trial_record_id: UUID
    ) -> PersistedBacktestTradeOutcomeRows:
        """Đọc lại từng row authoritative, giữ UUID, lineage, hash và ordinal."""
        result = await self._session.scalars(
            select(BacktestTradeOutcomeRecord)
            .where(
                BacktestTradeOutcomeRecord.workspace_id == workspace_id,
                BacktestTradeOutcomeRecord.trial_record_id == trial_record_id,
            )
            .order_by(BacktestTradeOutcomeRecord.ordinal)
        )
        rows = tuple(result.all())
        persisted_rows: list[PersistedBacktestTradeOutcome] = []
        canonical_values: list[str] = []
        for expected_ordinal, row in enumerate(rows):
            if row.ordinal != expected_ordinal:
                raise BacktestRepositoryConflict("Dãy outcome bị thiếu hoặc sai ordinal.")
            if row.workspace_id != workspace_id or row.trial_record_id != trial_record_id:
                raise BacktestRepositoryConflict("Outcome row vượt workspace/trial scope.")
            if type(row.outcome) is not dict or set(row.outcome) != {
                "trade_id",
                "opened_at",
                "closed_at",
                "gross_pnl",
                "fees",
                "funding",
                "slippage",
                "exposure",
            }:
                raise BacktestRepositoryConflict("Outcome payload không đúng schema trade.")
            if _canonical(row.outcome) != row.canonical_json:
                raise BacktestRepositoryConflict("Canonical outcome không khớp JSON đã lưu.")
            if _sha256(row.canonical_json) != row.outcome_hash:
                raise BacktestRepositoryConflict("Hash outcome đã lưu không khớp payload.")
            trade = _trade_from_payload(row.outcome)
            if trade.trade_id != row.trade_id or _trade_payload(trade) != row.outcome:
                raise BacktestRepositoryConflict("Trade không khớp payload outcome chuẩn hóa.")
            persisted_rows.append(
                PersistedBacktestTradeOutcome(
                    record_id=row.id,
                    workspace_id=row.workspace_id,
                    run_id=row.run_id,
                    trial_record_id=row.trial_record_id,
                    manifest_hash=row.manifest_hash,
                    ordinal=row.ordinal,
                    trade_id=row.trade_id,
                    outcome_hash=row.outcome_hash,
                    canonical_json=row.canonical_json,
                    trade=trade,
                )
            )
            canonical_values.append(row.canonical_json)
        canonical_order = tuple(
            sorted(
                persisted_rows,
                key=lambda item: (item.trade.closed_at, item.trade.trade_id),
            )
        )
        if tuple(item.record_id for item in persisted_rows) != tuple(
            item.record_id for item in canonical_order
        ):
            raise BacktestRepositoryConflict("Ordinal outcome không theo thứ tự trade canonical.")
        return PersistedBacktestTradeOutcomeRows(
            rows=tuple(persisted_rows),
            outcomes_hash=_sha256(_canonical(canonical_values)),
        )

    async def load_trade_outcome_rows_by_id(
        self,
        *,
        workspace_id: UUID,
        trial_record_id: UUID,
        outcome_record_ids: set[UUID],
    ) -> PersistedBacktestTradeOutcomeRows:
        """Đọc đúng các outcome UUID được yêu cầu, không tải payload khác."""
        if not outcome_record_ids or any(type(item) is not UUID for item in outcome_record_ids):
            raise ValueError("outcome_record_ids phải là tập UUID không rỗng.")
        result = await self._session.scalars(
            select(BacktestTradeOutcomeRecord)
            .where(
                BacktestTradeOutcomeRecord.workspace_id == workspace_id,
                BacktestTradeOutcomeRecord.trial_record_id == trial_record_id,
                BacktestTradeOutcomeRecord.id.in_(outcome_record_ids),
            )
            .order_by(BacktestTradeOutcomeRecord.ordinal)
        )
        rows = tuple(result.all())
        if {row.id for row in rows} != outcome_record_ids or len(rows) != len(outcome_record_ids):
            raise BacktestRepositoryConflict(
                "Không tìm thấy đủ outcome UUID trong trial/workspace."
            )

        persisted_rows: list[PersistedBacktestTradeOutcome] = []
        canonical_values: list[str] = []
        for row in rows:
            if row.workspace_id != workspace_id or row.trial_record_id != trial_record_id:
                raise BacktestRepositoryConflict("Outcome row vượt workspace/trial scope.")
            if type(row.outcome) is not dict or set(row.outcome) != {
                "trade_id",
                "opened_at",
                "closed_at",
                "gross_pnl",
                "fees",
                "funding",
                "slippage",
                "exposure",
            }:
                raise BacktestRepositoryConflict("Outcome payload không đúng schema trade.")
            if _canonical(row.outcome) != row.canonical_json:
                raise BacktestRepositoryConflict("Canonical outcome không khớp JSON đã lưu.")
            if _sha256(row.canonical_json) != row.outcome_hash:
                raise BacktestRepositoryConflict("Hash outcome đã lưu không khớp payload.")
            trade = _trade_from_payload(row.outcome)
            if trade.trade_id != row.trade_id or _trade_payload(trade) != row.outcome:
                raise BacktestRepositoryConflict("Trade không khớp payload outcome chuẩn hóa.")
            persisted_rows.append(
                PersistedBacktestTradeOutcome(
                    record_id=row.id,
                    workspace_id=row.workspace_id,
                    run_id=row.run_id,
                    trial_record_id=row.trial_record_id,
                    manifest_hash=row.manifest_hash,
                    ordinal=row.ordinal,
                    trade_id=row.trade_id,
                    outcome_hash=row.outcome_hash,
                    canonical_json=row.canonical_json,
                    trade=trade,
                )
            )
            canonical_values.append(row.canonical_json)
        return PersistedBacktestTradeOutcomeRows(
            rows=tuple(persisted_rows),
            outcomes_hash=_sha256(_canonical(canonical_values)),
        )


def _validate_dataset_bundle(
    workspace_id: UUID, dataset_hash: str, bundle: BacktestDatasetReplayBundle
) -> None:
    """Kiểm scope và snapshot quyền đầy đủ trước bất kỳ thao tác ghi outcome."""
    if type(bundle) is not BacktestDatasetReplayBundle:
        raise ValueError("dataset_bundle phải là BacktestDatasetReplayBundle đã xác thực.")
    if bundle.workspace_id != workspace_id:
        raise BacktestRepositoryConflict("Bundle không khớp workspace của outcome.")
    if bundle.dataset_hash != dataset_hash:
        raise BacktestRepositoryConflict("Hash bundle không khớp manifest sensitivity.")
    for component in bundle.components:
        manifest = component.manifest
        manifest.require_ready()
        rights = component.current_rights
        if type(rights) is not BackfillRightsEvidence or (
            manifest.rights_workspace_id != workspace_id
            or rights.workspace_id != workspace_id
            or rights.provider != manifest.rights_provider
            or rights.purpose.value != manifest.rights_purpose
            or rights.policy_fingerprint != manifest.policy_fingerprint
        ):
            raise BacktestRepositoryConflict("Bằng chứng quyền không khớp scope/policy manifest.")
        if rights.checked_at is None:
            raise ValueError("checked_at từ đồng hồ database là bắt buộc cho biên nhận quyền.")


def _validate_rights_receipt(
    row: BacktestRunDatasetRightsRecord, expected: BacktestRunDatasetRightsRecord
) -> None:
    """So canonical, JSONB, hash và cột chỉ mục để từ chối bằng chứng bị đổi."""
    _validate_stored_rights_receipt_integrity(row)
    fields = (
        "workspace_id",
        "run_id",
        "bundle_hash",
        "child_dataset_hash",
        "interval",
        "provider",
        "purpose",
        "terms_url",
        "policy_fingerprint",
        "verified_at",
        "checked_at",
        "valid_until",
        "canonical_evidence",
        "evidence_hash",
    )
    if (
        any(getattr(row, field) != getattr(expected, field) for field in fields)
        or row.canonical_evidence != expected.canonical_evidence
    ):
        raise BacktestRepositoryConflict("Biên nhận quyền đã lưu không khớp bằng chứng lần chạy.")


def _validate_rights_receipt_retry(
    row: BacktestRunDatasetRightsRecord, current: BacktestRunDatasetRightsRecord
) -> None:
    """Giữ receipt đầu khi retry deterministic đã kiểm lại policy hiện hành."""
    _validate_stored_rights_receipt_integrity(row)
    policy_bound_fields = (
        "workspace_id",
        "run_id",
        "bundle_hash",
        "child_dataset_hash",
        "interval",
        "provider",
        "purpose",
        "terms_url",
        "policy_fingerprint",
        "verified_at",
    )
    if any(getattr(row, field) != getattr(current, field) for field in policy_bound_fields):
        raise BacktestRepositoryConflict(
            "Receipt của run cũ không khớp scope hoặc policy quyền hiện hành."
        )


def _validate_stored_rights_receipt_integrity(row: BacktestRunDatasetRightsRecord) -> None:
    """Xác minh row, JSONB, canonical text và hash của snapshot quyền đầu tiên."""
    expected_payload = {
        "schema_version": 1,
        "workspace_id": str(row.workspace_id),
        "run_id": str(row.run_id),
        "bundle_hash": row.bundle_hash,
        "child_dataset_hash": row.child_dataset_hash,
        "interval": row.interval,
        "provider": row.provider,
        "purpose": row.purpose,
        "terms_url": row.terms_url,
        "policy_fingerprint": row.policy_fingerprint,
        "verified_at": row.verified_at.isoformat(),
        "checked_at": row.checked_at.isoformat(),
        "valid_until": row.valid_until.isoformat(),
    }
    if (
        type(row.evidence_json) is not dict
        or row.evidence_json != expected_payload
        or _canonical(row.evidence_json) != row.canonical_evidence
        or _sha256(row.canonical_evidence) != row.evidence_hash
    ):
        raise BacktestRepositoryConflict("Biên nhận quyền đã lưu sai canonical hoặc hash.")


async def _append_unique(
    *,
    session: AsyncSession,
    model: type[Any],
    lookup: tuple[Any, ...],
    row: Any,
    canonical_attribute: str,
    canonical_payload: str,
) -> tuple[Any, bool]:
    existing = await session.scalar(select(model).where(*lookup))
    if existing is not None:
        _validate_existing(existing, canonical_attribute, canonical_payload)
        return existing, True
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as error:
        existing = await session.scalar(select(model).where(*lookup))
        if existing is None:
            raise BacktestRepositoryConflict("Xung đột unique khi lưu replay outcome.") from error
        _validate_existing(existing, canonical_attribute, canonical_payload)
        return existing, True
    return row, False


def _validate_existing(row: Any, attribute: str, payload: str) -> None:
    if getattr(row, attribute) != payload:
        raise BacktestRepositoryConflict("Khóa idempotency đã tồn tại với payload khác.")


def _validate_outcomes_against_report(
    report: EvaluationReport, trades: tuple[EvaluationTrade, ...]
) -> None:
    ordered = _canonical_trade_order(trades)
    if len(ordered) != report.trade_count:
        raise ValueError("Trade outcomes không khớp report: trade_count khác.")
    totals = {
        "gross_pnl": sum((item.gross_pnl for item in ordered), Decimal("0")),
        "fees": sum((item.fees for item in ordered), Decimal("0")),
        "funding": sum((item.funding for item in ordered), Decimal("0")),
        "slippage": sum((item.slippage for item in ordered), Decimal("0")),
        "net_pnl": sum((item.net_pnl for item in ordered), Decimal("0")),
    }
    expected = {
        "gross_pnl": report.gross_pnl,
        "fees": report.fees,
        "funding": report.funding,
        "slippage": report.slippage,
        "net_pnl": report.net_pnl,
    }
    if totals != expected:
        raise ValueError("Trade outcomes không khớp report: tổng PnL/chi phí khác.")


def _canonical_trade_order(trades: tuple[EvaluationTrade, ...]) -> tuple[EvaluationTrade, ...]:
    if not isinstance(trades, tuple):
        raise ValueError("trade_outcomes phải là tuple bất biến.")
    trade_ids = tuple(item.trade_id for item in trades)
    if len(set(trade_ids)) != len(trade_ids):
        raise ValueError("trade_outcomes có trade_id trùng.")
    return tuple(sorted(trades, key=lambda item: (item.closed_at, item.trade_id)))


def _trade_payload(trade: EvaluationTrade) -> dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "opened_at": trade.opened_at.isoformat(),
        "closed_at": trade.closed_at.isoformat(),
        "gross_pnl": str(trade.gross_pnl),
        "fees": str(trade.fees),
        "funding": str(trade.funding),
        "slippage": str(trade.slippage),
        "exposure": str(trade.exposure),
    }


def _trade_from_payload(payload: dict[str, Any]) -> EvaluationTrade:
    return EvaluationTrade(
        trade_id=str(payload["trade_id"]),
        opened_at=datetime.fromisoformat(str(payload["opened_at"])),
        closed_at=datetime.fromisoformat(str(payload["closed_at"])),
        gross_pnl=Decimal(str(payload["gross_pnl"])),
        fees=Decimal(str(payload["fees"])),
        funding=Decimal(str(payload["funding"])),
        slippage=Decimal(str(payload["slippage"])),
        exposure=Decimal(str(payload["exposure"])),
    )


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
