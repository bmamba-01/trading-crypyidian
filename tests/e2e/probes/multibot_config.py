"""Probe MB-02 chạy trong container Compose; phase và state nhận qua biến môi trường.

Mỗi phase in đúng một dòng JSON cuối cùng để test host đọc kết quả.
"""

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_bot.adapters.database.engine import create_database_engine, database_url_from_secret

PHASE = os.environ["MB02_PHASE"]
STATE = json.loads(os.environ.get("MB02_STATE") or "{}")
WORKSPACE_ID = UUID(STATE["workspace_id"])
SYMBOL = "BTCUSDT"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def database():
    url = database_url_from_secret(
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
        host="postgres",
        database="crypidian",
    )
    engine = create_database_engine(url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def emit(payload):
    print(json.dumps(payload, default=str, sort_keys=True))


async def instrument_phase():
    """admin-web: seed instrument point-in-time cho BTCUSDT/ETHUSDT."""
    from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
    from trading_bot.domain.market.ingestion import InstrumentObservation
    from trading_bot.domain.market.models import Instrument

    engine, sessions = database()
    observed_at = datetime.now(UTC) - timedelta(minutes=1)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(WORKSPACE_ID)},
        )
        repository = MarketDataRepository(session)
        for symbol, base in (("BTCUSDT", "BTC"), ("ETHUSDT", "ETH")):
            await repository.append_instrument(
                workspace_id=WORKSPACE_ID,
                observation=InstrumentObservation(
                    instrument=Instrument(
                        symbol=symbol,
                        base_asset=base,
                        quote_asset="USDT",
                        tick_size=Decimal("0.1"),
                        quantity_step=Decimal("0.001"),
                        min_notional=Decimal("5"),
                    ),
                    source="BINANCE_USDM",
                    observed_at=observed_at,
                ),
                idempotency_key=f"mb02:exchange-info:{symbol}",
            )
    await engine.dispose()
    emit({"instruments": 2})


async def seed_phase():
    """trading-runtime: tạo PAPER account (trigger tạo bot), seed vốn và checkpoint gốc."""
    from trading_bot.adapters.database.models import BotInstance
    from trading_bot.adapters.database.repositories.paper_checkpoints import (
        PaperCheckpointRepository,
    )
    from trading_bot.adapters.database.repositories.workspace_settings import (
        WorkspaceSettingsRepository,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.paper_account_initialization import (
        PaperAccountInitializationService,
    )
    from trading_bot.application.paper_execution_profile import (
        default_paper_execution_profile_catalog,
        resolve_paper_execution_profile,
    )
    from trading_bot.domain.execution.paper import PaperExecutionGateway

    engine, sessions = database()
    now = datetime.now(UTC)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        published = await WorkspaceSettingsRepository(uow.session).load_effective_config(
            workspace_id=WORKSPACE_ID
        )
        assert published is not None
        profile = resolve_paper_execution_profile(
            published, default_paper_execution_profile_catalog()
        )
        result = await PaperAccountInitializationService(
            account_name="mb02-paper-account"
        ).ensure_initialized(
            uow,
            published_settings=published,
            execution_profile=profile,
            max_notional=Decimal("10000"),
            evaluated_at=now,
        )
        account_id = result.account.id
        genesis_key = f"mb02-genesis:{account_id}"
        await PaperCheckpointRepository().append(
            uow,
            account_id=account_id,
            event_key=genesis_key,
            event_hash=sha(genesis_key),
            snapshot=PaperExecutionGateway(profile.fill_policy).export_state(),
            occurred_at=now,
        )
        bot_id = await uow.session.scalar(
            select(BotInstance.id).where(
                BotInstance.workspace_id == WORKSPACE_ID,
                BotInstance.trading_account_id == account_id,
            )
        )
    await engine.dispose()
    emit({"account_id": str(account_id), "bot_id": str(bot_id)})


async def admin_rls_phase():
    """admin-web: RLS bot_config_version theo membership/role/grant thật."""
    from trading_bot.adapters.database.models import BotConfigRequest, BotConfigVersion
    from trading_bot.application.outbox import UnitOfWork

    engine, sessions = database()
    bot_id = UUID(STATE["bot_id"])

    async def count(model, membership, workspace=WORKSPACE_ID):
        async with UnitOfWork(
            sessions, workspace_id=workspace, actor_membership_id=membership
        ) as uow:
            return await uow.session.scalar(
                select(func.count()).select_from(model).where(model.bot_id == bot_id)
            )

    owner = UUID(STATE["owner_membership_id"])
    operator = UUID(STATE["operator_membership_id"])
    result = {
        "owner_versions": await count(BotConfigVersion, owner),
        "owner_requests": await count(BotConfigRequest, owner),
        "operator_versions": await count(BotConfigVersion, operator),
        "foreign_workspace_versions": await count(BotConfigVersion, owner, uuid4()),
        "no_member_versions": await count(BotConfigVersion, None),
    }
    await engine.dispose()
    emit(result)


def fixture_bars(now):
    """Chuỗi 1h kết thúc bằng nến phá kênh Donchian để H03 sinh tín hiệu long."""
    from trading_bot.domain.market.models import OHLCVBar

    base = now - timedelta(hours=220)
    bars = []
    for index in range(220):
        opened = base + timedelta(hours=index)
        closed = opened + timedelta(hours=1)
        current = index == 219
        bars.append(
            OHLCVBar(
                symbol=SYMBOL,
                timeframe="1h",
                open_time=opened,
                close_time=closed,
                event_at=closed,
                received_at=closed + timedelta(milliseconds=1),
                available_at=closed + timedelta(milliseconds=2),
                open=Decimal("101") if current else Decimal("96"),
                high=Decimal("102") if current else Decimal("100"),
                low=Decimal("94") if current else Decimal("95"),
                close=Decimal("101.5") if current else Decimal("96"),
                volume=Decimal("6") if current else Decimal("5"),
                is_closed=True,
                source="MB02_H03_FIXTURE",
                source_cursor=f"mb02-bar-{index}",
                checksum=sha(f"mb02-bar-{index}"),
            )
        )
    return bars


class H03Provider:
    """Context provider tin cậy của E2E: dựng snapshot/planner từ dữ liệu đã persist."""

    def __init__(self, window_start, window_end):
        self.window_start = window_start
        self.window_end = window_end
        self.monitor_pins = []

    async def _bars(self, uow, feature):
        from trading_bot.adapters.database.repositories.market_data import MarketDataRepository

        return await MarketDataRepository(uow.session).list_bars(
            workspace_id=WORKSPACE_ID,
            symbol=feature.symbol,
            timeframe=feature.timeframe,
            start=self.window_start,
            end=self.window_end,
            as_of=feature.as_of,
            closed_only=True,
        )

    async def _feature(self, uow, snapshot_id, as_of):
        from trading_bot.adapters.database.models.feature_entities import FeatureSnapshotRecord
        from trading_bot.adapters.database.repositories.feature_snapshots import (
            FeatureSnapshotRepository,
        )

        row = await uow.session.scalar(
            select(FeatureSnapshotRecord).where(
                FeatureSnapshotRecord.workspace_id == WORKSPACE_ID,
                FeatureSnapshotRecord.snapshot_id == snapshot_id,
            )
        )
        assert row is not None
        assert row.is_usable
        feature = await FeatureSnapshotRepository(uow.session).get_as_of(
            workspace_id=WORKSPACE_ID,
            symbol=row.symbol,
            timeframe=row.timeframe,
            as_of=as_of,
            schema_hash=row.schema_hash,
            formula_hash=row.formula_hash,
            code_hash=row.code_hash,
        )
        assert feature is not None
        assert feature.snapshot_id == snapshot_id
        return feature

    @staticmethod
    def _snapshot(feature, bars, *, flat):
        from trading_bot.domain.strategy.contracts import (
            DecisionSnapshot,
            FeatureObservation,
            FeatureQuality,
            MarketSeries,
            SeriesQuality,
        )

        atr = feature.features["atr_14"]
        return DecisionSnapshot(
            symbol=feature.symbol,
            decision_time=feature.as_of + timedelta(seconds=1),
            snapshot_id=feature.snapshot_id,
            series=(
                MarketSeries(
                    timeframe=feature.timeframe,
                    bars=tuple(bars),
                    quote_volumes=tuple(float(bar.close * bar.volume) for bar in bars),
                    quality=SeriesQuality.READY,
                    expected_interval=timedelta(hours=1),
                    max_event_age=timedelta(hours=2),
                    backfill_complete=True,
                ),
            ),
            features=(
                FeatureObservation(
                    feature_id=f"{feature.snapshot_id}:atr-14",
                    name="atr_14",
                    timeframe=feature.timeframe,
                    value=atr,
                    previous_value=None,
                    available_at=feature.as_of,
                    quality=FeatureQuality.READY,
                ),
            ),
            position_is_flat=flat,
        )

    async def load_context(self, *, uow, command, published_settings, execution_profile, account):
        from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
        from trading_bot.adapters.database.repositories.paper_account_state import (
            PaperAccountStateRepository,
        )
        from trading_bot.application.paper_account_state import PaperAccountStateService
        from trading_bot.application.paper_runtime_application import PaperDecisionContext
        from trading_bot.application.runtime_risk_profile import load_risk_profile
        from trading_bot.domain.risk.engine import RiskState
        from trading_bot.domain.risk.planner import AccountSnapshot, ExchangeRules, PlannerSettings
        from trading_bot.domain.strategy.registry import StrategyRegistry

        feature = await self._feature(uow, command.feature_snapshot_id, command.evaluated_at)
        bars = await self._bars(uow, feature)
        snapshot = self._snapshot(feature, bars, flat=True)
        state = await PaperAccountStateService(PaperAccountStateRepository(uow.session)).get_state(
            workspace_id=WORKSPACE_ID, account_id=account.account_id
        )
        instrument = await MarketDataRepository(uow.session).get_instrument(
            workspace_id=WORKSPACE_ID,
            source="BINANCE_USDM",
            symbol=feature.symbol,
            as_of=feature.as_of,
        )
        assert instrument is not None
        root = published_settings.settings
        risk_profile = await load_risk_profile(
            uow.session, workspace_id=WORKSPACE_ID, profile_id=root.risk.profile_version_id
        )
        release = StrategyRegistry().get_release(root.strategy.default_release)
        settings_id = str(published_settings.settings_version_id)
        planner = PlannerSettings(
            workspace_id=str(WORKSPACE_ID),
            settings_version_id=settings_id,
            risk_profile_version_id=str(root.risk.profile_version_id),
            published=True,
            active_release_ids=(root.strategy.default_release,),
            leverage=risk_profile.limits.max_leverage,
            exchange=ExchangeRules(
                instrument.tick_size,
                instrument.quantity_step,
                Decimal("1"),
                instrument.min_notional,
                risk_profile.limits.max_notional,
                risk_profile.limits.max_notional / bars[-1].close,
            ),
            limits=risk_profile.limits,
            release_content_hashes=((root.strategy.default_release, release.content_hash),),
            settings_hash=published_settings.settings_hash,
        )
        account_snapshot = AccountSnapshot(
            workspace_id=str(WORKSPACE_ID),
            account_id=str(account.account_id),
            settings_version_id=settings_id,
            captured_at=state.as_of,
            equity=state.equity,
            available_margin=state.equity,
            is_known=True,
        )
        # Provider E2E không dựng exposure mở; phase sau publish dừng ở universe trước risk.
        risk_state = RiskState(
            workspace_id=str(WORKSPACE_ID),
            account_id=str(account.account_id),
            settings_version_id=settings_id,
            risk_profile_version_id=str(root.risk.profile_version_id),
            evaluated_at=command.evaluated_at,
            account_captured_at=account_snapshot.captured_at,
            market_data_as_of=max(bar.event_at for bar in bars),
            equity=account_snapshot.equity,
            available_margin=account_snapshot.available_margin,
            open_exposures=(),
            realized_loss_today=Decimal("0"),
            realized_loss_rolling=Decimal("0"),
            open_position_count=0,
            is_known=True,
            pending_reservation_initial_margin=Decimal("0"),
        )
        return PaperDecisionContext(
            snapshot=snapshot,
            planner_settings=planner,
            account=account_snapshot,
            risk_state=risk_state,
            account_quote_asset=state.quote_asset,
            open_exposures=(),
            evidence={
                "feature_snapshot_id": feature.snapshot_id,
                "feature_content_hash": feature.content_hash,
            },
        )

    async def load_monitor_evidence(
        self,
        *,
        uow,
        command,
        published_settings,
        execution_profile,
        account,
        position,
        pinned_strategy_release_name,
        pinned_strategy_release_hash,
        pinned_strategy_settings_version_id,
        pinned_strategy_parameters,
    ):
        from trading_bot.application.paper_runtime_application import PaperMonitorEvidence
        from trading_bot.domain.strategy.contracts import StrategyCandidate, StrategySelection
        from trading_bot.domain.strategy.registry import StrategyRegistry

        self.monitor_pins.append(
            {
                "release": pinned_strategy_release_name,
                "settings_version_id": pinned_strategy_settings_version_id,
                "parameters": [list(item) for item in pinned_strategy_parameters],
            }
        )
        feature = await self._feature(uow, command.feature_snapshot_id, command.bar.opened_at)
        bars = await self._bars(uow, feature)
        snapshot = self._snapshot(feature, bars, flat=False)
        selection = StrategySelection(
            settings_version_id=pinned_strategy_settings_version_id,
            active_release_ids=(pinned_strategy_release_name,),
            published=True,
            parameters=pinned_strategy_parameters,
        )
        signal = (
            StrategyRegistry()
            .resolve(pinned_strategy_release_name, selection)
            .evaluate(snapshot, selection)
        )
        return PaperMonitorEvidence(
            workspace_id=WORKSPACE_ID,
            account_id=account.account_id,
            feature_snapshot_id=feature.snapshot_id,
            symbol=feature.symbol,
            available_at=feature.as_of,
            strategy_release_name=pinned_strategy_release_name,
            strategy_release_hash=pinned_strategy_release_hash,
            opposing_signal=(
                isinstance(signal, StrategyCandidate) and signal.direction != position.direction
            ),
            atr=Decimal(str(feature.features["atr_14"])),
            strategy_settings_version_id=pinned_strategy_settings_version_id,
            strategy_parameters=pinned_strategy_parameters,
        )


def runtime_app(sessions, provider):
    from trading_bot.app import create_app
    from trading_bot.config.settings import AppSettings

    return create_app(
        AppSettings(process_role="trading-runtime", runtime_workspace_id=WORKSPACE_ID),
        session_factory=sessions,
        paper_decision_context_provider_factory=lambda **_: provider,
    )


async def open_phase():
    """trading-runtime: entry đầu tiên dùng overlay v1, khớp lệnh và giữ vị thế OPEN."""
    from trading_bot.adapters.database.models.paper_entities import (
        OrderIntentRecord,
        TradeRecord,
    )
    from trading_bot.adapters.database.repositories.feature_snapshots import (
        FeatureSnapshotRepository,
    )
    from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.paper_decision_cycle import DecisionCycleOutcome
    from trading_bot.application.paper_runtime_application import (
        PaperDecisionCommand,
        PaperEntryEventCommand,
    )
    from trading_bot.domain.execution.paper import PaperBar
    from trading_bot.domain.market.feature_snapshot import FeatureCalculator, FeatureSettings
    from trading_bot.domain.market.ingestion import MarketRecordVersion, MarketStream
    from trading_bot.domain.market.models import MarketSnapshot

    engine, sessions = database()
    account_id = UUID(STATE["account_id"])
    now = datetime.now(UTC)
    bars = fixture_bars(now)
    window_start = bars[0].open_time
    window_end = bars[-1].open_time + timedelta(hours=1)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        market = MarketDataRepository(uow.session)
        instrument = await market.get_instrument(
            workspace_id=WORKSPACE_ID, source="BINANCE_USDM", symbol=SYMBOL, as_of=now
        )
        assert instrument is not None
        stream = MarketStream("MB02_H03_FIXTURE", SYMBOL, "1h")
        for bar in bars:
            await market.append_bar(
                workspace_id=WORKSPACE_ID,
                instrument_id=instrument.id,
                bar=bar,
                version=MarketRecordVersion(
                    record_id=uuid4(),
                    stream=stream,
                    logical_key=f"{SYMBOL}:1h:{bar.open_time.isoformat()}",
                    idempotency_key=f"mb02-fixture:{bar.source_cursor}",
                    revision=1,
                    content_checksum=bar.checksum,
                    event_at=bar.event_at,
                    received_at=bar.received_at,
                    available_at=bar.available_at,
                ),
            )
        persisted = await market.list_bars(
            workspace_id=WORKSPACE_ID,
            symbol=SYMBOL,
            timeframe="1h",
            start=window_start,
            end=window_end,
            as_of=bars[-1].available_at,
            closed_only=True,
        )
        feature = FeatureCalculator(
            FeatureSettings(
                code_hash=sha("mb02-h03-feature-fixture-v1"),
                expected_interval=timedelta(hours=1),
                max_event_age=timedelta(hours=2),
            )
        ).calculate(
            MarketSnapshot(as_of=bars[-1].available_at, bars=tuple(persisted)),
            workspace_id=WORKSPACE_ID,
            input_event_ids=tuple(bar.source_cursor for bar in persisted),
        )
        await FeatureSnapshotRepository(uow.session).append(
            workspace_id=WORKSPACE_ID, snapshot=feature
        )
    evaluated_at = bars[-1].available_at + timedelta(seconds=2)
    provider = H03Provider(window_start, window_end)
    app = runtime_app(sessions, provider)
    async with app.router.lifespan_context(app):
        service = app.state.paper_application
        assert service is not None
        assert service.is_ready
        result = await service.evaluate_and_submit(
            PaperDecisionCommand(
                account_id=account_id,
                feature_snapshot_id=feature.snapshot_id,
                evaluated_at=evaluated_at,
            )
        )
        decision = result.decision
        assert decision is not None
        assert decision.outcome is DecisionCycleOutcome.RISK_APPROVED, decision.outcome
        plan = decision.plan
        submission = result.submission
        entry_opened = submission.order.eligible_at
        entry_closed = entry_opened + timedelta(minutes=1)
        price = plan.entry_reference_price
        entry = await service.apply_entry_event(
            PaperEntryEventCommand(
                account_id=account_id,
                ledger_intent_id=submission.ledger_intent_id,
                paper_intent_id=submission.order.intent_id,
                bar=PaperBar(
                    "mb02-entry",
                    plan.symbol,
                    entry_opened,
                    entry_closed,
                    entry_closed,
                    price,
                    price,
                    price,
                    price,
                ),
            )
        )
        assert entry.order.status.value == "FILLED"
        position = entry.position
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        await uow.bind_account(account_id)
        intent = await uow.session.scalar(
            select(OrderIntentRecord).where(
                OrderIntentRecord.workspace_id == WORKSPACE_ID,
                OrderIntentRecord.id == submission.ledger_intent_id,
            )
        )
        trade = await uow.session.scalar(
            select(TradeRecord).where(
                TradeRecord.workspace_id == WORKSPACE_ID, TradeRecord.intent_id == intent.id
            )
        )
    await engine.dispose()
    emit(
        {
            "feature_snapshot_id": feature.snapshot_id,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "evaluated_at": evaluated_at.isoformat(),
            "risk_budget": str(plan.risk_budget),
            "settings_version_id": str(plan.settings_version_id),
            "intent_id": str(intent.id),
            "intent_bot_config_version_id": intent.evidence.get("bot_config_version_id"),
            "intent_effective_config_hash": intent.evidence.get("effective_config_hash"),
            "trade_id": str(trade.id),
            "trade_status": trade.status,
            "position_id": position.position_id,
            "paper_intent_id": submission.order.intent_id,
            "symbol": plan.symbol,
            "stop_price": str(plan.stop_price),
            "entry_execution_price": str(position.entry_execution_price),
            "entry_closed": entry_closed.isoformat(),
        }
    )


async def after_phase():
    """trading-runtime: entry mới dùng v2, vị thế cũ giữ lineage v1 và thoát bằng stop."""
    from trading_bot.adapters.database.models import BotConfigVersion
    from trading_bot.adapters.database.models.paper_entities import (
        OrderIntentRecord,
        TradeRecord,
    )
    from trading_bot.application.bot_config_resolver import load_effective_bot_config
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.paper_decision_cycle import DecisionCycleOutcome
    from trading_bot.application.paper_runtime_application import (
        PaperDecisionCommand,
        PaperMonitorCommand,
    )
    from trading_bot.application.runtime_risk_profile import load_risk_profile
    from trading_bot.config.bot_settings import RISK_FIELDS
    from trading_bot.domain.execution.paper import PaperBar

    engine, sessions = database()
    account_id = UUID(STATE["account_id"])
    opened = STATE["open"]
    decision_time = datetime.fromisoformat(opened["evaluated_at"])
    now = datetime.now(UTC)
    report = {}
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        await uow.bind_account(account_id)
        before = await load_effective_bot_config(
            uow.session, workspace_id=WORKSPACE_ID, account_id=account_id, as_of=decision_time
        )
        current = await load_effective_bot_config(
            uow.session, workspace_id=WORKSPACE_ID, account_id=account_id, as_of=now
        )
        root_profile = await load_risk_profile(
            uow.session, workspace_id=WORKSPACE_ID, profile_id=current.risk_profile_version_id
        )
        report["pinned_version_id"] = str(before.bot_config_version_id)
        report["pinned_effective_hash"] = before.effective_hash
        report["current_version_id"] = str(current.bot_config_version_id)
        report["hashes_verify"] = before.verify_hash() and current.verify_hash()
        report["current_symbols"] = list(current.symbols)
        report["current_per_trade"] = str(current.risk_limits.per_trade_risk_fraction)
        report["root_caps_enforced"] = all(
            getattr(current.risk_limits, name) is None
            or getattr(current.risk_limits, name)
            <= getattr(
                root_profile.limits,
                "account_max_open_risk_fraction" if name == "bot_max_open_risk_fraction" else name,
            )
            for name in RISK_FIELDS
        )
        statuses = list(
            (
                await uow.session.scalars(
                    select(BotConfigVersion.status).where(
                        BotConfigVersion.workspace_id == WORKSPACE_ID
                    )
                )
            ).all()
        )
        report["runtime_visible_statuses"] = sorted(set(statuses))
        report["runtime_visible_count"] = len(statuses)
        try:
            async with uow.session.begin_nested():
                await uow.session.execute(
                    text("UPDATE bot_config_version SET revision = revision WHERE true")
                )
            report["runtime_update"] = "allowed"
        except DBAPIError as error:
            report["runtime_update"] = getattr(error.orig, "sqlstate", "error")
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as unbound:
        report["unbound_runtime_count"] = await unbound.session.scalar(
            select(func.count()).select_from(BotConfigVersion)
        )
    provider = H03Provider(
        datetime.fromisoformat(opened["window_start"]),
        datetime.fromisoformat(opened["window_end"]),
    )
    app = runtime_app(sessions, provider)
    async with app.router.lifespan_context(app):
        service = app.state.paper_application
        assert service is not None
        assert service.is_ready
        fresh = await service.evaluate_and_submit(
            PaperDecisionCommand(
                account_id=account_id,
                feature_snapshot_id=opened["feature_snapshot_id"],
                evaluated_at=now,
            )
        )
        report["new_entry_outcome"] = fresh.decision.outcome.value
        report["new_entry_code"] = getattr(fresh.decision.strategy_result, "code", None)
        assert fresh.decision.outcome is DecisionCycleOutcome.NO_TRADE
        stop = Decimal(opened["stop_price"])
        entry_price = Decimal(opened["entry_execution_price"])
        exit_opened = datetime.fromisoformat(opened["entry_closed"]) + timedelta(minutes=1)
        exit_closed = exit_opened + timedelta(minutes=1)
        exit_result = await service.monitor_position(
            PaperMonitorCommand(
                account_id=account_id,
                ledger_intent_id=UUID(opened["intent_id"]),
                trade_id=UUID(opened["trade_id"]),
                paper_position_id=opened["position_id"],
                bar=PaperBar(
                    "mb02-stop",
                    opened["symbol"],
                    exit_opened,
                    exit_closed,
                    exit_closed,
                    entry_price,
                    max(entry_price, stop),
                    min(entry_price, stop),
                    stop,
                ),
                feature_snapshot_id=opened["feature_snapshot_id"],
            )
        )
        report["exit_reason"] = exit_result.monitor_result.exit_reason
        report["trade_closed"] = exit_result.trade_closed
        report["monitor_pins"] = provider.monitor_pins
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        await uow.bind_account(account_id)
        intent = await uow.session.scalar(
            select(OrderIntentRecord).where(
                OrderIntentRecord.workspace_id == WORKSPACE_ID,
                OrderIntentRecord.id == UUID(opened["intent_id"]),
            )
        )
        trade = await uow.session.scalar(
            select(TradeRecord).where(
                TradeRecord.workspace_id == WORKSPACE_ID,
                TradeRecord.id == UUID(opened["trade_id"]),
            )
        )
        report["intent_bot_config_version_id"] = intent.evidence.get("bot_config_version_id")
        report["trade_status"] = trade.status
        report["trade_settings_version_id"] = str(trade.settings_version_id)
        report["intent_count"] = await uow.session.scalar(
            select(func.count())
            .select_from(OrderIntentRecord)
            .where(
                OrderIntentRecord.workspace_id == WORKSPACE_ID,
                OrderIntentRecord.account_id == account_id,
            )
        )
    await engine.dispose()
    emit(report)


PHASES = {
    "instrument": instrument_phase,
    "seed": seed_phase,
    "admin_rls": admin_rls_phase,
    "open": open_phase,
    "after": after_phase,
}

if __name__ == "__main__":
    asyncio.run(PHASES[PHASE]())
