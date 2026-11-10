"""Bộ xác minh outcome WP06 chỉ đọc từ manifest và backtest đã lưu."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.backtest_entities import (
    BacktestReportRecord,
    BacktestRunRecord,
    BacktestTrialRecord,
)
from trading_bot.adapters.database.models.backtest_outcome_entities import (
    BacktestTradeOutcomeRecord,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import (
    BacktestOutcomeRepository,
    PersistedBacktestTradeOutcome,
)
from trading_bot.application.backtest import ReplayManifest
from trading_bot.application.learning_fold_evaluation import (
    FoldOutcomeExpectation,
    FoldOutcomeVerificationBatch,
    FoldOutcomeVerificationRequest,
    FoldOutcomeVerifier,
    PersistedFoldOutcome,
    PersistedFoldTrade,
    persisted_outcome_row_hash,
    persisted_trade_row_hash,
    verification_batch_hash,
)
from trading_bot.application.learning_fold_plan import (
    ManifestFoldPlan,
    build_manifest_fold_plan,
)
from trading_bot.domain.learning.dataset import DatasetManifest
from trading_bot.domain.learning.evaluation import EvaluationTrade

_TRIAL_FIELDS = {
    "trial_id",
    "manifest_hash",
    "parameter_family_hash",
    "trial_index",
    "trial_count",
    "status",
    "observation_count",
    "completed_observation_count",
    "trade_count",
    "error_type",
    "error_detail",
}


@dataclass(frozen=True, slots=True)
class _BacktestReportMetadata:
    """Liên kết report tối thiểu; không nạp metrics hay report hash."""

    workspace_id: UUID
    run_id: UUID
    trial_record_id: UUID
    manifest_hash: str


class FoldOutcomeVerificationError(ValueError):
    """Evidence outcome không thể được xác minh fail-closed."""


class DatabaseFoldOutcomeVerifier(FoldOutcomeVerifier):
    """Xác minh trade trong transaction rights-serialized, không đọc lại payload ngoài receipt.

    Caller mở transaction PostgreSQL ``READ COMMITTED`` để source gate nhận quyền
    mới nhất sau khi chờ WP-05 lock. Outcome/trial/report đã proof-seal và append-only;
    mọi truy vấn vẫn ở cùng transaction với receipt evaluator. Adapter không tự
    mở, commit hay thay đổi isolation. Adapter chỉ xác minh report lineage, không
    đọc hoặc xác minh report metrics/hash.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        access_receipt_id: UUID,
        manifest: DatasetManifest,
    ) -> None:
        if type(access_receipt_id) is not UUID or type(manifest) is not DatasetManifest:
            raise ValueError("Verifier cần receipt và manifest đã nhận từ gateway authoritative.")
        self._session = session
        self._access_receipt_id = access_receipt_id
        self._manifest = manifest

    async def load_verified_fold_outcomes(
        self,
        request: FoldOutcomeVerificationRequest,
    ) -> FoldOutcomeVerificationBatch:
        """Đọc manifest, liên kết report, trial và đúng các trade row được yêu cầu.

        Canonical report, metrics và report_hash không được nạp hoặc xác minh.
        """
        if type(request) is not FoldOutcomeVerificationRequest:
            raise FoldOutcomeVerificationError("Verifier cần request WP06 có kiểu tường minh.")
        await self._require_receipt_snapshot(request.workspace_id)
        if request.access_receipt_id != self._access_receipt_id:
            raise FoldOutcomeVerificationError("Request không khớp receipt evaluator hiện hành.")
        manifest = self._manifest

        self._validate_request_and_manifest(request, manifest)
        outcome_record_ids = {
            UUID(expectation.outcome_id) for expectation in request.expected_outcomes
        }
        result = await self._session.execute(
            select(
                BacktestTradeOutcomeRecord.id,
                BacktestTradeOutcomeRecord.trial_record_id,
            ).where(
                BacktestTradeOutcomeRecord.workspace_id == request.workspace_id,
                BacktestTradeOutcomeRecord.id.in_(outcome_record_ids),
            )
        )
        target_trial_rows = tuple(result.all())
        target_trial_by_id = dict(target_trial_rows)
        if (
            len(target_trial_by_id) != len(target_trial_rows)
            or set(target_trial_by_id) != outcome_record_ids
        ):
            missing = outcome_record_ids - set(target_trial_by_id)
            if missing:
                raise FoldOutcomeVerificationError(
                    "Thiếu trade outcome row authoritative; sample không trade hoặc chưa "
                    "được persist không thể được suy diễn thành COMPLETED."
                )
            raise FoldOutcomeVerificationError("Outcome row UUID bị lặp trong kho.")

        trial_ids = set(target_trial_by_id.values())
        trials = await self._load_trials(request.workspace_id, trial_ids)
        reports = await self._load_reports(request.workspace_id, trial_ids)
        run_ids = {trial.run_id for trial in trials.values()}
        runs = await self._load_runs(request.workspace_id, run_ids)

        rows_by_trial: dict[UUID, dict[UUID, PersistedBacktestTradeOutcome]] = {
            trial_id: {} for trial_id in trials
        }
        for trial_record_id, trial in trials.items():
            report = reports.get(trial_record_id)
            run = runs.get(trial.run_id)
            if report is None or run is None:
                raise FoldOutcomeVerificationError(
                    "Trial COMPLETED thiếu report hoặc replay manifest đã lưu."
                )
            self._validate_trial(trial, workspace_id=request.workspace_id)
            replay_manifest = self._validate_run(run, workspace_id=request.workspace_id)
            self._validate_report_metadata(
                report,
                trial=trial,
                workspace_id=request.workspace_id,
            )
            if (
                trial.run_id != run.id
                or trial.manifest_hash != run.manifest_hash
                or report.run_id != run.id
                or report.manifest_hash != trial.manifest_hash
            ):
                raise FoldOutcomeVerificationError("Run, trial và report lệch manifest lineage.")

            (
                row_count,
                min_ordinal,
                max_ordinal,
                distinct_ordinals,
            ) = await self._load_outcome_ordinal_metadata(
                workspace_id=request.workspace_id,
                trial_record_id=trial_record_id,
            )
            if (
                row_count != trial.trade_count
                or distinct_ordinals != row_count
                or (row_count > 0 and (min_ordinal != 0 or max_ordinal != row_count - 1))
                or (row_count == 0 and (min_ordinal is not None or max_ordinal is not None))
            ):
                raise FoldOutcomeVerificationError(
                    "Ordinal metadata không liên tục hoặc trade_count lệch trial."
                )
            if (
                trial.parameter_family_hash != replay_manifest.parameter_family_hash
                or trial.trial_index != replay_manifest.trial_index
                or trial.trial_count != replay_manifest.trial_count
            ):
                raise FoldOutcomeVerificationError(
                    "Trial parameter family/index/count không khớp replay manifest."
                )

        persisted = await BacktestOutcomeRepository(
            self._session
        ).load_authorized_evaluation_fold_rows(
            receipt_id=self._access_receipt_id,
            workspace_id=request.workspace_id,
            manifest_hash=request.manifest_hash,
            split_policy_version=request.split_policy_version,
            fold_plan_hash=request.fold_plan_hash,
            fold_id=request.fold_id,
            outcome_record_ids=outcome_record_ids,
        )
        for row in persisted.rows:
            if (
                row.workspace_id != request.workspace_id
                or row.trial_record_id not in trials
                or row.run_id != trials[row.trial_record_id].run_id
                or row.manifest_hash != trials[row.trial_record_id].manifest_hash
            ):
                raise FoldOutcomeVerificationError(
                    "Trade row lệch workspace, run, trial hoặc replay manifest."
                )
            rows_by_trial[row.trial_record_id][row.record_id] = row

        fold_outcomes: list[PersistedFoldOutcome] = []
        for expectation in request.expected_outcomes:
            record_id = UUID(expectation.outcome_id)
            trial_record_id = target_trial_by_id[record_id]
            target_trial = trials.get(trial_record_id)
            if target_trial is None or target_trial.status != "COMPLETED":
                raise FoldOutcomeVerificationError(
                    "Outcome chỉ được dùng khi trial có trạng thái COMPLETED."
                )
            persisted_row = rows_by_trial[trial_record_id].get(record_id)
            if persisted_row is None:
                raise FoldOutcomeVerificationError(
                    "Outcome UUID không nằm trong các row được yêu cầu của trial."
                )
            if expectation.outcome_hash != persisted_row.outcome_hash:
                raise FoldOutcomeVerificationError("Outcome hash không khớp manifest sample.")
            if not (
                expectation.outcome_start <= persisted_row.trade.opened_at < expectation.outcome_end
                and persisted_row.trade.opened_at
                <= persisted_row.trade.closed_at
                <= expectation.outcome_end
            ):
                raise FoldOutcomeVerificationError(
                    "Trade nằm ngoài outcome interval nửa mở của sample."
                )
            fold_outcomes.append(
                self._build_fold_outcome(
                    request=request,
                    expectation=expectation,
                    status=target_trial.status,
                    outcome_hash=persisted_row.outcome_hash,
                    trade=persisted_row.trade,
                )
            )

        outcomes = tuple(sorted(fold_outcomes, key=lambda item: item.sample_id))
        return FoldOutcomeVerificationBatch(
            workspace_id=request.workspace_id,
            manifest_hash=request.manifest_hash,
            split_policy_version=request.split_policy_version,
            fold_plan_hash=request.fold_plan_hash,
            fold_id=request.fold_id,
            outcomes=outcomes,
            batch_hash=verification_batch_hash(request, outcomes),
        )

    async def _require_receipt_snapshot(self, workspace_id: UUID) -> None:
        if not self._session.in_transaction():
            raise FoldOutcomeVerificationError(
                "Caller phải mở transaction rights-serialized và có receipt evaluator."
            )
        isolation: str = str(
            (await self._session.execute(text("SHOW transaction_isolation"))).scalar_one()
        )
        read_only: str = str(
            (await self._session.execute(text("SHOW transaction_read_only"))).scalar_one()
        )
        if str(isolation).lower() not in {"read committed", "repeatable read", "serializable"}:
            raise FoldOutcomeVerificationError(
                "Outcome verifier cần isolation READ COMMITTED, REPEATABLE READ hoặc SERIALIZABLE."
            )
        if str(read_only).lower() not in {"off", "false", "0"}:
            raise FoldOutcomeVerificationError(
                "Outcome verifier cần transaction READ WRITE để ghi receipt fold."
            )
        if self._session.info.get("workspace_id") != workspace_id:
            raise FoldOutcomeVerificationError(
                "Session thiếu workspace đã xác thực hoặc workspace không khớp request."
            )
        current_workspace = (
            await self._session.execute(text("SELECT current_setting('app.workspace_id', true)"))
        ).scalar_one_or_none()
        if current_workspace != str(workspace_id):
            raise FoldOutcomeVerificationError(
                "Tenant context PostgreSQL thiếu hoặc không khớp workspace request."
            )

    def _validate_request_and_manifest(
        self,
        request: FoldOutcomeVerificationRequest,
        manifest: DatasetManifest,
    ) -> None:
        if request.workspace_id != manifest.workspace_id:
            raise FoldOutcomeVerificationError("Manifest workspace không khớp request.")
        if request.manifest_hash != manifest.content_hash:
            raise FoldOutcomeVerificationError("Manifest hash lưu trữ không khớp request.")
        plan = request.fold_plan
        if type(plan) is not ManifestFoldPlan:
            raise FoldOutcomeVerificationError("Request thiếu ManifestFoldPlan tường minh.")
        if (
            plan.manifest_hash != manifest.content_hash
            or plan.split_policy_version != request.split_policy_version
            or plan.content_hash != request.fold_plan_hash
            or manifest.split_policy_version != request.split_policy_version
        ):
            raise FoldOutcomeVerificationError("Fold plan không gắn đúng manifest/policy.")
        try:
            rebuilt_plan = build_manifest_fold_plan(
                manifest,
                folds=tuple(selection.split for selection in plan.folds),
                expected_split_policy_version=manifest.split_policy_version,
                evaluation_cutoff=plan.evaluation_cutoff,
            )
        except Exception as error:
            raise FoldOutcomeVerificationError(
                "Không thể tái dựng fold plan từ manifest."
            ) from error
        if rebuilt_plan != plan:
            raise FoldOutcomeVerificationError("Fold plan request không khớp tái dựng canonical.")
        selected_folds = tuple(
            item for item in rebuilt_plan.folds if item.fold_id == request.fold_id
        )
        if len(selected_folds) != 1:
            raise FoldOutcomeVerificationError("Fold ID không thuộc fold plan authoritative.")

        selected = selected_folds[0]
        expected_roles: dict[str, str] = {}
        for role, sample_ids in (
            ("train", selected.train_sample_ids),
            ("validation", selected.validation_sample_ids),
            ("test", selected.test_sample_ids),
        ):
            for sample_id in sample_ids:
                if sample_id in expected_roles:
                    raise FoldOutcomeVerificationError("Fold plan lặp sample giữa các role.")
                expected_roles[sample_id] = role
        if not request.expected_outcomes:
            raise FoldOutcomeVerificationError("Fold không có sample outcome để xác minh.")
        if type(request.expected_outcomes) is not tuple:
            raise FoldOutcomeVerificationError("Expected outcomes phải là tuple bất biến.")
        expectations: dict[str, FoldOutcomeExpectation] = {}
        outcome_ids: set[str] = set()
        samples_by_id = {sample.sample_id: sample for sample in manifest.samples}
        for expectation in request.expected_outcomes:
            if expectation.sample_id in expectations:
                raise FoldOutcomeVerificationError("Request lặp sample membership.")
            if expected_roles.get(expectation.sample_id) != expectation.role:
                raise FoldOutcomeVerificationError("Sample role không khớp fold plan.")
            sample = samples_by_id.get(expectation.sample_id)
            if sample is None:
                raise FoldOutcomeVerificationError("Sample không thuộc manifest lưu trữ.")
            if (
                expectation.outcome_id != sample.outcome_id
                or expectation.outcome_hash != sample.outcome_hash
                or expectation.outcome_start != sample.outcome_start
                or expectation.outcome_end != sample.outcome_end
            ):
                raise FoldOutcomeVerificationError(
                    "Outcome ID/hash/interval của request không khớp sample manifest."
                )
            if expectation.role not in {"train", "validation", "test"}:
                raise FoldOutcomeVerificationError("Sample role không được phép.")
            if expectation.outcome_id in outcome_ids:
                raise FoldOutcomeVerificationError(
                    "Nhiều sample cùng trỏ tới một trade outcome row."
                )
            try:
                outcome_id = UUID(expectation.outcome_id)
            except (AttributeError, TypeError, ValueError) as error:
                raise FoldOutcomeVerificationError(
                    "outcome_id phải là UUID của BacktestTradeOutcomeRecord."
                ) from error
            if str(outcome_id) != expectation.outcome_id:
                raise FoldOutcomeVerificationError(
                    "outcome_id UUID phải ở dạng canonical lowercase."
                )
            _normalize_utc(expectation.outcome_start, "outcome_start")
            _normalize_utc(expectation.outcome_end, "outcome_end")
            if not expectation.outcome_start < expectation.outcome_end:
                raise FoldOutcomeVerificationError("Outcome interval phải có độ dài dương.")
            expectations[expectation.sample_id] = expectation
            outcome_ids.add(expectation.outcome_id)
        if set(expectations) != set(expected_roles):
            raise FoldOutcomeVerificationError(
                "Expected outcome membership thiếu hoặc thừa sample so với fold plan."
            )

    async def _load_trials(
        self, workspace_id: UUID, trial_ids: set[UUID]
    ) -> dict[UUID, BacktestTrialRecord]:
        result = await self._session.scalars(
            select(BacktestTrialRecord).where(
                BacktestTrialRecord.workspace_id == workspace_id,
                BacktestTrialRecord.id.in_(trial_ids),
            )
        )
        rows = tuple(result.all())
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(rows) or set(by_id) != trial_ids:
            raise FoldOutcomeVerificationError("Trial row thiếu, trùng hoặc vượt workspace scope.")
        return by_id

    async def _load_reports(
        self, workspace_id: UUID, trial_ids: set[UUID]
    ) -> dict[UUID, _BacktestReportMetadata]:
        result = await self._session.execute(
            select(
                BacktestReportRecord.workspace_id,
                BacktestReportRecord.run_id,
                BacktestReportRecord.trial_record_id,
                BacktestReportRecord.manifest_hash,
            ).where(
                BacktestReportRecord.workspace_id == workspace_id,
                BacktestReportRecord.trial_record_id.in_(trial_ids),
            )
        )
        rows = tuple(result.all())
        reports = tuple(_BacktestReportMetadata(*row) for row in rows)
        by_id = {row.trial_record_id: row for row in reports}
        if len(by_id) != len(rows):
            raise FoldOutcomeVerificationError("Trial có nhiều report hoặc report trùng.")
        return by_id

    async def _load_outcome_ordinal_metadata(
        self, *, workspace_id: UUID, trial_record_id: UUID
    ) -> tuple[int, int | None, int | None, int]:
        result = await self._session.execute(
            select(
                func.count(BacktestTradeOutcomeRecord.id),
                func.min(BacktestTradeOutcomeRecord.ordinal),
                func.max(BacktestTradeOutcomeRecord.ordinal),
                func.count(distinct(BacktestTradeOutcomeRecord.ordinal)),
            ).where(
                BacktestTradeOutcomeRecord.workspace_id == workspace_id,
                BacktestTradeOutcomeRecord.trial_record_id == trial_record_id,
            )
        )
        row_count, min_ordinal, max_ordinal, distinct_ordinals = result.one()
        return int(row_count), min_ordinal, max_ordinal, int(distinct_ordinals)

    async def _load_runs(
        self, workspace_id: UUID, run_ids: set[UUID]
    ) -> dict[UUID, BacktestRunRecord]:
        result = await self._session.scalars(
            select(BacktestRunRecord).where(
                BacktestRunRecord.workspace_id == workspace_id,
                BacktestRunRecord.id.in_(run_ids),
            )
        )
        rows = tuple(result.all())
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(rows) or set(by_id) != run_ids:
            raise FoldOutcomeVerificationError("Replay manifest thiếu hoặc vượt workspace scope.")
        return by_id

    @staticmethod
    def _validate_trial(trial: BacktestTrialRecord, *, workspace_id: UUID) -> None:
        payload = _load_canonical_object(trial.canonical_trial, None)
        if set(payload) != _TRIAL_FIELDS:
            raise FoldOutcomeVerificationError("Canonical trial payload sai schema.")
        if hashlib.sha256(trial.canonical_trial.encode("utf-8")).hexdigest() != trial.content_hash:
            raise FoldOutcomeVerificationError("Trial content hash không khớp canonical payload.")
        denormalized = {
            "trial_id": trial.trial_id,
            "manifest_hash": trial.manifest_hash,
            "parameter_family_hash": trial.parameter_family_hash,
            "trial_index": trial.trial_index,
            "trial_count": trial.trial_count,
            "status": trial.status,
            "observation_count": trial.observation_count,
            "completed_observation_count": trial.completed_observation_count,
            "trade_count": trial.trade_count,
            "error_type": trial.error_type,
            "error_detail": trial.error_detail,
        }
        if payload != denormalized or trial.workspace_id != workspace_id:
            raise FoldOutcomeVerificationError("Trial columns không khớp canonical payload/scope.")
        if trial.status != "COMPLETED":
            raise FoldOutcomeVerificationError("Trial chưa COMPLETED.")
        if trial.error_type is not None or trial.error_detail is not None:
            raise FoldOutcomeVerificationError("Trial COMPLETED mang trường lỗi.")
        expected_trial_id = hashlib.sha256(f"trial:{trial.manifest_hash}".encode()).hexdigest()
        if trial.trial_id != expected_trial_id:
            raise FoldOutcomeVerificationError("Trial ID không khớp replay manifest hash.")

    @staticmethod
    def _validate_run(run: BacktestRunRecord, *, workspace_id: UUID) -> ReplayManifest:
        payload = _load_canonical_object(run.canonical_manifest, run.manifest)
        if payload != run.manifest or run.workspace_id != workspace_id:
            raise FoldOutcomeVerificationError("Replay manifest JSONB/canonical scope không khớp.")
        try:
            normalized = dict(payload)
            normalized.setdefault("learning_intent", "RESEARCH")
            normalized.setdefault("learning_job_id", None)
            normalized["range_start"] = datetime.fromisoformat(str(normalized["range_start"]))
            normalized["range_end"] = datetime.fromisoformat(str(normalized["range_end"]))
            normalized["cost_assumptions"] = tuple(
                tuple(item) for item in normalized["cost_assumptions"]
            )
            replay_manifest = ReplayManifest(**normalized)
        except Exception as error:
            raise FoldOutcomeVerificationError("Replay manifest không thể tái dựng.") from error
        if replay_manifest.content_hash != run.manifest_hash:
            raise FoldOutcomeVerificationError("Replay manifest content hash không khớp.")
        return replay_manifest

    @staticmethod
    def _validate_report_metadata(
        report: _BacktestReportMetadata,
        *,
        trial: BacktestTrialRecord,
        workspace_id: UUID,
    ) -> None:
        if report.workspace_id != workspace_id:
            raise FoldOutcomeVerificationError("Report metadata vượt workspace scope.")
        if report.trial_record_id != trial.id or report.run_id != trial.run_id:
            raise FoldOutcomeVerificationError("Report không gắn đúng trial/run.")
        if report.manifest_hash != trial.manifest_hash:
            raise FoldOutcomeVerificationError("Report manifest hash không khớp trial.")

    @staticmethod
    def _build_fold_outcome(
        *,
        request: FoldOutcomeVerificationRequest,
        expectation: FoldOutcomeExpectation,
        status: str,
        outcome_hash: str,
        trade: EvaluationTrade,
    ) -> PersistedFoldOutcome:
        # DTO dùng ordinal cục bộ 0; SQL đã kiểm tra ordinal trial-wide từ metadata.
        outcome = PersistedFoldOutcome(
            sample_id=expectation.sample_id,
            role=expectation.role,
            outcome_id=expectation.outcome_id,
            outcome_hash=outcome_hash,
            status=status,
            trade_count=1,
            row_hash="0" * 64,
            trades=(),
        )
        trade_row = PersistedFoldTrade(ordinal=0, trade=trade, row_hash="0" * 64)
        trade_row = replace(
            trade_row,
            row_hash=persisted_trade_row_hash(request, outcome, trade_row),
        )
        outcome = replace(outcome, trades=(trade_row,))
        return replace(
            outcome,
            row_hash=persisted_outcome_row_hash(request, outcome, (trade_row,)),
        )


def _normalize_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FoldOutcomeVerificationError(f"{name} phải là timestamp có múi giờ.")
    return value.astimezone(UTC)


def _load_canonical_object(canonical_json: str, expected: Any) -> dict[str, Any]:
    try:
        payload = json.loads(canonical_json)
    except (TypeError, ValueError) as error:
        raise FoldOutcomeVerificationError("Canonical JSON không hợp lệ.") from error
    if type(payload) is not dict or _canonical(payload) != canonical_json:
        raise FoldOutcomeVerificationError("Canonical JSON bytes không chuẩn.")
    if expected is not None and payload != expected:
        raise FoldOutcomeVerificationError("Canonical JSON không khớp payload JSONB.")
    return payload


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
