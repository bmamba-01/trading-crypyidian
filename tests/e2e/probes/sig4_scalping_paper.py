"""Probe SIG-4 (Compose): H18 → thang chốt lời → ledger PAPER chốt từng phần trên PostgreSQL thật.

Thời gian lấy từ dữ liệu tổng hợp (Thứ Hai 02/11/2026, phiên London) và đồng hồ tiêm vào các
repository (`clock=`), nên bộ lọc phiên/funding không phụ thuộc giờ chạy E2E. Không gọi sàn: socket
Python bị chặn ở pha `lifecycle`/`replay` (driver PostgreSQL dùng libpq, không qua socket Python).

- `seed` (migrator): workspace, account PAPER (bot tự tạo), root settings đã publish, release
  `h18-v1` PUBLISHED, một hàng `feature_snapshot` cho lineage của intent.
- `lifecycle` (trading-runtime): snapshot từ builder thật → `PaperDecisionCycle` (registry sản phẩm)
  → `RiskReservationRepository.record_evaluation/reserve` → `PaperLifecycleCoordinator` với ledger
  + checkpoint thật: submit → khớp nến 10:05 → TP1 (fill `TAKE_PROFIT_1`, `STOP_UPDATED` hòa vốn)
  → TP2 đóng trade → coordinator mới hydrate từ checkpoint (recovery đối soát ledger).
- `replay` (db-provisioner): replay PAPER H18, lưu outcome qua
  `BacktestDatasetReplayExecutionService` bằng role `crypidian_replay_writer`.
"""

import asyncio
import hashlib
import json
import os
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.types.json import Jsonb

STATE = json.loads(os.environ["SIG4_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
FIXTURE = __import__("sig4_runtime_replay")
# PostgreSQL ràng buộc `risk_reservation.expires_at > created_at (= now())`, nên kịch bản ledger
# chạy ở cùng thứ/giờ của một tuần tương lai; mọi repository khác nhận đồng hồ tiêm vào.
DAY = FIXTURE.same_slot_after(datetime.fromisoformat(STATE["now"]))
SIGNAL_CLOSE = DAY + timedelta(hours=10, minutes=5)
# Nến tín hiệu đóng/available ở 10:04:59.999; quyết định ngay sau đó để lệnh khớp ở giá mở 10:05.
DECISION_TIME = SIGNAL_CLOSE - timedelta(milliseconds=1)
EVALUATED_AT = SIGNAL_CLOSE - timedelta(microseconds=500)


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"sig4-scalping-paper:{WORKSPACE}:{name}")


ACCOUNT = _id("account")
MEMBERSHIP = _id("membership")
SETTINGS = _id("root-settings")
RISK_PROFILE = _id("risk-profile")
RELEASE = _id("release-h18")
FEATURE_SNAPSHOT = hashlib.sha256(f"sig4:{WORKSPACE}".encode()).hexdigest()


def _settings_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime": {"mode": "PAPER", "timezone": "UTC"},
        "market_data": {
            "venue": "BINANCE_USDM",
            "symbols": ["BTCUSDT"],
            "timeframes": ["5m", "15m"],
            "max_age_seconds": 900,
        },
        "features": {"formula_version": "crypidian-features-v1", "sessions_timezone": "UTC"},
        "strategy": {
            "default_release": "h18-v1",
            "parameters": {},
            "entry_timeframe": "5m",
            "confirmation_timeframe": "15m",
        },
        "risk": {
            "profile_version_id": str(RISK_PROFILE),
            "max_equity_age_seconds": 3600,
            "max_mark_age_seconds": 3600,
        },
        "paper_execution": {
            "quote_asset": "USDT",
            "starting_equity": "10000",
            "fee_schedule_version": "vip0-taker-v1",
            "fill_model": "next_bar_with_adverse_costs",
        },
    }


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip(),
        autocommit=True,
    )


def seed() -> dict[str, Any]:
    from trading_bot.adapters.database.repositories.workspace_settings import (
        canonical_settings,
        settings_hash,
    )
    from trading_bot.application.strategy_catalog import strategy_registry
    from trading_bot.config.workspace_settings import WorkspaceSettings

    payload = canonical_settings(WorkspaceSettings.model_validate(_settings_payload()))
    root_hash = settings_hash(payload)
    effective = DAY - timedelta(days=1)
    with _connect() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO workspace (id, name, slug, mode, status) "
            "VALUES (%s, 'SIG-4 scalping E2E', %s, 'PAPER', 'ACTIVE')",
            (WORKSPACE, "sig4-" + WORKSPACE.hex[:12]),
        )
        owner = _id("owner")
        connection.execute(
            "INSERT INTO user_account (id, email_normalized, password_hash, status) "
            "VALUES (%s, %s, 'e2e-unused', 'ACTIVE')",
            (owner, f"sig4-{WORKSPACE.hex[:12]}@example.invalid"),
        )
        connection.execute(
            "INSERT INTO user_membership (id, workspace_id, user_id, status) "
            "VALUES (%s, %s, %s, 'ACTIVE')",
            (MEMBERSHIP, WORKSPACE, owner),
        )
        connection.execute(
            "INSERT INTO trading_account (id, workspace_id, name, venue, mode, environment, "
            "status, max_notional) VALUES (%s, %s, 'SIG-4 PAPER', 'binance_usdm', 'PAPER', "
            "'local', 'ENABLED', 10000)",
            (ACCOUNT, WORKSPACE),
        )
        bot_id = connection.execute(
            "SELECT id FROM bot_instance WHERE workspace_id=%s AND trading_account_id=%s",
            (WORKSPACE, ACCOUNT),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO workspace_settings_version (id, workspace_id, version, schema_version, "
            "settings_json, settings_hash, status, revision, created_by, approved_by, "
            "published_at, effective_at) VALUES (%s, %s, 1, 1, %s, %s, 'PUBLISHED', 1, %s, %s, "
            "%s, %s)",
            (
                SETTINGS,
                WORKSPACE,
                Jsonb(payload),
                root_hash,
                MEMBERSHIP,
                MEMBERSHIP,
                effective,
                effective,
            ),
        )
        connection.execute(
            "INSERT INTO strategy_release (id, workspace_id, name, version, status, recipe, "
            "content_hash) VALUES (%s, %s, 'h18-v1', 1, 'PUBLISHED', '{}'::jsonb, %s)",
            (RELEASE, WORKSPACE, strategy_registry().get_release("h18-v1").content_hash),
        )
        # Lineage feature của intent; nội dung chi tiết đã được kiểm bởi builder ở pha lifecycle.
        connection.execute("SET LOCAL session_replication_role = replica")
        connection.execute(
            "INSERT INTO feature_snapshot (id, workspace_id, snapshot_id, content_hash, symbol, "
            "timeframe, as_of, computed_at, schema_hash, formula_hash, code_hash, input_bar_ids, "
            "input_event_ids, features, warmup, quality_flags, data_freshness, data_coverage, "
            "regime, is_usable, canonical_json) VALUES (gen_random_uuid(), %s, %s, %s, "
            "'BTCUSDT', '5m', %s, %s, %s, %s, %s, '[]', '[]', '{}', '{}', '[]', 'FRESH', 1.0, "
            "'{}', true, '{}')",
            (
                WORKSPACE,
                FEATURE_SNAPSHOT,
                FEATURE_SNAPSHOT,
                SIGNAL_CLOSE,
                SIGNAL_CLOSE,
                "a" * 64,
                "b" * 64,
                "c" * 64,
            ),
        )
    return {"bot_id": str(bot_id), "settings_hash": root_hash}


class _Clock:
    """Đồng hồ tiêm vào repository; probe đẩy tới theo từng nến của dữ liệu."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def _guard_network() -> list[str]:
    attempted: list[str] = []

    def guarded(self: socket.socket, address: Any) -> Any:
        attempted.append(str(address))
        raise OSError("SIG4_E2E_NETWORK_BLOCKED")

    socket.socket.connect = guarded  # type: ignore[method-assign]
    return attempted


def _paper_bar(bar: Any) -> Any:
    from trading_bot.domain.execution.paper import PaperBar

    closed = bar.open_time + timedelta(minutes=5)
    return PaperBar(
        bar_key=f"BTCUSDT|5m|{bar.open_time.isoformat()}",
        symbol="BTCUSDT",
        opened_at=bar.open_time,
        closed_at=closed,
        available_at=closed,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
    )


async def lifecycle() -> dict[str, Any]:
    from sqlalchemy import select, text

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models.paper_entities import (
        FillRecord,
        PositionEventRecord,
        TradeRecord,
    )
    from trading_bot.adapters.database.repositories.bot_runtime_leases import BotRuntimeLease
    from trading_bot.adapters.database.repositories.paper_checkpoints import (
        PaperCheckpointRepository,
    )
    from trading_bot.adapters.database.repositories.paper_ledger import PaperLedgerRepository
    from trading_bot.adapters.database.repositories.risk_reservations import (
        RiskReservationRepository,
    )
    from trading_bot.application.decision_snapshot_builder import (
        build_decision_snapshot_from_bars,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.paper_decision_cycle import (
        DecisionCycleOutcome,
        PaperDecisionCycle,
    )
    from trading_bot.application.paper_lifecycle import PaperLifecycleCoordinator
    from trading_bot.application.strategy_catalog import strategy_registry
    from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
    from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperExecutionPolicy
    from trading_bot.domain.risk.engine import RiskEngine, RiskState
    from trading_bot.domain.risk.planner import (
        AccountSnapshot,
        CostModel,
        ExchangeRules,
        PlannerSettings,
        RiskLimits,
        TradePlan,
        TradePlanner,
    )

    attempted = _guard_network()
    bars = [bar for bar in FIXTURE.sig4_bars("5m", day=DAY) if bar.close_time < SIGNAL_CLOSE]
    snapshot = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE,
        symbol="BTCUSDT",
        strategy_release_id="h18-v1",
        decision_time=DECISION_TIME,
        series_quality_as_of=DECISION_TIME,
        bars_by_timeframe={"5m": tuple(bars)},
        position_is_flat=True,
        source_identity_required=False,
        legacy_identity_namespace=f"sig4:{WORKSPACE}",
    )
    release = strategy_registry().get_release("h18-v1")
    settings_hash = STATE["settings_hash"]
    published = PublishedWorkspaceSettings(
        workspace_id=WORKSPACE,
        settings_version_id=SETTINGS,
        settings_hash=settings_hash,
        settings=WorkspaceSettings.model_validate(_settings_payload()),
    )
    planner_settings = PlannerSettings(
        workspace_id=str(WORKSPACE),
        settings_version_id=str(SETTINGS),
        risk_profile_version_id=str(RISK_PROFILE),
        published=True,
        active_release_ids=("h18-v1",),
        leverage=Decimal("2"),
        exchange=ExchangeRules(
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            contract_multiplier=Decimal("1"),
            min_notional=Decimal("5"),
            max_notional=Decimal("10000"),
            max_quantity=Decimal("1000"),
        ),
        limits=RiskLimits(
            per_trade_risk_fraction=Decimal("0.01"),
            max_aggregate_open_risk=Decimal("250"),
            max_notional=Decimal("10000"),
            max_leverage=Decimal("5"),
            max_positions=2,
            max_symbol_notional=Decimal("5000"),
            max_concentration_fraction=Decimal("0.5"),
            max_daily_loss=Decimal("200"),
            max_rolling_loss=Decimal("500"),
            max_account_age=timedelta(hours=1),
            max_data_age=timedelta(minutes=10),
        ),
        release_content_hashes=(("h18-v1", release.content_hash),),
        settings_hash=settings_hash,
    )
    account = AccountSnapshot(
        workspace_id=str(WORKSPACE),
        account_id=str(ACCOUNT),
        settings_version_id=str(SETTINGS),
        captured_at=EVALUATED_AT - timedelta(seconds=1),
        equity=Decimal("10000"),
        available_margin=Decimal("10000"),
        is_known=True,
    )
    risk_state = RiskState(
        workspace_id=str(WORKSPACE),
        account_id=str(ACCOUNT),
        settings_version_id=str(SETTINGS),
        risk_profile_version_id=str(RISK_PROFILE),
        evaluated_at=EVALUATED_AT,
        account_captured_at=account.captured_at,
        market_data_as_of=DECISION_TIME,
        equity=account.equity,
        available_margin=account.available_margin,
        open_exposures=(),
        realized_loss_today=Decimal("0"),
        realized_loss_rolling=Decimal("0"),
        open_position_count=0,
        is_known=True,
        pending_reservation_initial_margin=Decimal("0"),
    )
    costs = CostModel(
        "vip0-taker-v1", Decimal("0.0005"), Decimal("0.0005"), Decimal("0"), Decimal("0")
    )
    policy = PaperExecutionPolicy(
        version_id="paper-fill-v1",
        latency=timedelta(0),
        spread_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
        fee_rate=Decimal("0.0005"),
    )
    gateway = PaperExecutionGateway(policy)
    decision = PaperDecisionCycle(
        strategy_registry(), TradePlanner(), RiskEngine(), gateway
    ).prepare_published(
        published_settings=published,
        snapshot=snapshot,
        evaluated_at=EVALUATED_AT,
        planner_settings=planner_settings,
        account=account,
        risk_state=risk_state,
        open_exposures=(),
        cost_model=costs,
    )
    assert decision.outcome is DecisionCycleOutcome.RISK_APPROVED, (
        decision.outcome,
        getattr(decision.strategy_result, "code", None),
        getattr(decision.plan, "reason_code", None),
        getattr(decision.risk_decision, "reason_code", None),
    )
    plan = decision.plan
    assert isinstance(plan, TradePlan)

    clock = _Clock(EVALUATED_AT)
    ledger = PaperLedgerRepository(clock=clock)
    checkpoints = PaperCheckpointRepository()
    coordinator = PaperLifecycleCoordinator(gateway, ledger, checkpoints)
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    idempotency_key = f"sig4:{WORKSPACE}:h18"
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await uow.bind_account(ACCOUNT)
            risk = RiskReservationRepository(uow.session, clock=clock)
            evaluation = await risk.record_evaluation(plan=plan, decision=decision.risk_decision)
            reservation = await risk.reserve(
                decision.risk_decision,
                idempotency_key=idempotency_key,
                risk_decision_id=evaluation.risk_decision_id,
            )
            submission = await coordinator.submit_approved(
                uow,
                account_id=ACCOUNT,
                result=decision,
                evaluated_at=EVALUATED_AT,
                idempotency_key=idempotency_key,
                strategy_release_id=RELEASE,
                feature_snapshot_id=FEATURE_SNAPSHOT,
                risk_decision_id=evaluation.risk_decision_id,
                risk_reservation_id=reservation.reservation_id,
                quote_asset="USDT",
                evidence={
                    "settings_hash": settings_hash,
                    "paper_execution_profile_hash": "e" * 64,
                    "plan_hash": plan.plan_hash,
                },
            )
        paper_intent_id = submission.order.intent_id
        ledger_intent_id = submission.ledger_intent_id
        # MB-05: checkpoint genesis (revision 1) được ghi không token; từ revision 2 phải có
        # lease runtime hiện hành và fencing token khớp (trigger DB kiểm bằng đồng hồ thật).
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await uow.bind_account(ACCOUNT)
            lease = BotRuntimeLease(uow.session)
            db_now = await lease.database_now()
            token = await lease.acquire(
                WORKSPACE, UUID(STATE["bot_id"]), "sig4-e2e", db_now + timedelta(seconds=60)
            )

        path = {
            bar.open_time: _paper_bar(bar)
            for bar in FIXTURE.sig4_bars("5m", day=DAY)
            if SIGNAL_CLOSE <= bar.open_time < SIGNAL_CLOSE + timedelta(minutes=15)
        }
        entry_bar, tp1_bar, tp2_bar = (path[key] for key in sorted(path))
        clock.moment = entry_bar.closed_at
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await uow.bind_account(ACCOUNT)
            entry = await coordinator.apply_entry_market_event(
                uow,
                account_id=ACCOUNT,
                paper_intent_id=paper_intent_id,
                ledger_intent_id=ledger_intent_id,
                bar=entry_bar,
                fencing_token=token,
            )
        trade_id = entry.trade_id
        assert trade_id is not None, "Khớp lệnh vào không mở trade"
        results = []
        for bar in (tp1_bar, tp2_bar):
            clock.moment = bar.closed_at
            position = gateway.position(paper_intent_id)
            assert position is not None
            async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
                await uow.bind_account(ACCOUNT)
                results.append(
                    await coordinator.monitor_market_event(
                        uow,
                        account_id=ACCOUNT,
                        paper_position_id=position.position_id,
                        ledger_intent_id=ledger_intent_id,
                        trade_id=trade_id,
                        position=position,
                        bar=bar,
                        fencing_token=token,
                    )
                )

        # Khôi phục: gateway mới hydrate từ checkpoint, recovery đối soát với ledger thật.
        restored = PaperExecutionGateway(policy)
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await uow.bind_account(ACCOUNT)
            await PaperLifecycleCoordinator(restored, ledger, checkpoints).hydrate_account(
                uow, account_id=ACCOUNT
            )

        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await uow.bind_account(ACCOUNT)
            fills = (
                await uow.session.scalars(
                    select(FillRecord)
                    .where(
                        FillRecord.workspace_id == WORKSPACE,
                        FillRecord.intent_id == ledger_intent_id,
                    )
                    .order_by(FillRecord.occurred_at, FillRecord.fill_role)
                )
            ).all()
            events = (
                await uow.session.scalars(
                    select(PositionEventRecord)
                    .where(
                        PositionEventRecord.workspace_id == WORKSPACE,
                        PositionEventRecord.intent_id == ledger_intent_id,
                    )
                    .order_by(PositionEventRecord.occurred_at)
                )
            ).all()
            trade = await uow.session.get(TradeRecord, trade_id)
            fill_rows = [
                row[0]
                for row in (
                    await uow.session.execute(
                        text(
                            "SELECT to_jsonb(f) FROM fill f WHERE workspace_id=:w "
                            "AND intent_id=:i ORDER BY occurred_at, fill_role"
                        ),
                        {"w": WORKSPACE, "i": ledger_intent_id},
                    )
                ).all()
            ]
    finally:
        await engine.dispose()
    final = gateway.position(paper_intent_id)
    restored_position = restored.position(paper_intent_id)
    return {
        "network_attempts": attempted,
        "plan_targets": [str(target.price) for target in plan.targets],
        "plan_quantity": str(plan.quantity),
        "fill_roles": [fill.fill_role for fill in fills],
        "fill_quantities": [str(fill.quantity.normalize()) for fill in fills],
        # Ledger ghi lý do thoát vào payload của position event `exit-fill:<fill_key>`.
        "exit_reasons": [
            (event.payload or {}).get("exit_reason")
            for event in events
            if event.event_key.startswith("exit-fill:")
        ],
        "exit_fill_count": sum(1 for row in fill_rows if row.get("fill_role") == "EXIT"),
        "events": [[event.event_type, (event.payload or {}).get("reason")] for event in events],
        "partial_first": results[0].monitor_result.partial_exit is not None,
        "second_exit_reason": results[1].monitor_result.exit_reason,
        "trade_status": trade.status if trade else None,
        "trade_net_pnl": str(trade.net_pnl) if trade else None,
        "gateway_net_pnl": str(final.net_realized_pnl) if final else None,
        "targets_hit": final.targets_hit if final else None,
        "restored_matches": restored_position == final,
    }


async def replay() -> dict[str, Any]:
    from dataclasses import replace

    from sqlalchemy import text

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.repositories.backtest_outcomes import (
        BacktestOutcomeRepository,
    )
    from trading_bot.application.backtest_dataset_replay_execution import (
        BacktestDatasetReplayExecutionService,
    )
    from trading_bot.application.outbox import UnitOfWork

    attempted = _guard_network()
    # Replay giữ ngày gốc của fixture (dữ liệu quá khứ như backtest thật).
    bundle, comparison = FIXTURE.build_h18_dataset_runtime_replay(WORKSPACE)
    engines = {
        role: create_database_engine(
            database_url_from_secret(
                username=role,
                password_file=Path("/run/secrets", f"database_{role[10:]}_password"),
                host="postgres",
                database="crypidian",
            )
        )
        for role in ("crypidian_backtest_worker", "crypidian_replay_writer")
    }
    read_sessions = create_session_factory(engines["crypidian_backtest_worker"])
    writer_sessions = create_session_factory(engines["crypidian_replay_writer"])

    class Loader:
        def __init__(self, uow: Any) -> None:
            self.uow = uow

        async def load_for_replay(self, **_kwargs: Any) -> Any:
            return bundle

        async def revalidate_current_rights(self, source: Any, **_kwargs: Any) -> Any:
            checked_at = await self.uow.session.scalar(text("SELECT clock_timestamp()"))
            return replace(
                source,
                components=tuple(
                    replace(
                        component,
                        current_rights=replace(
                            component.current_rights,
                            checked_at=checked_at,
                            valid_until=checked_at + timedelta(days=1),
                        ),
                    )
                    for component in source.components
                ),
            )

    service = BacktestDatasetReplayExecutionService(
        load_unit_of_work_factory=lambda ws: UnitOfWork(read_sessions, workspace_id=ws),
        persist_unit_of_work_factory=lambda ws: UnitOfWork(writer_sessions, workspace_id=ws),
        bundle_loader_factory=Loader,
        outcome_repository_factory=lambda uow: BacktestOutcomeRepository(uow.session),
        executor=lambda *args, **kwargs: comparison,
        clock=lambda: datetime.now(UTC),
    )
    try:
        result = await service.execute(
            workspace_id=WORKSPACE,
            bundle_hash=bundle.dataset_hash,
            manifest=comparison.runs[0].sensitivity_run.manifest,
        )
    finally:
        for engine in engines.values():
            await engine.dispose()
    report = comparison.runs[0].sensitivity_run.result
    password = Path("/run/secrets/postgres_password").read_text().strip()
    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=password
    ) as admin:
        stored = admin.execute(
            "SELECT r.report_hash, r.trade_count, r.report ->> 'net_pnl' FROM backtest_report r "
            "WHERE r.workspace_id=%s",
            (WORKSPACE,),
        ).fetchall()
        trades = admin.execute(
            "SELECT count(*) FROM backtest_trade_outcome WHERE workspace_id=%s", (WORKSPACE,)
        ).fetchone()[0]
    return {
        "network_attempts": attempted,
        "run_count": len(result.run_ids),
        "report_hash_matches": [row[0] for row in stored] == [report.report_hash],
        "stored_trade_count": [row[1] for row in stored],
        "stored_net_pnl_matches": [Decimal(row[2]) for row in stored] == [report.net_pnl],
        "trade_outcomes": trades,
        "net_pnl_positive": report.net_pnl > 0,
    }


phase = os.environ["SIG4_PHASE"]
if phase == "seed":
    print(json.dumps(seed()))
elif phase == "lifecycle":
    print(json.dumps(asyncio.run(lifecycle())))
elif phase == "replay":
    print(json.dumps(asyncio.run(replay())))
else:
    raise SystemExit(f"Pha không hợp lệ: {phase}")
