"""SC-4 (CRY-35): Bất biến một nguồn registry duy nhất và proof WP06 cho Strategy Card."""

from __future__ import annotations

import ast
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from tests.unit.test_candidate_universe_proof import WORKSPACE, MemorySession
from tests.unit.test_learning_feature_vector import BASE_TIME, _market_row, _rights

from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord
from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.adapters.database.replay_training_rights import (
    DatabaseReplayTrainingRightsGate,
    ReplayTrainingOperation,
    ReplayTrainingRightsReceipt,
)
from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
    BacktestDatasetManifestRepository,
)
from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
    BacktestDatasetReplayBundleRepository,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
    CandidateUniverseProofError,
    CandidateUniverseProofRepository,
)
from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
    LearningScorerTestOpportunityProofRepository,
)
from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundle,
    BacktestDatasetReplayBundleReference,
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.backtest_replay_executor import build_backtest_replay_plan
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.binance_rest_backfill import BackfillRightsPurpose, RestBackfillRequest
from trading_bot.application.candidate_universe_proof import CandidateUniverseProofService
from trading_bot.application.strategy_catalog import (
    PRODUCT_CARD_IDS,
    expanded_rules,
    fx8_rules,
    scalping_rules,
    strategy_card_rules,
    strategy_registry,
)
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar
from trading_bot.domain.strategy.registry import StrategyRegistry

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "trading_bot"
CATALOG_PATH = SRC_ROOT / "application" / "strategy_catalog.py"
REGISTRY_DEF_PATH = SRC_ROOT / "domain" / "strategy" / "registry.py"

_INTERVALS = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


def _iter_python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py")))


def test_no_bare_strategy_registry_instantiation_outside_strategy_catalog() -> None:
    """Cấm gọi `StrategyRegistry(...)` trực tiếp ngoài `strategy_catalog.py`."""
    violations: list[str] = []
    class_defs: list[Path] = []

    for path in _iter_python_files(SRC_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases: set[str] = {"StrategyRegistry"}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "trading_bot.domain.strategy.registry"
            ):
                for item in node.names:
                    if item.name == "StrategyRegistry" and item.asname:
                        aliases.add(item.asname)
            elif isinstance(node, ast.ClassDef) and node.name == "StrategyRegistry":
                class_defs.append(path)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            call_name: str | None = None
            if isinstance(func, ast.Name) and func.id in aliases:
                call_name = func.id
            elif isinstance(func, ast.Attribute) and func.attr in aliases:
                call_name = func.attr
            if call_name is not None and path != CATALOG_PATH:
                rel = path.relative_to(SRC_ROOT.parent)
                violations.append(f"{rel}:{node.lineno} gọi {call_name}(...)")

    assert class_defs == [REGISTRY_DEF_PATH]
    assert violations == [], (
        "Phát hiện nơi gọi StrategyRegistry(...) ngoài application/strategy_catalog.py:\n"
        + "\n".join(violations)
    )


def test_domain_ports_and_adapters_do_not_import_strategy_catalog() -> None:
    """Tầng domain, ports và adapters không được import ngược `application.strategy_catalog`."""
    forbidden_layers = (SRC_ROOT / "domain", SRC_ROOT / "ports", SRC_ROOT / "adapters")
    violations: list[str] = []

    for layer_root in forbidden_layers:
        for path in _iter_python_files(layer_root):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if "strategy_catalog" in node.module:
                        rel = path.relative_to(SRC_ROOT.parent)
                        violations.append(f"{rel}:{node.lineno} import {node.module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if "strategy_catalog" in alias.name:
                            rel = path.relative_to(SRC_ROOT.parent)
                            violations.append(f"{rel}:{node.lineno} import {alias.name}")

    assert violations == [], (
        "Phát hiện import ngược lên strategy_catalog từ domain/ports/adapters:\n"
        + "\n".join(violations)
    )


def test_candidate_universe_proofs_uses_port_without_importing_domain_strategy_registry() -> None:
    """`candidate_universe_proofs.py` chỉ dùng port tra cứu, không import `StrategyRegistry`."""
    proof_repo_path = (
        SRC_ROOT / "adapters" / "database" / "repositories" / "candidate_universe_proofs.py"
    )
    tree = ast.parse(proof_repo_path.read_text(encoding="utf-8"), filename=str(proof_repo_path))
    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.append(f"{node.module}.{alias.name}")
    assert "trading_bot.domain.strategy.registry.StrategyRegistry" not in imported_names
    assert any("ports.strategy_registry" in item for item in imported_names)


def test_single_catalog_exposes_all_cards_scalping_and_expanded_releases() -> None:
    """`strategy_registry()` chứa đủ built-ins, 8 cards, 3 scalping, 6 expanded và 5 FX-8."""
    catalog = strategy_registry()
    bare = StrategyRegistry()

    catalog_ids = {item.release_id for item in catalog.list_releases()}
    bare_ids = {item.release_id for item in bare.list_releases()}
    card_ids = {rule.release_id for rule, _, _, _ in strategy_card_rules()}
    scalp_ids = {rule.release_id for rule, _, _, _ in scalping_rules()}
    exp_ids = {rule.release_id for rule, _, _, _ in expanded_rules()}
    fx8_ids = {rule.release_id for rule, _, _, _ in fx8_rules()}

    assert len(PRODUCT_CARD_IDS) == 8
    assert len(card_ids) == 8
    assert len(scalp_ids) == 3
    assert len(exp_ids) == 6
    assert len(fx8_ids) == 5
    assert bare_ids.isdisjoint(card_ids | scalp_ids | exp_ids | fx8_ids)
    assert catalog_ids == bare_ids | card_ids | scalp_ids | exp_ids | fx8_ids


def _build_bars(timeframe: str, *, count: int, range_end) -> tuple[OHLCVBar, ...]:
    interval = _INTERVALS[timeframe]
    bars: list[OHLCVBar] = []
    for index in range(count):
        opened = range_end - interval * (count - index)
        closed = opened + interval - timedelta(milliseconds=1)
        checksum = hashlib.sha256(f"sc4:{timeframe}:{index}".encode()).hexdigest()
        bars.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe=timeframe,
                open_time=opened,
                close_time=closed,
                event_at=closed,
                received_at=closed,
                available_at=closed,
                open=Decimal("100") + Decimal(index) / Decimal("10"),
                high=Decimal("103") + Decimal(index) / Decimal("10"),
                low=Decimal("99") + Decimal(index) / Decimal("10"),
                close=Decimal("102") + Decimal(index) / Decimal("10"),
                volume=Decimal("200"),
                quote_volume=Decimal("20400"),
                taker_buy_base_volume=Decimal("110"),
                is_closed=True,
                source="BINANCE_USDM_REST_BACKFILL",
                source_cursor=f"{timeframe}:{opened.isoformat()}",
                checksum=checksum,
                raw_reference=str(uuid5(WORKSPACE, f"raw:{timeframe}:{opened.isoformat()}")),
                source_identity=MarketBarSourceIdentity(
                    workspace_id=WORKSPACE,
                    record_id=uuid5(WORKSPACE, f"{timeframe}:{opened.isoformat()}"),
                    logical_key=f"market:{timeframe}:{opened.isoformat()}",
                    revision=1,
                    supersedes_record_id=None,
                    checksum=checksum,
                ),
            )
        )
    return tuple(bars)


def _build_replay_bundle(
    bars_by_timeframe: dict[str, tuple[OHLCVBar, ...]],
    *,
    primary_timeframe: str,
) -> BacktestDatasetReplayBundle:
    primary_bars = bars_by_timeframe[primary_timeframe]
    evaluation_start = primary_bars[-4].open_time
    range_end = primary_bars[-1].open_time + _INTERVALS[primary_timeframe]
    inputs: list[BacktestDatasetReplayInput] = []
    for timeframe, bars in bars_by_timeframe.items():
        interval = _INTERVALS[timeframe]
        rights = _rights(range_end)
        request = RestBackfillRequest(
            symbol="BTCUSDT",
            interval=timeframe,
            start=bars[0].open_time,
            end=range_end,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        )
        manifest = build_backtest_dataset_manifest(
            request,
            evaluation_start=evaluation_start,
            warmup_bars=int((evaluation_start - bars[0].open_time) / interval),
            bars=bars,
            rights_evidence=rights,
            prepared_at=range_end,
            schema_version=4,
        )
        inputs.append(BacktestDatasetReplayInput(manifest, bars, rights))
    return build_backtest_dataset_replay_bundle(tuple(inputs))


async def _persist_training_base_replay(
    monkeypatch: pytest.MonkeyPatch,
    *,
    release_id: str,
    execution_interval: str,
    bars_by_timeframe: dict[str, tuple[OHLCVBar, ...]],
):
    """Tạo và lưu một lượt replay TRAINING_BASE hoàn chỉnh cho release bất kỳ trong catalog."""
    source = _build_replay_bundle(bars_by_timeframe, primary_timeframe=execution_interval)
    job_id = uuid4()
    submission = BacktestReplayJobSubmission(
        bundle_hash=source.dataset_hash,
        strategy_release_id=release_id,
        execution_interval=execution_interval,
        starting_equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0"),
        seed=23,
        learning_intent="TRAINING_BASE",
    )
    plan = build_backtest_replay_plan(WORKSPACE, job_id, source, submission)
    comparison = plan.executor(source, plan.manifest)
    assert comparison.is_complete
    replay = comparison.runs[0]

    session = MemorySession()
    session.clock = source.range_end + timedelta(days=1)
    session.events = []
    session.source_rows = {
        item.manifest.interval: [MarketBar(**vars(_market_row(bar))) for bar in item.bars]
        for item in source.components
    }
    original_execute = session.execute

    async def execute(statement, parameters=None):
        sql = str(statement)
        if "market_bar" in sql:
            session.events.append("source_scan")
            values = statement.compile().params
            timeframe = next(value for key, value in values.items() if key.startswith("timeframe"))
            dataset = next(
                item.manifest for item in source.components if item.manifest.interval == timeframe
            )
            rows = [
                row
                for row in session.source_rows[timeframe]
                if row.available_at <= dataset.prepared_at
                and row.is_closed
                and dataset.warmup_start <= row.open_time < dataset.range_end
            ]
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    all=lambda: [
                        {
                            column.key: getattr(row, column.key)
                            for column in statement.selected_columns
                        }
                        for row in rows
                    ]
                )
            )
        if "pg_advisory_xact_lock" in sql:
            session.events.append("source_lock")
        if "wp06_lock_candidate_trial" in sql:
            session.events.append("trial_lock")
        return await original_execute(statement, parameters)

    session.execute = execute
    receipt = await BacktestOutcomeRepository(session).append_sensitivity_run(
        workspace_id=WORKSPACE,
        sensitivity_run=replay.sensitivity_run,
        trade_outcomes=replay.trade_outcomes,
        opportunities=replay.opportunities,
    )
    payload = submission.to_payload()
    session.add(
        BacktestReplayJobRecord(
            id=job_id,
            workspace_id=WORKSPACE,
            requested_by=uuid4(),
            bundle_hash=submission.bundle_hash,
            strategy_release_id=submission.strategy_release_id,
            execution_interval=submission.execution_interval,
            request_payload=payload,
            canonical_request=json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            status="SUCCEEDED",
            stage="completed",
            progress=100,
            cancel_requested=False,
            attempt_count=1,
            lease_generation=1,
            result_run_ids=[str(receipt.run_id)],
            created_at=session.clock - timedelta(seconds=1),
            updated_at=session.clock,
        )
    )

    async def bundle_lookup(self, **kwargs):
        return BacktestDatasetReplayBundleReference.from_bundle(source)

    async def child_lookup(self, **kwargs):
        return next(
            item.manifest
            for item in source.components
            if item.manifest.dataset_hash == kwargs["dataset_hash"]
        )

    async def authorize(self, **kwargs):
        session.events.append("training_rights")
        return tuple(
            ReplayTrainingRightsReceipt(
                workspace_id=WORKSPACE,
                bundle_hash=source.dataset_hash,
                child_dataset_hash=item.manifest.dataset_hash,
                interval=item.manifest.interval,
                symbol=source.symbol,
                bars_content_hash=item.manifest.bars_content_hash,
                product="FUTURES",
                venue="BINANCE",
                contract="USD_M_PERPETUAL",
                data_start=item.manifest.warmup_start,
                data_end=item.manifest.range_end,
                policy_id="fixture:training",
                policy_fingerprint="4" * 64,
                bundle_policy_fingerprint=source.policy_fingerprint,
                current_policy_fingerprint="4" * 64,
                terms_url="https://example.invalid/training",
                verified_at=session.clock - timedelta(days=1),
                checked_at=session.clock,
                valid_until=session.clock + timedelta(minutes=5),
                license_evidence_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                evidence_digest="6" * 64,
                license_scope_hash="7" * 64,
                scope_hash="8" * 64,
                operation=ReplayTrainingOperation.TRAINING,
            )
            for item in source.components
        )

    monkeypatch.setattr(BacktestDatasetReplayBundleRepository, "get_by_hash", bundle_lookup)
    monkeypatch.setattr(BacktestDatasetManifestRepository, "get_by_hash", child_lookup)
    monkeypatch.setattr(DatabaseReplayTrainingRightsGate, "authorize", authorize)
    return session, receipt, source, replay


def _vol_breakout_bars() -> dict[str, tuple[OHLCVBar, ...]]:
    """Chuỗi nến 1h đủ warmup kèm nến Hammer kích hoạt `sc-binance-usdm-vol-breakout-001-v1.0.0`."""
    range_end = BASE_TIME + timedelta(hours=620)
    bars = list(_build_bars("1h", count=620, range_end=range_end))
    # Tại nến áp chót (trong evaluation window 4 nến cuối), tạo nến Hammer râu dưới dài
    # và nến kế tiếp chạm mục tiêu chốt lời để sinh cả candidate lẫn closed trade outcome.
    signal_idx = len(bars) - 2
    base_bar = bars[signal_idx]
    hammer_checksum = "a" * 64
    bars[signal_idx] = replace(
        base_bar,
        open=Decimal("100.0"),
        high=Decimal("100.4"),
        low=Decimal("95.0"),
        close=Decimal("100.2"),
        checksum=hammer_checksum,
        source_identity=replace(base_bar.source_identity, checksum=hammer_checksum),
    )
    exit_bar = bars[signal_idx + 1]
    exit_checksum = "b" * 64
    bars[signal_idx + 1] = replace(
        exit_bar,
        open=Decimal("100.2"),
        high=Decimal("112.0"),
        low=Decimal("100.0"),
        close=Decimal("111.0"),
        checksum=exit_checksum,
        source_identity=replace(exit_bar.source_identity, checksum=exit_checksum),
    )
    return {"1h": tuple(bars)}


@pytest.mark.asyncio
async def test_wp06_proof_accepts_strategy_card_release_via_injected_port_and_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proof WP06 (cả repository nhận port lẫn `CandidateUniverseProofService`) thấy release thẻ."""
    release_id = "sc-binance-usdm-vol-breakout-001-v1.0.0"
    session, receipt, _, replay = await _persist_training_base_replay(
        monkeypatch,
        release_id=release_id,
        execution_interval="1h",
        bars_by_timeframe=_vol_breakout_bars(),
    )
    assert len(replay.opportunities) == 4
    assert any(item.strategy_state == "CANDIDATE" for item in replay.opportunities)

    @asynccontextmanager
    async def begin():
        yield

    @asynccontextmanager
    async def session_factory():
        yield session

    session.begin = begin  # type: ignore[attr-defined]
    service = CandidateUniverseProofService(session_factory)  # type: ignore[arg-type]

    closed_result = await service.build(
        workspace_id=WORKSPACE, trial_record_id=receipt.trial_record_id
    )
    assert closed_result.proof.query_version == "base-closed-trade-universe-v1"
    assert closed_result.proof.candidate_count == len(replay.trade_outcomes)

    opp_result = await service.build_decision_opportunity(
        workspace_id=WORKSPACE, trial_record_id=receipt.trial_record_id
    )
    assert opp_result.proof.completeness_verified is True
    assert opp_result.proof.opportunity_count == 4
    assert opp_result.proof.candidate_count >= 1

    # Gọi trực tiếp qua repository với hàm tra cứu release (port callable) trả duplicate khớp.
    lookup_repo = CandidateUniverseProofRepository(
        session, release_lookup=strategy_registry().get_release
    )
    retry = await lookup_repo.build_decision_opportunity(
        workspace_id=WORKSPACE, trial_record_id=receipt.trial_record_id
    )
    assert retry.duplicate is True
    assert retry.record_id == opp_result.record_id
    assert retry.proof.content_hash == opp_result.proof.content_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("release_id", "execution_interval", "timeframes"),
    [
        ("sc-binance-usdm-mean-revert-002-v1.2.1", "15m", ("15m",)),
        ("sc-binance-usdm-tsmom-003-v1.0.0", "4h", ("4h",)),
        ("h02-v1", "4h", ("4h",)),
        ("h04-v1", "15m", ("15m",)),
    ],
)
async def test_wp06_proof_accepts_expanded_cards_and_expanded_rules(
    monkeypatch: pytest.MonkeyPatch,
    release_id: str,
    execution_interval: str,
    timeframes: tuple[str, ...],
) -> None:
    """Proof WP06 xác nhận được cả Strategy Card mở rộng lẫn rule trong `expanded_rules.py`."""
    range_end = BASE_TIME + timedelta(days=50)
    bars_by_tf = {tf: _build_bars(tf, count=620, range_end=range_end) for tf in timeframes}
    session, receipt, _, _ = await _persist_training_base_replay(
        monkeypatch,
        release_id=release_id,
        execution_interval=execution_interval,
        bars_by_timeframe=bars_by_tf,
    )
    repo = CandidateUniverseProofRepository(session, strategy_registry=strategy_registry())
    result = await repo.build_decision_opportunity(
        workspace_id=WORKSPACE, trial_record_id=receipt.trial_record_id
    )
    assert result.proof.completeness_verified is True
    assert result.proof.opportunity_count == 4


@pytest.mark.asyncio
async def test_wp06_proof_fails_closed_when_port_missing_or_registry_lacks_card_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thiếu port tra cứu hoặc registry thiếu release của thẻ đều phải fail-closed."""
    release_id = "sc-binance-usdm-vol-breakout-001-v1.0.0"
    session, receipt, _, _ = await _persist_training_base_replay(
        monkeypatch,
        release_id=release_id,
        execution_interval="1h",
        bars_by_timeframe=_vol_breakout_bars(),
    )

    missing_port_repo = CandidateUniverseProofRepository(session)
    with pytest.raises(CandidateUniverseProofError, match="Thiếu port"):
        await missing_port_repo.build_decision_opportunity(
            workspace_id=WORKSPACE, trial_record_id=receipt.trial_record_id
        )

    bare_repo = CandidateUniverseProofRepository(session, strategy_registry=StrategyRegistry())
    with pytest.raises(CandidateUniverseProofError, match="Bundle/strategy/window"):
        await bare_repo.build_decision_opportunity(
            workspace_id=WORKSPACE, trial_record_id=receipt.trial_record_id
        )


def test_learning_scorer_test_proof_repository_forwards_strategy_registry_port() -> None:
    """`LearningScorerTestOpportunityProofRepository` chuyển tiếp registry port vào proof repo."""
    registry = strategy_registry()
    repo = LearningScorerTestOpportunityProofRepository(
        SimpleNamespace(), strategy_registry=registry
    )
    proof_repo = repo._proof_repository()
    assert isinstance(proof_repo, CandidateUniverseProofRepository)
    assert proof_repo._release_lookup is not None
    release = proof_repo._release_lookup("sc-binance-usdm-vol-breakout-001-v1.0.0")
    assert (
        release.content_hash
        == registry.get_release("sc-binance-usdm-vol-breakout-001-v1.0.0").content_hash
    )
