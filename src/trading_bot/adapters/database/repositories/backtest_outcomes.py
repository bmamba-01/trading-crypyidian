"""Lưu tham số scenario và giao dịch replay theo hợp đồng bất biến."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Uuid, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.backtest_entities import (
    BacktestRunRecord,
    BacktestTrialRecord,
)
from trading_bot.adapters.database.models.backtest_opportunity_entities import (
    BacktestDecisionOpportunityRecord,
)
from trading_bot.adapters.database.models.backtest_outcome_entities import (
    BacktestRunDatasetRightsRecord,
    BacktestScenarioRecord,
    BacktestTradeOutcomeRecord,
)
from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.adapters.database.repositories.backtests import (
    BacktestAppendResult,
    BacktestRepository,
    BacktestRepositoryConflict,
)
from trading_bot.application.backtest import EvaluationReport, ReplayRejected
from trading_bot.application.backtest_dataset_manifest import (
    BacktestDatasetManifest,
    MarketBarIdentityMetadata,
    backtest_dataset_identity_metadata_hash,
)
from trading_bot.application.backtest_dataset_replay_bundle import BacktestDatasetReplayBundle
from trading_bot.application.backtest_decision_opportunities import ReplayDecisionOpportunity
from trading_bot.application.backtest_sensitivity import SensitivityRun
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    market_source_for_rights_provider,
)
from trading_bot.domain.learning.evaluation import EvaluationTrade
from trading_bot.domain.learning.source_use_lineage import SourceUseLineage
from trading_bot.domain.market.models import MarketBarSourceIdentity


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


@dataclass(frozen=True, slots=True)
class BacktestTrainingBundleLineage:
    """Lineage authoritative của run bundle cùng mọi child và MarketBar ID."""

    bundle_hash: str
    source_use_lineage: tuple[SourceUseLineage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.bundle_hash, str) or len(self.bundle_hash) != 64:
            raise ValueError("bundle_hash phải là SHA-256.")
        if not self.source_use_lineage or any(
            type(item) is not SourceUseLineage for item in self.source_use_lineage
        ):
            raise ValueError("Bundle lineage cần SourceUseLineage typed không rỗng.")
        ordered = tuple(sorted(self.source_use_lineage, key=lambda item: item.child_dataset_hash))
        if ordered != self.source_use_lineage:
            raise ValueError("Bundle lineage phải sắp xếp theo child hash.")
        if any(item.bundle_hash != self.bundle_hash for item in ordered):
            raise ValueError("Bundle lineage child không khớp bundle_hash.")
        if len({item.child_dataset_hash for item in ordered}) != len(ordered):
            raise ValueError("Bundle lineage không được lặp child.")


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
        opportunities: tuple[ReplayDecisionOpportunity, ...] = (),
    ) -> BacktestOutcomeAppendResult:
        """Bao savepoint toàn lượt append để caller bắt lỗi cũng không giữ hàng dở dang."""
        _validate_opportunity_links(workspace_id, sensitivity_run, trade_outcomes, opportunities)
        async with self._session.begin_nested():
            return await self._append_sensitivity_run(
                workspace_id=workspace_id,
                sensitivity_run=sensitivity_run,
                trade_outcomes=trade_outcomes,
                dataset_bundle=dataset_bundle,
                opportunities=opportunities,
            )

    async def _append_sensitivity_run(
        self,
        *,
        workspace_id: UUID,
        sensitivity_run: SensitivityRun,
        trade_outcomes: tuple[EvaluationTrade, ...],
        dataset_bundle: BacktestDatasetReplayBundle | None = None,
        opportunities: tuple[ReplayDecisionOpportunity, ...] = (),
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

        opportunity_duplicates = []
        for opportunity in opportunities:
            opportunity_duplicates.append(
                await self._append_opportunity(
                    appended=appended,
                    scenario_id=scenario_row.id,
                    opportunity=opportunity,
                )
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
                    entry_decision_snapshot_id=trade.entry_decision_snapshot_id,
                    entry_decision_at=trade.entry_decision_at,
                    entry_feature_schema_hash=trade.entry_feature_schema_hash,
                    entry_features=(
                        [list(item) for item in trade.entry_features]
                        if trade.has_entry_provenance
                        else None
                    ),
                    entry_features_hash=trade.entry_features_hash,
                    entry_symbol=trade.entry_symbol,
                    entry_opportunity_event_id=trade.entry_opportunity_event_id,
                    entry_candidate_id=trade.entry_candidate_id,
                    entry_vector_hash=trade.entry_vector_hash,
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
            duplicate=(
                appended.duplicate
                and scenario_duplicate
                and all(outcome_duplicates)
                and all(opportunity_duplicates)
            ),
        )

    async def _append_opportunity(
        self,
        *,
        appended: BacktestAppendResult,
        scenario_id: UUID,
        opportunity: ReplayDecisionOpportunity,
    ) -> bool:
        """Retry chỉ đọc canonical metadata; không cần quyền SELECT toàn bảng."""
        lookup = select(BacktestDecisionOpportunityRecord.canonical_opportunity).where(
            BacktestDecisionOpportunityRecord.workspace_id == opportunity.workspace_id,
            BacktestDecisionOpportunityRecord.trial_record_id == appended.trial_record_id,
            BacktestDecisionOpportunityRecord.query_version == opportunity.query_version,
            BacktestDecisionOpportunityRecord.event_id == opportunity.event_id,
        )
        canonical = opportunity.canonical_json()
        existing = await self._session.scalar(lookup)
        if existing is not None:
            if existing != canonical:
                raise BacktestRepositoryConflict("Opportunity event đã tồn tại với canonical khác.")
            return True
        payload = opportunity.canonical_payload()
        values = {
            key: getattr(opportunity, key)
            for key in payload
            if key not in {"scenario_id", "feature_snapshot_ids", "source_event_digests"}
        }
        row = BacktestDecisionOpportunityRecord(
            **values,
            id=uuid4(),
            run_id=appended.run_id,
            trial_record_id=appended.trial_record_id,
            scenario_id=scenario_id,
            scenario_key=opportunity.scenario_id,
            feature_snapshot_ids=[list(pair) for pair in opportunity.feature_snapshot_ids],
            source_event_digests=[list(pair) for pair in opportunity.source_event_digests],
            canonical_opportunity=canonical,
            content_hash=opportunity.content_hash,
            idempotency_key=opportunity.idempotency_key,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as error:
            existing = await self._session.scalar(lookup)
            if existing != canonical:
                raise BacktestRepositoryConflict(
                    "Opportunity event/ordinal bị conflict."
                ) from error
            return True
        return False

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
            trade = _validate_trade_outcome_row(row)
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
            trade = _validate_trade_outcome_row(row)
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

    async def load_training_bundle_lineage(
        self,
        *,
        workspace_id: UUID,
        trial_record_id: UUID,
        outcomes: tuple[PersistedBacktestTradeOutcome, ...],
    ) -> BacktestTrainingBundleLineage:
        """Lấy bundle từ run/receipt và xác minh manifest/bar identity từng child."""
        if type(workspace_id) is not UUID or type(trial_record_id) is not UUID or not outcomes:
            raise ValueError("Cần workspace, trial và outcomes đã nạp để dựng lineage.")
        trial = await self._session.scalar(
            select(BacktestTrialRecord).where(
                BacktestTrialRecord.workspace_id == workspace_id,
                BacktestTrialRecord.id == trial_record_id,
            )
        )
        if trial is None or trial.workspace_id != workspace_id or trial.id != trial_record_id:
            raise BacktestRepositoryConflict("Thiếu trial authoritative trong workspace.")
        run = await self._session.scalar(
            select(BacktestRunRecord).where(
                BacktestRunRecord.workspace_id == workspace_id,
                BacktestRunRecord.id == trial.run_id,
            )
        )
        if (
            run is None
            or run.workspace_id != workspace_id
            or run.id != trial.run_id
            or run.manifest_hash != trial.manifest_hash
            or _sha256(run.canonical_manifest) != run.manifest_hash
            or _canonical(run.manifest) != run.canonical_manifest
        ):
            raise BacktestRepositoryConflict("Run/trial manifest không khớp canonical lineage.")
        persisted_result = await self._session.execute(
            select(
                BacktestTradeOutcomeRecord.id,
                BacktestTradeOutcomeRecord.workspace_id,
                BacktestTradeOutcomeRecord.run_id,
                BacktestTradeOutcomeRecord.trial_record_id,
                BacktestTradeOutcomeRecord.manifest_hash,
                BacktestTradeOutcomeRecord.ordinal,
            )
            .where(
                BacktestTradeOutcomeRecord.workspace_id == workspace_id,
                BacktestTradeOutcomeRecord.trial_record_id == trial_record_id,
            )
            .order_by(BacktestTradeOutcomeRecord.ordinal)
        )
        persisted_outcomes = persisted_result.mappings().all()
        persisted_by_id = {item["id"]: item for item in persisted_outcomes}
        expected_by_id = {item.record_id: item for item in outcomes}
        if len(persisted_by_id) != len(persisted_outcomes) or set(persisted_by_id) != set(
            expected_by_id
        ):
            raise BacktestRepositoryConflict(
                "Tập outcome đã nạp không khớp đúng mọi trade row persisted của trial."
            )
        for record_id, persisted in persisted_by_id.items():
            expected = expected_by_id[record_id]
            if any(
                persisted[name] != getattr(expected, name)
                for name in (
                    "workspace_id",
                    "run_id",
                    "trial_record_id",
                    "manifest_hash",
                    "ordinal",
                )
            ):
                raise BacktestRepositoryConflict(
                    "Outcome row persisted lệch run/trial/manifest lineage."
                )
        if any(
            row.workspace_id != workspace_id
            or row.trial_record_id != trial_record_id
            or row.run_id != run.id
            or row.manifest_hash != run.manifest_hash
            for row in outcomes
        ):
            raise BacktestRepositoryConflict("Outcome vượt run/trial/manifest lineage.")
        bundle_hash = run.manifest.get("dataset_hash")
        if not isinstance(bundle_hash, str) or re.fullmatch(r"[0-9a-f]{64}", bundle_hash) is None:
            raise BacktestRepositoryConflict("Run không ghim dataset replay bundle.")

        from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
            BacktestDatasetReplayBundleRepository,
        )

        bundle = await BacktestDatasetReplayBundleRepository(self._session).get_by_hash(
            workspace_id=workspace_id,
            bundle_hash=bundle_hash,
        )
        if (
            bundle is None
            or bundle.workspace_id != workspace_id
            or bundle.bundle_hash != bundle_hash
        ):
            raise BacktestRepositoryConflict("Run thiếu bundle canonical trong workspace.")
        receipts_result = await self._session.scalars(
            select(BacktestRunDatasetRightsRecord).where(
                BacktestRunDatasetRightsRecord.workspace_id == workspace_id,
                BacktestRunDatasetRightsRecord.run_id == run.id,
            )
        )
        receipts = tuple(receipts_result.all())
        expected_children = {item.dataset_hash: item for item in bundle.components}
        if (
            not expected_children
            or len(expected_children) != len(bundle.components)
            or len(receipts) != len(expected_children)
            or {item.child_dataset_hash for item in receipts} != set(expected_children)
        ):
            raise BacktestRepositoryConflict("Tập receipt rights không khớp mọi bundle child.")
        rights_by_child = {}
        for receipt in receipts:
            component = expected_children[receipt.child_dataset_hash]
            _validate_stored_rights_receipt_integrity(receipt)
            if (
                receipt.workspace_id != workspace_id
                or receipt.run_id != run.id
                or receipt.bundle_hash != bundle_hash
                or receipt.child_dataset_hash != component.dataset_hash
                or receipt.interval != component.interval
                or receipt.provider != bundle.rights_provider
                or receipt.purpose != bundle.rights_purpose
                or receipt.policy_fingerprint != bundle.policy_fingerprint
            ):
                raise BacktestRepositoryConflict("Receipt rights lệch bundle/provider/policy.")
            rights_by_child[receipt.child_dataset_hash] = receipt

        from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
            BacktestDatasetManifestRepository,
        )

        lineage = []
        for component in bundle.components:
            dataset = await BacktestDatasetManifestRepository(self._session).get_by_hash(
                workspace_id=workspace_id,
                dataset_hash=component.dataset_hash,
            )
            if (
                type(dataset) is not BacktestDatasetManifest
                or not dataset.is_ready
                or not dataset.has_pinned_source_identities
                or dataset.rights_workspace_id != workspace_id
                or dataset.dataset_hash != component.dataset_hash
                or dataset.interval != component.interval
                or dataset.symbol != bundle.symbol
                or dataset.evaluation_start != bundle.evaluation_start
                or dataset.range_end != bundle.range_end
                or dataset.bars_content_hash != component.bars_content_hash
                or dataset.rights_provider != bundle.rights_provider
                or dataset.rights_purpose != bundle.rights_purpose
                or dataset.policy_fingerprint != bundle.policy_fingerprint
            ):
                raise BacktestRepositoryConflict(
                    "Child manifest không khớp bundle hoặc thiếu MarketBar identity v4."
                )
            right = rights_by_child[component.dataset_hash]
            source_bars = await _load_child_market_bar_identity_metadata(
                self._session,
                workspace_id=workspace_id,
                dataset=dataset,
            )
            observed_identity_hash = backtest_dataset_identity_metadata_hash(
                source_bars,
                workspace_id=workspace_id,
            )
            if observed_identity_hash != dataset.bar_identity_hash:
                raise BacktestRepositoryConflict(
                    "MarketBar UUID/revision set lệch child bar_identity_hash."
                )
            lineage.append(
                SourceUseLineage(
                    source_id=bundle.rights_provider,
                    provider=right.provider,
                    bundle_hash=bundle_hash,
                    child_dataset_hash=dataset.dataset_hash,
                    policy_fingerprint=right.policy_fingerprint,
                    symbol=dataset.symbol,
                    interval=dataset.interval,
                    data_start=dataset.warmup_start,
                    data_end=dataset.range_end,
                    bar_identity_hash=dataset.bar_identity_hash or "",
                    market_bar_ids=tuple(
                        f"market-bar-record:{item.source_identity.record_id}"
                        for item in sorted(
                            source_bars,
                            key=lambda item: (
                                item.open_time.astimezone(UTC),
                                item.source,
                                item.symbol,
                                item.timeframe,
                            ),
                        )
                    ),
                )
            )
        return BacktestTrainingBundleLineage(
            bundle_hash=bundle_hash,
            source_use_lineage=tuple(sorted(lineage, key=lambda item: item.child_dataset_hash)),
        )

    async def load_authorized_evaluation_fold_rows(
        self,
        *,
        receipt_id: UUID,
        workspace_id: UUID,
        manifest_hash: str,
        split_policy_version: str,
        fold_plan_hash: str,
        fold_id: str,
        outcome_record_ids: set[UUID],
    ) -> PersistedBacktestTradeOutcomeRows:
        """Đọc payload đúng một fold qua hàm DB và tiêu thụ fold trong receipt."""
        if any(type(value) is not UUID for value in (receipt_id, workspace_id)):
            raise ValueError("Receipt và workspace phải là UUID.")
        if not outcome_record_ids or any(type(item) is not UUID for item in outcome_record_ids):
            raise ValueError("Fold evaluator cần đúng tập UUID outcome không rỗng.")
        statement = text(
            "SELECT * FROM public.wp06_read_learning_fold_outcomes("
            "target_receipt_id => CAST(:receipt_id AS uuid), "
            "target_workspace => CAST(:workspace_id AS uuid), "
            "target_manifest_hash => CAST(:manifest_hash AS text), "
            "target_split_policy_version => CAST(:split_policy_version AS text), "
            "target_fold_plan_hash => CAST(:fold_plan_hash AS text), "
            "target_fold_id => CAST(:fold_id AS text), "
            "target_outcome_ids => CAST(:outcome_ids AS uuid[]))"
        ).bindparams(bindparam("outcome_ids", type_=ARRAY(Uuid(as_uuid=True))))
        rows_result = await self._session.scalars(
            select(BacktestTradeOutcomeRecord).from_statement(statement),
            {
                "receipt_id": str(receipt_id),
                "workspace_id": str(workspace_id),
                "manifest_hash": manifest_hash,
                "split_policy_version": split_policy_version,
                "fold_plan_hash": fold_plan_hash,
                "fold_id": fold_id,
                "outcome_ids": sorted(outcome_record_ids, key=str),
            },
        )
        records = tuple(rows_result.all())
        record_ids = {row.id for row in records}
        if len(record_ids) != len(records) or record_ids != outcome_record_ids:
            raise BacktestRepositoryConflict(
                "Gateway không trả đủ đúng các outcome UUID đã gán cho fold."
            )
        persisted_rows: list[PersistedBacktestTradeOutcome] = []
        canonical_values: list[str] = []
        for row in sorted(records, key=lambda item: item.ordinal):
            if row.workspace_id != workspace_id:
                raise BacktestRepositoryConflict("Gateway outcome vượt workspace scope.")
            trade = _validate_trade_outcome_row(row)
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


async def _load_child_market_bar_identity_metadata(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    dataset: BacktestDatasetManifest,
) -> tuple[MarketBarIdentityMetadata, ...]:
    """Nạp đúng MarketBar ID đã khả dụng tại prepared_at cho một child manifest."""
    columns = (
        MarketBar.id,
        MarketBar.workspace_id,
        MarketBar.symbol,
        MarketBar.timeframe,
        MarketBar.source,
        MarketBar.logical_key,
        MarketBar.revision,
        MarketBar.supersedes_id,
        MarketBar.checksum,
        MarketBar.raw_reference_id,
        MarketBar.open_time,
        MarketBar.close_time,
        MarketBar.available_at,
        MarketBar.is_closed,
    )
    try:
        market_source = market_source_for_rights_provider(dataset.rights_provider)
    except ValueError as error:
        raise BacktestRepositoryConflict(
            "Child provider không ánh xạ được MarketBar source."
        ) from error
    result = await session.execute(
        select(*columns)
        .where(
            MarketBar.workspace_id == workspace_id,
            MarketBar.symbol == dataset.symbol,
            MarketBar.timeframe == dataset.interval,
            MarketBar.source == market_source,
            MarketBar.open_time >= dataset.warmup_start,
            MarketBar.open_time < dataset.range_end,
            MarketBar.available_at <= dataset.prepared_at,
            MarketBar.is_closed.is_(True),
        )
        .order_by(MarketBar.open_time, MarketBar.revision.desc())
    )
    mappings = result.mappings().all()
    latest: dict[str, Any] = {}
    for row in mappings:
        if (
            row["workspace_id"] != workspace_id
            or row["symbol"] != dataset.symbol
            or row["timeframe"] != dataset.interval
            or row["source"] != market_source
            or not dataset.warmup_start <= row["open_time"] < dataset.range_end
            or row["available_at"] > dataset.prepared_at
            or row["is_closed"] is not True
            or not isinstance(row["logical_key"], str)
        ):
            raise BacktestRepositoryConflict("MarketBar metadata vượt scope child.")
        latest.setdefault(row["logical_key"], row)
    ordered_rows = tuple(
        sorted(
            latest.values(),
            key=lambda row: (
                row["open_time"].astimezone(UTC),
                row["source"],
                row["symbol"],
                row["timeframe"],
            ),
        )
    )
    bars = tuple(
        MarketBarIdentityMetadata(
            source_identity=MarketBarSourceIdentity(
                workspace_id=workspace_id,
                record_id=row["id"],
                logical_key=row["logical_key"],
                revision=row["revision"],
                supersedes_record_id=row["supersedes_id"],
                checksum=row["checksum"],
            ),
            source=row["source"],
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            open_time=row["open_time"],
            close_time=row["close_time"],
            available_at=row["available_at"],
            is_closed=row["is_closed"],
            raw_reference=(
                None if row["raw_reference_id"] is None else str(row["raw_reference_id"])
            ),
        )
        for row in ordered_rows
    )
    if dataset.expected_bar_count < 1 or len(bars) != dataset.expected_bar_count:
        raise BacktestRepositoryConflict("MarketBar set không đủ expected child count.")
    duration = (dataset.range_end - dataset.warmup_start) / dataset.expected_bar_count
    expected_times = {
        dataset.warmup_start + duration * index for index in range(dataset.expected_bar_count)
    }
    actual_times = {bar.open_time for bar in bars}
    if len(actual_times) != len(bars) or actual_times != expected_times:
        raise BacktestRepositoryConflict("MarketBar open_time không khớp exact child grid.")
    return bars


def _validate_existing(row: Any, attribute: str, payload: str) -> None:
    if isinstance(row, BacktestTradeOutcomeRecord):
        _validate_trade_outcome_row(row)
    if getattr(row, attribute) != payload:
        raise BacktestRepositoryConflict("Khóa idempotency đã tồn tại với payload khác.")


def _validate_opportunity_links(
    workspace_id: UUID,
    run: SensitivityRun,
    trades: tuple[EvaluationTrade, ...],
    opportunities: tuple[ReplayDecisionOpportunity, ...],
) -> None:
    if type(opportunities) is not tuple or any(
        type(item) is not ReplayDecisionOpportunity for item in opportunities
    ):
        raise ValueError("opportunities phải là tuple contract bất biến.")
    if not opportunities:
        if any(trade.entry_opportunity_event_id is not None for trade in trades):
            raise ValueError("Entry opportunity link thiếu tập opportunity cùng trial.")
        return
    manifest = run.manifest
    if (
        run.scenario.kind != "BASE"
        or manifest.learning_intent != "TRAINING_BASE"
        or not isinstance(run.result, EvaluationReport)
    ):
        raise ValueError("Opportunity chỉ được append cùng BASE TRAINING_BASE đã hoàn tất.")
    if len(opportunities) != run.result.observation_count or tuple(
        item.decision_ordinal for item in opportunities
    ) != tuple(range(len(opportunities))):
        raise ValueError("Opportunity count/ordinal không khớp trial.")
    by_event = {item.event_id: item for item in opportunities}
    if len(by_event) != len(opportunities):
        raise ValueError("Opportunity event_id bị trùng.")
    for item in opportunities:
        if (
            item.workspace_id != workspace_id
            or item.manifest_hash != manifest.content_hash
            or item.bundle_hash != manifest.dataset_hash
            or item.scenario_id != run.scenario.scenario_id
            or item.strategy_release_id != manifest.strategy_release_id
            or item.strategy_content_hash != manifest.strategy_content_hash
            or item.evaluation_start != manifest.range_start
            or item.range_end != manifest.range_end
            or item.content_hash != _sha256(item.canonical_json())
        ):
            raise ValueError("Opportunity scope/hash không khớp workspace/trial.")
    linked_candidates = set()
    for trade in trades:
        entry_opportunity = by_event.get(trade.entry_opportunity_event_id or "")
        if (
            entry_opportunity is None
            or entry_opportunity.input_state != "EVALUATED"
            or entry_opportunity.strategy_state != "CANDIDATE"
            or entry_opportunity.decision_outcome != "PAPER_INTENT_CREATED"
            or (
                trade.entry_candidate_id != entry_opportunity.candidate_id
                or trade.entry_decision_snapshot_id != entry_opportunity.runtime_snapshot_id
                or trade.entry_decision_at != entry_opportunity.decision_at
                or trade.entry_symbol != entry_opportunity.symbol
                or trade.entry_feature_schema_hash != entry_opportunity.feature_schema_hash
                or trade.entry_vector_hash != entry_opportunity.vector_hash
                or trade.entry_features_hash != entry_opportunity.feature_values_hash
            )
        ):
            raise ValueError(
                "Entry outcome không khớp exact opportunity/candidate/snapshot/schema/vector."
            )
        if trade.entry_candidate_id in linked_candidates:
            raise ValueError("Entry candidate không được nối nhiều outcome.")
        linked_candidates.add(trade.entry_candidate_id)


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
    payload: dict[str, Any] = {
        "trade_id": trade.trade_id,
        "opened_at": trade.opened_at.isoformat(),
        "closed_at": trade.closed_at.isoformat(),
        "gross_pnl": str(trade.gross_pnl),
        "fees": str(trade.fees),
        "funding": str(trade.funding),
        "slippage": str(trade.slippage),
        "exposure": str(trade.exposure),
    }
    if trade.has_entry_provenance:
        assert trade.entry_decision_at is not None
        payload.update(
            {
                "entry_decision_snapshot_id": trade.entry_decision_snapshot_id,
                "entry_decision_at": trade.entry_decision_at.isoformat(),
                "entry_feature_schema_hash": trade.entry_feature_schema_hash,
                "entry_features": [list(item) for item in trade.entry_features],
                "entry_features_hash": trade.entry_features_hash,
                "entry_symbol": trade.entry_symbol,
            }
        )
    if trade.entry_opportunity_event_id is not None:
        payload.update(
            {
                "entry_opportunity_event_id": trade.entry_opportunity_event_id,
                "entry_candidate_id": trade.entry_candidate_id,
                "entry_vector_hash": trade.entry_vector_hash,
            }
        )
    return payload


def _validate_trade_outcome_row(row: BacktestTradeOutcomeRecord) -> EvaluationTrade:
    """Một đường kiểm schema/hash/cột dùng chung cho loader và retry."""
    legacy_keys = {
        "trade_id",
        "opened_at",
        "closed_at",
        "gross_pnl",
        "fees",
        "funding",
        "slippage",
        "exposure",
    }
    entry_keys = {
        "entry_decision_snapshot_id",
        "entry_decision_at",
        "entry_feature_schema_hash",
        "entry_features",
        "entry_features_hash",
        "entry_symbol",
    }
    link_keys = {"entry_opportunity_event_id", "entry_candidate_id", "entry_vector_hash"}
    if type(row.outcome) is not dict or set(row.outcome) not in (
        legacy_keys,
        legacy_keys | entry_keys,
        legacy_keys | entry_keys | link_keys,
    ):
        raise BacktestRepositoryConflict("Outcome payload không đúng schema trade.")
    try:
        if _canonical(row.outcome) != row.canonical_json:
            raise ValueError("Canonical outcome không khớp JSON đã lưu.")
        if _sha256(row.canonical_json) != row.outcome_hash:
            raise ValueError("Hash outcome đã lưu không khớp payload.")
        trade = _trade_from_payload(row.outcome)
        if (
            trade.trade_id != row.trade_id
            or _canonical(_trade_payload(trade)) != row.canonical_json
        ):
            raise ValueError("Trade không khớp payload outcome chuẩn hóa.")
        for key in entry_keys | link_keys:
            expected = getattr(trade, key)
            actual = getattr(row, key)
            if key == "entry_features":
                expected = [list(item) for item in expected] if trade.has_entry_provenance else None
                if _canonical(actual) != _canonical(expected):
                    raise ValueError("Cột vector entry không khớp payload.")
            elif actual != expected:
                raise ValueError("Cột entry provenance không khớp payload.")
        return trade
    except (ValueError, TypeError, KeyError, ArithmeticError) as error:
        raise BacktestRepositoryConflict("Outcome sai canonical/hash/entry provenance.") from error


def _trade_from_payload(payload: dict[str, Any]) -> EvaluationTrade:
    entry: dict[str, Any] = {}
    if "entry_decision_snapshot_id" in payload:
        features = payload["entry_features"]
        if type(features) is not list or any(
            type(pair) is not list or len(pair) != 2 for pair in features
        ):
            raise ValueError("Vector entry JSON phải là danh sách các cặp.")
        if not isinstance(payload["entry_decision_at"], str):
            raise ValueError("entry_decision_at phải là chuỗi ISO có múi giờ.")
        entry = {
            "entry_decision_snapshot_id": payload["entry_decision_snapshot_id"],
            "entry_decision_at": datetime.fromisoformat(payload["entry_decision_at"]),
            "entry_feature_schema_hash": payload["entry_feature_schema_hash"],
            "entry_features": tuple(tuple(pair) for pair in features),
            "entry_features_hash": payload["entry_features_hash"],
            "entry_symbol": payload["entry_symbol"],
        }
    if "entry_opportunity_event_id" in payload:
        entry.update(
            {
                key: payload[key]
                for key in ("entry_opportunity_event_id", "entry_candidate_id", "entry_vector_hash")
            }
        )
    return EvaluationTrade(
        trade_id=str(payload["trade_id"]),
        opened_at=datetime.fromisoformat(str(payload["opened_at"])),
        closed_at=datetime.fromisoformat(str(payload["closed_at"])),
        gross_pnl=Decimal(str(payload["gross_pnl"])),
        fees=Decimal(str(payload["fees"])),
        funding=Decimal(str(payload["funding"])),
        slippage=Decimal(str(payload["slippage"])),
        exposure=Decimal(str(payload["exposure"])),
        **entry,
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
