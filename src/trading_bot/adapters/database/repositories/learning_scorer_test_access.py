"""Cấp quyền TEST scorer một lần và lưu report trong cùng transaction tiêu thụ."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models.backtest_outcome_entities import (
    BacktestTradeOutcomeRecord,
)
from trading_bot.adapters.database.models.learning_fold_sample_entities import (
    NormalizedLearningSampleRecord,
)
from trading_bot.adapters.database.models.learning_scorer_test_access_entities import (
    LearningScorerTestAccessReceiptRecord,
    LearningScorerTestEvaluationReportRecord,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import (
    _validate_trade_outcome_row,
)
from trading_bot.application.learning_scorer_evaluation import (
    ScorerPortfolioSummary,
    ScorerReliabilityBin,
    ScorerTestEvaluationReport,
    ScorerTestEvaluationScope,
    ScorerTestRow,
    scorer_test_payload_commitment_hash,
    scorer_test_payload_membership_digest,
)
from trading_bot.application.learning_scorer_test_service import (
    ScorerTestAccessReceipt,
    ScorerTestEvaluationAccessConflict,
    ScorerTestEvaluationReportConflict,
    ScorerTestPayloadConsumption,
    ScorerTestStoredReport,
)
from trading_bot.application.outbox import UnitOfWork

_REPORT_LOCK_PREFIX = "wp06-scorer-test-evaluation"


class LearningScorerTestAccessRepository:
    """Adapter cho receipt TEST và gateway payload/report atomic theo workspace."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def open_committed_receipt(
        self,
        scope: ScorerTestEvaluationScope,
        *,
        scorer_artifact_hash: str,
        policy_hash: str,
        request_fingerprint: str,
    ) -> ScorerTestAccessReceipt:
        """Kiểm quyền hiện hành, ghi receipt scope-only rồi commit trước khi trả."""
        if type(scope) is not ScorerTestEvaluationScope:
            raise ValueError("Cần scope scorer TEST authoritative.")
        if not _is_sha256(scorer_artifact_hash, policy_hash, request_fingerprint):
            raise ValueError("Artifact, policy và request fingerprint phải là SHA-256.")

        async with UnitOfWork(self._sessions, workspace_id=scope.workspace_id) as uow:
            await self._lock_sources(
                uow.session,
                workspace_id=scope.workspace_id,
                normalized_dataset_id=scope.normalized_dataset_id,
            )
            current_scope_digest = await _current_training_scope_digest(
                uow.session,
                workspace_id=scope.workspace_id,
                bundle_hash=scope.bundle_hash,
            )
            try:
                result = await uow.session.execute(
                    text(
                        "SELECT * FROM public.wp06_open_scorer_test_access_receipt("
                        "target_workspace => CAST(:workspace_id AS uuid), "
                        "target_evaluation_run_id => CAST(:evaluation_run_id AS uuid), "
                        "target_attempt_id => CAST(:attempt_id AS uuid), "
                        "target_normalized_dataset_id => CAST(:normalized_dataset_id AS uuid), "
                        "target_proof_id => CAST(:proof_id AS uuid), "
                        "target_scorer_artifact_hash => CAST(:scorer_artifact_hash AS text), "
                        "target_policy_hash => CAST(:policy_hash AS text), "
                        "target_request_fingerprint => CAST(:request_fingerprint AS text), "
                        "target_training_scope_digest => CAST(:training_scope_digest AS text))"
                    ),
                    {
                        "workspace_id": str(scope.workspace_id),
                        "evaluation_run_id": str(scope.evaluation_run_id),
                        "attempt_id": str(scope.attempt_id),
                        "normalized_dataset_id": str(scope.normalized_dataset_id),
                        "proof_id": str(scope.opportunity_proof_id),
                        "scorer_artifact_hash": scorer_artifact_hash,
                        "policy_hash": policy_hash,
                        "request_fingerprint": request_fingerprint,
                        "training_scope_digest": current_scope_digest,
                    },
                )
                row = result.mappings().one()
                receipt = _receipt_from_mapping(
                    cast(Mapping[str, Any], row),
                    workspace_id=scope.workspace_id,
                )
            except DBAPIError as error:
                _raise_access_error(error)

        # Receipt chỉ được trao khi UnitOfWork đã commit thành công.
        if receipt.committed_at is None:
            raise ScorerTestEvaluationAccessConflict("Receipt TEST chưa commit.")
        return receipt

    async def evaluate_and_persist(
        self,
        receipt: ScorerTestAccessReceipt,
        *,
        request_fingerprint: str,
        evaluate_payload: Any,
    ) -> ScorerTestStoredReport:
        """Recheck quyền, tiêu thụ TEST, tính và append report trước một lần commit."""
        if (
            type(receipt) is not ScorerTestAccessReceipt
            or type(receipt.workspace_id) is not UUID
            or type(receipt.receipt_id) is not UUID
            or not _is_sha256(request_fingerprint)
            or receipt.request_fingerprint != request_fingerprint
            or not callable(evaluate_payload)
        ):
            raise ScorerTestEvaluationAccessConflict("Receipt hoặc fingerprint TEST không hợp lệ.")

        stored: ScorerTestStoredReport | None = None
        async with UnitOfWork(self._sessions, workspace_id=receipt.workspace_id) as uow:
            session = uow.session
            receipt_row = await session.scalar(
                select(LearningScorerTestAccessReceiptRecord).where(
                    LearningScorerTestAccessReceiptRecord.workspace_id == receipt.workspace_id,
                    LearningScorerTestAccessReceiptRecord.id == receipt.receipt_id,
                )
            )
            if receipt_row is None:
                raise ScorerTestEvaluationAccessConflict("Không tìm thấy receipt TEST.")
            self._validate_receipt_row(receipt_row, receipt, request_fingerprint)
            await self._lock_sources(
                session,
                workspace_id=receipt.workspace_id,
                normalized_dataset_id=receipt_row.normalized_dataset_id,
            )
            await session.execute(
                text(
                    "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 0))"
                ),
                {
                    "key": (
                        f"{_REPORT_LOCK_PREFIX}:{receipt.workspace_id}:"
                        f"{receipt.proof_id}:{receipt.receipt_id}"
                    )
                },
            )
            existing = await _get_report_for_receipt(
                session,
                workspace_id=receipt.workspace_id,
                receipt_id=receipt.receipt_id,
            )
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise ScorerTestEvaluationReportConflict(
                        "Report cùng receipt có request fingerprint khác."
                    )
                stored = _stored_report(existing, is_duplicate=True)
            else:
                current_scope_digest = await _current_training_scope_digest(
                    session,
                    workspace_id=receipt.workspace_id,
                    bundle_hash=receipt_row.bundle_hash,
                )
                try:
                    result = await session.execute(
                        text(
                            "SELECT * FROM public.wp06_consume_scorer_test_payload("
                            "target_workspace => CAST(:workspace_id AS uuid), "
                            "target_receipt_id => CAST(:receipt_id AS uuid), "
                            "target_training_scope_digest => "
                            "CAST(:training_scope_digest AS text))"
                        ),
                        {
                            "workspace_id": str(receipt.workspace_id),
                            "receipt_id": str(receipt.receipt_id),
                            "training_scope_digest": current_scope_digest,
                        },
                    )
                    raw_rows = cast(
                        list[Mapping[str, Any]],
                        list(result.mappings().all()),
                    )
                    consumption = _consumption_from_rows(
                        raw_rows,
                        receipt=receipt,
                        request_fingerprint=request_fingerprint,
                        manifest_hash=receipt_row.manifest_hash,
                        fold_plan_hash=receipt_row.fold_plan_hash,
                        expected_membership_digest=receipt_row.test_membership_digest,
                        expected_current_rights_hash=receipt.current_rights_receipt_hash,
                    )
                    report = evaluate_payload(consumption)
                    if type(report) is not ScorerTestEvaluationReport:
                        raise ScorerTestEvaluationReportConflict(
                            "Evaluator callback không trả report scorer TEST typed."
                        )
                    report_record = LearningScorerTestEvaluationReportRecord.from_domain(
                        report,
                        request_fingerprint=request_fingerprint,
                    )
                    session.add(report_record)
                    await session.flush()
                    stored = _stored_report(report_record, is_duplicate=False)
                except DBAPIError as error:
                    _raise_access_error(error)

        if stored is None:
            raise ScorerTestEvaluationReportConflict("Gateway không trả report đã commit.")
        return stored

    @staticmethod
    def _validate_receipt_row(
        row: LearningScorerTestAccessReceiptRecord,
        receipt: ScorerTestAccessReceipt,
        request_fingerprint: str,
    ) -> None:
        expected = (
            row.id == receipt.receipt_id,
            row.workspace_id == receipt.workspace_id,
            row.request_fingerprint == request_fingerprint,
            row.receipt_hash == receipt.receipt_hash,
            row.proof_id == receipt.proof_id,
            row.proof_hash == receipt.proof_hash,
            row.scorer_artifact_hash == receipt.scorer_artifact_hash,
            row.policy_hash == receipt.policy_hash,
            row.current_rights_receipt_hash == receipt.current_rights_receipt_hash,
        )
        if not all(expected):
            raise ScorerTestEvaluationAccessConflict(
                "Receipt lưu trữ không khớp scope/fingerprint đã commit."
            )

    @staticmethod
    async def _lock_sources(
        session: AsyncSession,
        *,
        workspace_id: UUID,
        normalized_dataset_id: UUID,
    ) -> None:
        await session.execute(
            text(
                "SELECT public.wp06_lock_scorer_test_sources("
                "target_workspace => CAST(:workspace_id AS uuid), "
                "target_normalized_dataset_id => CAST(:normalized_dataset_id AS uuid))"
            ),
            {
                "workspace_id": str(workspace_id),
                "normalized_dataset_id": str(normalized_dataset_id),
            },
        )


class LearningScorerTestEvaluationReportRepository:
    """Đọc lại report metadata để retry không mở lại payload TEST."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_by_run(
        self,
        *,
        workspace_id: UUID,
        evaluation_run_id: UUID,
    ) -> ScorerTestStoredReport | None:
        if type(workspace_id) is not UUID or type(evaluation_run_id) is not UUID:
            raise ValueError("Workspace và evaluation run phải là UUID.")
        async with UnitOfWork(
            self._sessions,
            workspace_id=workspace_id,
            snapshot_read_only=True,
        ) as uow:
            row = await uow.session.scalar(
                select(LearningScorerTestEvaluationReportRecord).where(
                    LearningScorerTestEvaluationReportRecord.workspace_id == workspace_id,
                    LearningScorerTestEvaluationReportRecord.evaluation_run_id == evaluation_run_id,
                )
            )
            if row is None:
                return None
            return _stored_report(row, is_duplicate=False)


async def _current_training_scope_digest(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    bundle_hash: str,
) -> str:
    """Lấy quyền TRAINING hiện hành cho mọi child của bundle trong transaction."""
    from trading_bot.adapters.database.replay_training_rights import (
        DatabaseReplayTrainingRightsGate,
        ReplayTrainingOperation,
        training_scope_digest,
    )

    try:
        receipts = await DatabaseReplayTrainingRightsGate(session).authorize(
            workspace_id=workspace_id,
            bundle_hash=bundle_hash,
            operation=ReplayTrainingOperation.EVALUATION,
        )
        if not receipts or any(
            item.workspace_id != workspace_id
            or item.bundle_hash != bundle_hash
            or item.purpose.value != "training"
            for item in receipts
        ):
            raise PermissionError("Bundle không có đủ quyền TRAINING hiện hành.")
        digest = training_scope_digest(receipts)
    except (DBAPIError, LookupError, PermissionError, ValueError, TypeError) as error:
        raise ScorerTestEvaluationAccessConflict(
            "Không xác minh được quyền TRAINING hiện hành cho bundle TEST."
        ) from error
    if not _is_sha256(digest):
        raise ScorerTestEvaluationAccessConflict("Rights scope digest không hợp lệ.")
    return digest


def _receipt_from_mapping(row: Mapping[str, Any], *, workspace_id: UUID) -> ScorerTestAccessReceipt:
    try:
        returned_workspace_id = row["workspace_id"]
        if type(returned_workspace_id) is not UUID or returned_workspace_id != workspace_id:
            raise ValueError("Gateway trả receipt khác workspace request.")
        receipt = ScorerTestAccessReceipt(
            receipt_id=row["receipt_id"],
            workspace_id=returned_workspace_id,
            receipt_hash=row["receipt_hash"],
            request_fingerprint=row["request_fingerprint"],
            scorer_artifact_hash=row["scorer_artifact_hash"],
            policy_hash=row["policy_hash"],
            proof_id=row["proof_id"],
            proof_hash=row["proof_hash"],
            current_rights_receipt_hash=row["current_rights_receipt_hash"],
            committed_at=row["committed_at"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ScorerTestEvaluationAccessConflict(
            "Gateway trả metadata receipt TEST không hợp lệ."
        ) from error
    if (
        type(receipt.receipt_id) is not UUID
        or not _is_sha256(
            receipt.receipt_hash,
            receipt.request_fingerprint,
            receipt.scorer_artifact_hash,
            receipt.policy_hash,
            receipt.proof_hash,
            receipt.current_rights_receipt_hash,
        )
        or type(receipt.proof_id) is not UUID
        or not isinstance(receipt.committed_at, datetime)
        or receipt.committed_at.tzinfo is None
        or receipt.committed_at.utcoffset() is None
    ):
        raise ScorerTestEvaluationAccessConflict("Gateway trả receipt TEST chưa commit/hợp lệ.")
    return receipt


def _consumption_from_rows(
    raw_rows: list[Mapping[str, Any]],
    *,
    receipt: ScorerTestAccessReceipt,
    request_fingerprint: str,
    manifest_hash: str,
    fold_plan_hash: str,
    expected_membership_digest: str,
    expected_current_rights_hash: str,
) -> ScorerTestPayloadConsumption:
    """Kiểm metadata lặp, sample/outcome link và hash trước khi evaluator nhận batch."""
    if not raw_rows:
        raise ScorerTestEvaluationAccessConflict("Gateway không trả đúng payload TEST đã duyệt.")
    try:
        metadata = tuple(
            (
                item["consumption_id"],
                item["receipt_id"],
                item["workspace_id"],
                item["request_fingerprint"],
                item["payload_count"],
                item["membership_digest"],
                item["payload_commitment_hash"],
                item["consumption_hash"],
                item["current_rights_receipt_hash"],
                item["consumed_at"],
            )
            for item in raw_rows
        )
        if len(set(metadata)) != 1:
            raise ValueError("Consumption metadata không đồng nhất giữa các hàng.")
        (
            consumption_id,
            receipt_id,
            workspace_id,
            returned_fingerprint,
            payload_count,
            membership_digest,
            payload_commitment_hash,
            consumption_hash,
            current_rights_hash,
            consumed_at,
        ) = metadata[0]
        if (
            type(consumption_id) is not UUID
            or receipt_id != receipt.receipt_id
            or workspace_id != receipt.workspace_id
            or returned_fingerprint != request_fingerprint
            or type(payload_count) is not int
            or payload_count != len(raw_rows)
            or membership_digest != expected_membership_digest
            or current_rights_hash != expected_current_rights_hash
            or not _is_sha256(
                membership_digest,
                payload_commitment_hash,
                consumption_hash,
                current_rights_hash,
            )
            or not isinstance(consumed_at, datetime)
            or consumed_at.tzinfo is None
            or consumed_at.utcoffset() is None
        ):
            raise ValueError("Metadata consumption không khớp quyền hoặc receipt.")

        rows = tuple(
            _scorer_test_row(
                item,
                workspace_id=receipt.workspace_id,
                manifest_hash=manifest_hash,
                fold_plan_hash=fold_plan_hash,
            )
            for item in raw_rows
        )
        if scorer_test_payload_membership_digest(rows) != membership_digest:
            raise ValueError("Membership digest không khớp sample/vector đã nạp.")
        if scorer_test_payload_commitment_hash(rows) != payload_commitment_hash:
            raise ValueError("Payload commitment không khớp candidate/outcome/sample/vector.")
        return ScorerTestPayloadConsumption(
            consumption_id=consumption_id,
            workspace_id=workspace_id,
            receipt_id=receipt_id,
            request_fingerprint=returned_fingerprint,
            payload_count=payload_count,
            membership_digest=membership_digest,
            payload_commitment_hash=payload_commitment_hash,
            consumption_hash=consumption_hash,
            current_rights_receipt_hash=current_rights_hash,
            consumed_at=consumed_at.astimezone(UTC),
            rows=rows,
        )
    except ScorerTestEvaluationAccessConflict:
        raise
    except (ArithmeticError, KeyError, TypeError, ValueError) as error:
        raise ScorerTestEvaluationAccessConflict(
            "TEST payload không khớp metadata, membership hoặc outcome authoritative."
        ) from error


def _scorer_test_row(
    row: Mapping[str, Any],
    *,
    workspace_id: UUID,
    manifest_hash: str,
    fold_plan_hash: str,
) -> ScorerTestRow:
    """Tái xác minh sample JSONB và trade outcome canonical trước khi tạo row typed."""
    sample_id = row["sample_id"]
    sample_outcome_id = _uuid(row["sample_outcome_id"])
    outcome_id = _uuid(row["outcome_id"])
    sample_outcome_hash = row["sample_outcome_hash"]
    outcome_hash = row["outcome_hash"]
    if sample_outcome_id != outcome_id or sample_outcome_hash != outcome_hash:
        raise ValueError("Normalized sample không trỏ đúng outcome đã đóng.")

    sample_record = NormalizedLearningSampleRecord(
        workspace_id=workspace_id,
        manifest_hash=manifest_hash,
        fold_plan_hash=fold_plan_hash,
        sample_id=sample_id,
        outcome_id=str(sample_outcome_id),
        outcome_hash=sample_outcome_hash,
        feature_snapshot_hash=row["feature_snapshot_hash"],
        feature_schema_hash=row["feature_schema_hash"],
        label_hash=row["label_hash"],
        label_value=row["label_value"],
        features=row["features"],
        sample_hash=row["sample_hash"],
    )
    sample = sample_record.to_domain()

    trade_payload = row["outcome_payload"]
    if type(trade_payload) is not dict:
        raise ValueError("Outcome TEST phải là JSON object.")
    outcome_payload_canonical = row["outcome_payload_canonical"]
    if not isinstance(outcome_payload_canonical, str) or outcome_payload_canonical != _canonical(
        trade_payload
    ):
        raise ValueError("Outcome TEST không khớp JSON canonical trong DB.")
    outcome = SimpleNamespace(
        workspace_id=workspace_id,
        id=outcome_id,
        trade_id=row["trade_id"],
        outcome_hash=outcome_hash,
        outcome=trade_payload,
        canonical_json=outcome_payload_canonical,
        entry_decision_snapshot_id=trade_payload.get("entry_decision_snapshot_id"),
        entry_decision_at=_optional_datetime(trade_payload.get("entry_decision_at")),
        entry_feature_schema_hash=trade_payload.get("entry_feature_schema_hash"),
        entry_features=trade_payload.get("entry_features"),
        entry_features_hash=trade_payload.get("entry_features_hash"),
        entry_symbol=trade_payload.get("entry_symbol"),
        entry_opportunity_event_id=trade_payload.get("entry_opportunity_event_id"),
        entry_candidate_id=trade_payload.get("entry_candidate_id"),
        entry_vector_hash=trade_payload.get("entry_vector_hash"),
    )
    trade = _validate_trade_outcome_row(cast(BacktestTradeOutcomeRecord, outcome))
    vector_hash = row["vector_hash"]
    candidate_id_hash = row["candidate_id_hash"]
    if (
        not _is_sha256(vector_hash, candidate_id_hash)
        or trade.entry_vector_hash != vector_hash
        or trade.entry_candidate_id != candidate_id_hash
        or trade.trade_id != row["trade_id"]
    ):
        raise ValueError("Outcome không khớp candidate/vector hash của TEST proof.")
    if (
        sample.feature_snapshot_hash != trade.entry_features_hash
        or sample.feature_schema_hash != trade.entry_feature_schema_hash
    ):
        raise ValueError("Feature snapshot/schema của TEST sample không khớp outcome entry.")
    if dict(sample.features) != dict(trade.entry_features):
        raise ValueError("Feature của TEST sample không khớp vector outcome entry.")
    outcome_label = int(trade.net_pnl > 0) - int(trade.net_pnl < 0)
    if sample.label_value != outcome_label:
        raise ValueError("Label của TEST sample không khớp dấu net PnL outcome.")
    return ScorerTestRow(
        sample_id=sample.sample_id,
        outcome_id=outcome_id,
        outcome_hash=sample.outcome_hash,
        role="test",
        sample_hash=sample_record.sample_hash,
        vector_hash=vector_hash,
        candidate_id_hash=candidate_id_hash,
        features=sample.features,
        label_value=sample.label_value,
        trade=trade,
    )


async def _get_report_for_receipt(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    receipt_id: UUID,
) -> LearningScorerTestEvaluationReportRecord | None:
    return await session.scalar(
        select(LearningScorerTestEvaluationReportRecord).where(
            LearningScorerTestEvaluationReportRecord.workspace_id == workspace_id,
            LearningScorerTestEvaluationReportRecord.access_receipt_id == receipt_id,
        )
    )


def _stored_report(
    row: LearningScorerTestEvaluationReportRecord,
    *,
    is_duplicate: bool,
) -> ScorerTestStoredReport:
    try:
        report = scorer_test_evaluation_report_from_canonical_json(
            row.canonical_report,
            expected_report_hash=row.report_hash,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ScorerTestEvaluationReportConflict(
            "Report scorer TEST lưu sai canonical bytes hoặc metadata."
        ) from error
    denormalized = (
        row.workspace_id == report.workspace_id,
        row.evaluation_run_id == report.evaluation_run_id,
        row.proof_id == report.opportunity_proof_id,
        row.report_hash == report.report_hash,
        row.evaluation_status == report.status,
        row.scorer_artifact_hash == report.scorer_artifact_hash,
        row.base_artifact_hash == report.base_artifact_hash,
        row.policy_hash == report.policy_hash,
        row.policy_version == report.policy_version,
        row.access_receipt_id == report.test_access_receipt_id,
        row.access_receipt_hash == report.test_access_receipt_hash,
        row.current_rights_receipt_hash == report.test_current_rights_receipt_hash,
        row.payload_consumption_id == report.test_payload_consumption_id,
        row.payload_commitment_hash == report.test_payload_commitment_hash,
        row.payload_consumption_hash == report.test_payload_consumption_hash,
        row.payload_consumed_at == report.test_payload_consumed_at,
        row.candidate_completeness_verified == report.candidate_completeness_verified,
        row.scored_candidate_coverage_verified == report.scored_candidate_coverage_verified,
        row.test_sample_count == report.test_sample_count,
        row.opportunity_count == report.opportunity_count,
        row.request_fingerprint is not None and _is_sha256(row.request_fingerprint),
        row.report == json.loads(row.canonical_report),
        row.created_at is not None,
    )
    if not all(denormalized):
        raise ScorerTestEvaluationReportConflict(
            "Cột report scorer TEST bị lệch canonical payload."
        )
    if type(is_duplicate) is not bool:
        raise ValueError("is_duplicate phải là bool.")
    return ScorerTestStoredReport(
        record_id=row.id,
        request_fingerprint=row.request_fingerprint,
        report=report,
        is_duplicate=is_duplicate,
        committed_at=row.created_at,
    )


def scorer_test_evaluation_report_from_canonical_json(
    canonical_report: str,
    *,
    expected_report_hash: str,
) -> ScorerTestEvaluationReport:
    """Tái dựng report đã lưu rồi kiểm canonical bytes và hash trước khi trả."""
    if (
        not isinstance(canonical_report, str)
        or not canonical_report
        or not _is_sha256(expected_report_hash)
        or hashlib.sha256(canonical_report.encode("utf-8")).hexdigest() != expected_report_hash
    ):
        raise ValueError("Report hash không khớp canonical bytes.")
    payload = json.loads(canonical_report, parse_constant=_reject_json_constant)
    if type(payload) is not dict or _canonical(payload) != canonical_report:
        raise ValueError("Report không dùng encoding JSON canonical.")
    if _contains_forbidden_payload(payload):
        raise ValueError("Report chứa khóa payload hàng TEST.")

    portfolio_fields = (
        "trade_count",
        "gross_pnl",
        "fees",
        "funding",
        "slippage",
        "net_pnl",
        "maximum_drawdown",
    )
    portfolios: dict[str, ScorerPortfolioSummary] = {}
    for name in ("unfiltered_portfolio", "filtered_portfolio"):
        value = payload[name]
        if type(value) is not dict or set(value) != set(portfolio_fields):
            raise ValueError("Portfolio summary report sai schema.")
        portfolios[name] = ScorerPortfolioSummary(
            trade_count=_strict_int(value["trade_count"]),
            gross_pnl=_strict_decimal(value["gross_pnl"]),
            fees=_strict_decimal(value["fees"]),
            funding=_strict_decimal(value["funding"]),
            slippage=_strict_decimal(value["slippage"]),
            net_pnl=_strict_decimal(value["net_pnl"]),
            maximum_drawdown=_strict_decimal(value["maximum_drawdown"]),
        )

    raw_bins = payload["reliability_bins"]
    if type(raw_bins) is not list:
        raise ValueError("reliability_bins phải là list canonical.")
    reliability_bins = tuple(
        ScorerReliabilityBin(
            lower_bound=_strict_number(item["lower_bound"]),
            upper_bound=_strict_number(item["upper_bound"]),
            sample_count=_strict_int(item["sample_count"]),
            mean_confidence=_strict_number(item["mean_confidence"]),
            accuracy=_strict_number(item["accuracy"]),
        )
        for item in raw_bins
        if type(item) is dict
    )
    if len(reliability_bins) != len(raw_bins):
        raise ValueError("Một reliability bin không đúng schema.")
    status = _strict_text(payload["status"])
    if status not in {
        "INCONCLUSIVE",
        "ACCEPTED_SHADOW_ONLY",
        "ACCEPTED_PAPER_GATE_ELIGIBLE",
    }:
        raise ValueError("Trạng thái report scorer TEST không hợp lệ.")
    report = ScorerTestEvaluationReport(
        status=cast(Any, status),
        workspace_id=UUID(_strict_text(payload["workspace_id"])),
        evaluation_run_id=UUID(_strict_text(payload["evaluation_run_id"])),
        attempt_id=UUID(_strict_text(payload["attempt_id"])),
        normalized_dataset_id=UUID(_strict_text(payload["normalized_dataset_id"])),
        manifest_hash=_strict_text(payload["manifest_hash"]),
        fold_plan_hash=_strict_text(payload["fold_plan_hash"]),
        fold_id=_strict_text(payload["fold_id"]),
        approval_hash=_strict_text(payload["approval_hash"]),
        scorer_artifact_hash=_strict_text(payload["scorer_artifact_hash"]),
        base_artifact_hash=_strict_text(payload["base_artifact_hash"]),
        vector_schema_version=_strict_text(payload["vector_schema_version"]),
        vector_schema_hash=_strict_text(payload["vector_schema_hash"]),
        bundle_hash=_strict_text(payload["bundle_hash"]),
        test_membership_digest=_strict_text(payload["test_membership_digest"]),
        source_scope_hash=_strict_text(payload["source_scope_hash"]),
        opportunity_proof_id=_optional_uuid(payload["opportunity_proof_id"]),
        proof_hash=_optional_text(payload["proof_hash"]),
        candidate_completeness_verified=_strict_bool(payload["candidate_completeness_verified"]),
        scored_candidate_coverage_verified=_strict_bool(
            payload["scored_candidate_coverage_verified"]
        ),
        policy_version=_strict_text(payload["policy_version"]),
        policy_hash=_strict_text(payload["policy_hash"]),
        evaluator_version=_strict_text(payload["evaluator_version"]),
        evaluated_at=_datetime(payload["evaluated_at"]),
        test_sample_count=_strict_int(payload["test_sample_count"]),
        class_support=_tuple_pairs(payload["class_support"]),
        coverage=_strict_number(payload["coverage"]),
        abstain_count=_strict_int(payload["abstain_count"]),
        log_loss=_strict_number(payload["log_loss"]),
        brier_score=_strict_number(payload["brier_score"]),
        prior_baseline_log_loss=_strict_number(payload["prior_baseline_log_loss"]),
        identity_temperature_log_loss=_strict_number(payload["identity_temperature_log_loss"]),
        expected_calibration_error=_strict_number(payload["expected_calibration_error"]),
        reliability_bins=reliability_bins,
        confusion_matrix=_tuple_matrix(payload["confusion_matrix"]),
        opportunity_count=_strict_int(payload["opportunity_count"]),
        opportunity_candidate_count=_strict_int(payload["opportunity_candidate_count"]),
        no_candidate_count=_strict_int(payload["no_candidate_count"]),
        input_excluded_count=_strict_int(payload["input_excluded_count"]),
        candidate_count=_strict_int(payload["candidate_count"]),
        scored_candidate_count=_strict_int(payload["scored_candidate_count"]),
        approved_candidate_count=_strict_int(payload["approved_candidate_count"]),
        vetoed_candidate_count=_strict_int(payload["vetoed_candidate_count"]),
        false_veto_count=_strict_int(payload["false_veto_count"]),
        false_veto_rate=_strict_number(payload["false_veto_rate"]),
        scored_candidate_retention=_strict_number(payload["scored_candidate_retention"]),
        candidate_retention=_optional_number(payload["candidate_retention"]),
        unfiltered_portfolio=portfolios["unfiltered_portfolio"],
        filtered_portfolio=portfolios["filtered_portfolio"],
        test_access_receipt_id=_optional_uuid(payload["test_access_receipt_id"]),
        test_access_receipt_hash=_optional_text(payload["test_access_receipt_hash"]),
        test_current_rights_receipt_hash=_optional_text(
            payload["test_current_rights_receipt_hash"]
        ),
        test_payload_consumption_id=_optional_uuid(payload["test_payload_consumption_id"]),
        test_payload_commitment_hash=_optional_text(payload["test_payload_commitment_hash"]),
        test_payload_consumption_hash=_optional_text(payload["test_payload_consumption_hash"]),
        test_payload_consumed_at=(
            None
            if payload["test_payload_consumed_at"] is None
            else _datetime(payload["test_payload_consumed_at"])
        ),
        report_hash=expected_report_hash,
    )
    if report.canonical_json != canonical_report:
        raise ValueError("Report typed tái dựng không khớp canonical bytes.")
    return report


def _contains_forbidden_payload(value: object) -> bool:
    forbidden = {
        "sample_ids",
        "sample_id",
        "features",
        "labels",
        "outcomes",
        "trade_rows",
        "trade_id",
        "test_payload",
    }
    if isinstance(value, dict):
        return any(
            key in forbidden or _contains_forbidden_payload(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_payload(item) for item in value)
    return False


def _tuple_pairs(value: object) -> tuple[tuple[int, int], ...]:
    if type(value) is not list:
        raise ValueError("class_support phải là list.")
    pairs = []
    for item in value:
        if type(item) is not list or len(item) != 2:
            raise ValueError("class_support có cặp sai schema.")
        pairs.append((_strict_int(item[0]), _strict_int(item[1])))
    return tuple(pairs)


def _tuple_matrix(value: object) -> tuple[tuple[int, ...], ...]:
    if type(value) is not list or any(type(row) is not list for row in value):
        raise ValueError("confusion_matrix phải là danh sách ma trận.")
    return tuple(tuple(_strict_int(item) for item in row) for row in value)


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp report phải là chuỗi ISO.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp report cần timezone.")
    return parsed.astimezone(UTC)


def _optional_uuid(value: object) -> UUID | None:
    return None if value is None else UUID(_strict_text(value))


def _optional_text(value: object) -> str | None:
    return None if value is None else _strict_text(value)


def _optional_number(value: object) -> float | None:
    return None if value is None else _strict_number(value)


def _strict_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Giá trị canonical cần là chuỗi.")
    return value


def _strict_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("Giá trị canonical cần là số nguyên.")
    return value


def _strict_bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("Giá trị canonical cần là bool.")
    return value


def _strict_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Giá trị canonical cần là số hữu hạn.")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError("Giá trị canonical phải hữu hạn.") from error
    if not math.isfinite(number):
        raise ValueError("Giá trị canonical phải hữu hạn.")
    return number


def _strict_decimal(value: object) -> Decimal:
    try:
        decimal_value = Decimal(_strict_text(value))
    except InvalidOperation as error:
        raise ValueError("Số Decimal canonical không hợp lệ.") from error
    if not decimal_value.is_finite():
        raise ValueError("Số Decimal canonical phải hữu hạn.")
    return decimal_value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Report JSON chứa hằng số không chuẩn: {value}.")


def _uuid(value: object) -> UUID:
    if type(value) is UUID:
        return value
    if isinstance(value, str):
        return UUID(value)
    raise ValueError("Outcome id phải là UUID.")


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _is_sha256(*values: object) -> bool:
    return all(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
        for value in values
    )


def _raise_access_error(error: DBAPIError) -> None:
    message = str(error.orig).upper()
    access_tokens = (
        "WP06_SCORER_TEST",
        "WP06_SOURCE_TRAINING_RIGHTS",
        "WP06_SOURCE_RIGHTS",
        "WP06_REPLAY_TRAINING_RIGHTS",
    )
    code = next((token for token in access_tokens if token in message), None)
    if code is not None:
        raise ScorerTestEvaluationAccessConflict(
            f"Không thể đọc TEST có kiểm quyền hiện hành (mã {code})."
        ) from None
    if "UNIQUE" in message or "DUPLICATE" in message:
        raise ScorerTestEvaluationAccessConflict(
            "Receipt hoặc consumption TEST đã được tạo đồng thời; retry đọc report đã lưu."
        ) from None
    raise error


__all__ = [
    "LearningScorerTestAccessRepository",
    "LearningScorerTestEvaluationReportRepository",
    "scorer_test_evaluation_report_from_canonical_json",
]
