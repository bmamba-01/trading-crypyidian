"""Probe MB-03 chạy trong container Compose; mã được gửi qua stdin.

Phase `seed`/`verify`/`prepare_downgrade`/`verify_downgraded` chạy bằng migrator
(superuser, dùng `SET LOCAL ROLE` để kiểm RLS từng role). Phase `dispatch` chạy bằng
login thật của `crypidian_trading_runtime` và gọi đúng code dispatcher/store của app.

Tích hợp MB-02: context của bot đến từ `EffectiveBotDecisionContextProvider` (resolver MB-02
trên root settings + bot config PUBLISHED); bot A3 ENABLED chưa publish config bị loại khỏi
fan-out với lý do `BOT_CONFIG_NOT_PUBLISHED`.
"""

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

START = datetime(2026, 1, 1, tzinfo=UTC)
# Thời điểm quyết định chung của fixture H01/H03 (nến 1h cuối đóng lúc START + 2h - 1µs).
DECISION_TIME = START + timedelta(hours=2) - timedelta(microseconds=1)
EXECUTION_TABLES = (
    "trade_plan",
    "risk_decision",
    "risk_reservation",
    "order_intent",
    "exchange_order",
    "fill",
    "position_event",
    "trade",
)
NEW_TABLES = ("shared_signal_evidence", "bot_decision_job", "bot_decision")
ROOT_EFFECTIVE_AT = DECISION_TIME - timedelta(days=2)
BOT_CONFIG_EFFECTIVE_AT = DECISION_TIME - timedelta(days=1)
HARD_CAPS = {
    "schema_version": 1,
    "hard_caps": {
        "max_aggregate_open_risk": "250",
        "max_notional": "10000",
        "max_leverage": "5",
        "max_positions": 2,
        "max_symbol_notional": "2000",
        "max_concentration_fraction": "0.2",
        "max_daily_loss": "200",
        "max_rolling_loss": "500",
        "max_account_age_seconds": 30,
        "max_data_age_seconds": 300,
    },
}
# Bot A1/A2 có config PUBLISHED với release khác nhau; A3 ENABLED nhưng chưa publish.
BOT_CONFIG_RELEASES = {"a1": "h01-v1", "a2": "h03-v1"}


def identity(name):
    return uuid5(NAMESPACE_URL, "crypidian-mb03-e2e:" + name)


def insert(connection, table, **values):
    values.setdefault("id", identity(table))
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, values)),
        sql.SQL(",").join(sql.Placeholder() for _ in values),
    )
    connection.execute(query, tuple(values.values()))


def scope(connection, role, workspace, bot="", member=""):
    connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    connection.execute(
        "SELECT set_config('app.workspace_id', %s, true), "
        "set_config('app.bot_id', %s, true), set_config('app.membership_id', %s, true)",
        (str(workspace), str(bot), str(member)),
    )


def rejected(connection, action, state=None, marker=None):
    try:
        with connection.transaction():
            action()
    except psycopg.Error as error:
        if state is not None and error.sqlstate != state:
            raise AssertionError((state, error.sqlstate, str(error))) from error
        if marker is not None and marker not in str(error):
            raise AssertionError((marker, str(error))) from error
        return
    raise AssertionError("Thao tác phải bị PostgreSQL từ chối")


def count(connection, query, *params):
    return connection.execute(query, params).fetchone()[0]


def feature_snapshot(workspace):
    from trading_bot.domain.market.feature_snapshot import (
        DataFreshness,
        FeatureSnapshot,
        MarketRegime,
        RegimeAssessment,
    )

    provisional = FeatureSnapshot(
        snapshot_id="0" * 64,
        content_hash="0" * 64,
        workspace_id=workspace,
        symbol="BTCUSDT",
        timeframe="1h",
        as_of=DECISION_TIME - timedelta(minutes=2),
        computed_at=DECISION_TIME - timedelta(seconds=90),
        schema_hash="a" * 64,
        formula_hash="b" * 64,
        code_hash="c" * 64,
        input_bar_ids=("mb03-bar",),
        input_event_ids=("mb03-event",),
        features={"atr_14": 2.0},
        warmup={"atr_14": True},
        quality_flags=(),
        data_freshness=DataFreshness.FRESH,
        data_coverage=1.0,
        regime=RegimeAssessment(MarketRegime.TREND, 0.8, 1.0, "MB03_FIXTURE", "Dữ liệu E2E."),
        is_usable=True,
    )
    digest = hashlib.sha256(provisional.canonical_json().encode()).hexdigest()
    return replace(provisional, snapshot_id=digest, content_hash=digest)


def seed(connection):
    workspace_a, workspace_b = identity("workspace-a"), identity("workspace-b")
    for workspace, slug in ((workspace_a, "mb03-a"), (workspace_b, "mb03-b")):
        insert(
            connection,
            "workspace",
            id=workspace,
            name=slug,
            slug=slug,
            mode="PAPER",
            status="ACTIVE",
        )
    for role in ("owner", "operator"):
        user, member = identity(role + "-user"), identity(role + "-member")
        insert(
            connection,
            "user_account",
            id=user,
            email_normalized=f"mb03-{role}@example.test",
            password_hash="unused",
            status="ACTIVE",
        )
        insert(
            connection,
            "user_membership",
            id=member,
            workspace_id=workspace_a,
            user_id=user,
            status="ACTIVE",
        )
        insert(
            connection,
            "role_binding",
            id=identity(role + "-binding"),
            workspace_id=workspace_a,
            membership_id=member,
            role=role,
        )
    accounts = {
        "a1": (workspace_a, "MB03 bot A1"),
        "a2": (workspace_a, "MB03 bot A2"),
        "a3": (workspace_a, "MB03 bot A3"),
        "b1": (workspace_b, "MB03 bot B1"),
    }
    for key, (workspace, name) in accounts.items():
        # Trigger MB-01 tạo bot PAPER cùng transaction khi insert trading_account.
        insert(
            connection,
            "trading_account",
            id=identity("account-" + key),
            workspace_id=workspace,
            name=name,
            venue="binance_usdm",
            mode="PAPER",
            environment="local",
            status="ENABLED",
            max_notional=10000,
        )
    bots = {
        key: str(
            connection.execute(
                "SELECT id FROM bot_instance WHERE trading_account_id=%s",
                (identity("account-" + key),),
            ).fetchone()[0]
        )
        for key in accounts
    }
    insert(
        connection,
        "bot_grant",
        id=identity("operator-grant"),
        workspace_id=workspace_a,
        bot_id=UUID(bots["a1"]),
        membership_id=identity("operator-member"),
        permission="bot:read",
        granted_by=identity("owner-member"),
    )
    root = seed_root_and_bot_configs(connection, workspace_a, bots)
    snapshot = feature_snapshot(workspace_a)
    payload = snapshot.canonical_payload()
    insert(
        connection,
        "feature_snapshot",
        id=identity("feature-snapshot"),
        workspace_id=workspace_a,
        snapshot_id=snapshot.snapshot_id,
        content_hash=snapshot.content_hash,
        symbol=snapshot.symbol,
        timeframe=snapshot.timeframe,
        as_of=snapshot.as_of,
        computed_at=snapshot.computed_at,
        schema_hash=snapshot.schema_hash,
        formula_hash=snapshot.formula_hash,
        code_hash=snapshot.code_hash,
        input_bar_ids=Jsonb(list(snapshot.input_bar_ids)),
        input_event_ids=Jsonb(list(snapshot.input_event_ids)),
        features=Jsonb(dict(snapshot.features)),
        warmup=Jsonb(dict(snapshot.warmup)),
        quality_flags=Jsonb(list(snapshot.quality_flags)),
        data_freshness=snapshot.data_freshness.value,
        data_coverage=snapshot.data_coverage,
        regime=Jsonb(payload["regime"]),
        is_usable=True,
        canonical_json=snapshot.canonical_json(),
    )
    return {
        "workspace_a": str(workspace_a),
        "workspace_b": str(workspace_b),
        "account_a1": str(identity("account-a1")),
        "account_a2": str(identity("account-a2")),
        "account_a3": str(identity("account-a3")),
        "account_b1": str(identity("account-b1")),
        "bot_a1": bots["a1"],
        "bot_a2": bots["a2"],
        "bot_a3": bots["a3"],
        "bot_b1": bots["b1"],
        "owner_member": str(identity("owner-member")),
        "operator_member": str(identity("operator-member")),
        "feature_snapshot_id": snapshot.snapshot_id,
        **root,
    }


def seed_root_and_bot_configs(connection, workspace, bots):
    """Root settings + risk profile + release PUBLISHED và config bot A1/A2 qua DRAFT→PUBLISHED.

    Hash tính bằng đúng hàm của ứng dụng để resolver MB-02 chấp nhận; bot A3 không có config.
    """
    from trading_bot.adapters.database.repositories.workspace_settings import (
        canonical_settings,
        settings_hash,
    )
    from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
    from trading_bot.config.workspace_settings import WorkspaceSettings
    from trading_bot.domain.strategy.registry import StrategyRegistry

    owner = identity("owner-member")
    profile = identity("risk-profile")
    insert(
        connection,
        "risk_profile_version",
        id=profile,
        workspace_id=workspace,
        name="MB03 risk",
        version=1,
        max_risk_fraction=Decimal("0.01"),
        settings=Jsonb(HARD_CAPS),
        is_active=True,
    )
    settings = WorkspaceSettings.model_validate(
        {
            "schema_version": 1,
            "runtime": {"mode": "PAPER", "timezone": "UTC"},
            "market_data": {
                "venue": "BINANCE_USDM",
                "symbols": ["BTCUSDT", "ETHUSDT"],
                "timeframes": ["1h", "4h"],
                "max_age_seconds": 30,
            },
            "features": {"formula_version": "crypidian-features-v1", "sessions_timezone": "UTC"},
            "strategy": {
                "default_release": "h03-v1",
                "parameters": {},
                "entry_timeframe": "1h",
                "confirmation_timeframe": "4h",
            },
            "risk": {"profile_version_id": str(profile)},
            "paper_execution": {
                "quote_asset": "USDT",
                "starting_equity": "10000",
                "fee_schedule_version": "paper-fees-v1",
                "fill_model": "next_bar_with_adverse_costs",
            },
        }
    )
    payload = canonical_settings(settings)
    root_hash = settings_hash(payload)
    insert(
        connection,
        "workspace_settings_version",
        id=identity("root-settings-v1"),
        workspace_id=workspace,
        version=1,
        schema_version=settings.schema_version,
        settings_json=Jsonb(payload),
        settings_hash=root_hash,
        status="PUBLISHED",
        revision=1,
        created_by=owner,
        approved_by=owner,
        published_at=ROOT_EFFECTIVE_AT,
        effective_at=ROOT_EFFECTIVE_AT,
    )
    registry = StrategyRegistry()
    for release in sorted(set(BOT_CONFIG_RELEASES.values())):
        insert(
            connection,
            "strategy_release",
            id=identity("release-" + release),
            workspace_id=workspace,
            name=release,
            version=1,
            status="PUBLISHED",
            recipe=Jsonb({}),
            content_hash=registry.get_release(release).content_hash,
        )
    configs = {}
    for key, release in BOT_CONFIG_RELEASES.items():
        overlay = canonical_overlay(
            BotConfigOverlay.model_validate(
                {
                    "schema_version": 1,
                    "strategy": {"release_id": release},
                    "universe": {"symbols": ["BTCUSDT"]},
                }
            )
        )
        version_id = identity(f"config-{key}-v1")
        insert(
            connection,
            "bot_config_version",
            id=version_id,
            workspace_id=workspace,
            bot_id=UUID(bots[key]),
            account_id=identity("account-" + key),
            version=1,
            schema_version=1,
            overlay_json=Jsonb(overlay),
            overlay_hash=overlay_hash(overlay),
            base_settings_version_id=identity("root-settings-v1"),
            base_settings_hash=root_hash,
            status="DRAFT",
            revision=1,
            created_by=owner,
            updated_by=owner,
        )
        # Trigger MB-02 chỉ cho INSERT DRAFT; publish là chuyển trạng thái riêng.
        connection.execute(
            "UPDATE bot_config_version SET status='PUBLISHED', approved_by=%s, "
            "published_at=%s, effective_at=%s WHERE id=%s",
            (owner, BOT_CONFIG_EFFECTIVE_AT, BOT_CONFIG_EFFECTIVE_AT, version_id),
        )
        configs[f"config_{key}"] = str(version_id)
    return {"root_settings_hash": root_hash, **configs}


def fixture_snapshots():
    """Snapshot H01/H03 xác định, cùng decision time; dựng tại chỗ vì image không có tests/."""
    from trading_bot.domain.market.models import OHLCVBar
    from trading_bot.domain.strategy.contracts import (
        DecisionSnapshot,
        FeatureObservation,
        MarketSeries,
        SeriesQuality,
    )

    intervals = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}

    def bar(index, timeframe, close, *, high=None, low=None):
        opened = START + index * intervals[timeframe]
        closed = opened + intervals[timeframe] - timedelta(microseconds=1)
        price = Decimal(str(close))
        return OHLCVBar(
            symbol="BTCUSDT",
            timeframe=timeframe,
            open_time=opened,
            close_time=closed,
            event_at=closed,
            received_at=closed,
            available_at=closed,
            open=price,
            high=Decimal(str(high if high is not None else close + 1)),
            low=Decimal(str(low if low is not None else close - 1)),
            close=price,
            volume=Decimal("10"),
            is_closed=True,
        )

    def feature(name, timeframe, value, available_at, previous=None):
        return FeatureObservation(
            feature_id=f"{timeframe}:{name}:{value}:{previous}",
            name=name,
            timeframe=timeframe,
            value=value,
            previous_value=previous,
            available_at=available_at,
        )

    def snapshot(snapshot_id, series, features):
        qualified = tuple(
            replace(
                item,
                quality=SeriesQuality.READY,
                expected_interval=intervals[item.timeframe],
                max_event_age=2 * intervals[item.timeframe],
                backfill_complete=True,
            )
            for item in series
        )
        return DecisionSnapshot(
            symbol="BTCUSDT",
            decision_time=DECISION_TIME,
            snapshot_id=snapshot_id,
            series=qualified,
            features=features,
            position_is_flat=True,
            macro_blackout=False,
            macro_evidence_ids=("macro-calendar-mb03",),
            macro_evidence_available_at=DECISION_TIME,
            macro_is_fresh=True,
        )

    h01_bars = (bar(0, "1h", 100.0), bar(1, "1h", 101.0))
    h01 = snapshot(
        "mb03-h01-snapshot",
        (MarketSeries("1h", h01_bars), MarketSeries("4h", (bar(-1, "4h", 110.0),))),
        (
            feature("ema_20", "1h", 101.0, DECISION_TIME, previous=99.0),
            feature("ema_80", "1h", 100.5, DECISION_TIME, previous=100.0),
            feature("ema_200", "4h", 100.0, DECISION_TIME),
            feature("adx_14", "1h", 20.0, DECISION_TIME),
            feature("atr_14", "1h", 2.0, DECISION_TIME),
        ),
    )
    h03_bars = tuple(bar(index - 54, "1h", 96.0, high=100.0, low=95.0) for index in range(55)) + (
        bar(1, "1h", 101.5, high=102.0, low=94.0),
    )
    h03 = snapshot(
        "mb03-h03-snapshot",
        (MarketSeries("1h", h03_bars, quote_volumes=(5.0,) * 55 + (6.0,)),),
        (feature("atr_14", "1h", 2.0, DECISION_TIME),),
    )
    return {"h01-v1": h01, "h03-v1": h03}


async def dispatch(state):
    from sqlalchemy import select, text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models import BotDecisionJobRecord, BotDecisionRecord
    from trading_bot.adapters.database.repositories.bot_decisions import (
        PostgresSignalDecisionStore,
    )
    from trading_bot.adapters.database.repositories.feature_snapshots import (
        FeatureSnapshotRepository,
    )
    from trading_bot.adapters.database.repositories.signal_evidence import (
        SharedSignalEvidenceRepository,
    )
    from trading_bot.application.bot_decision_contexts import (
        EffectiveBotDecisionContextProvider,
        FanOutExclusionRecorder,
        RuntimeFanOutGate,
    )
    from trading_bot.application.bot_dispatcher import (
        BotEvidenceDispatcher,
        EvidenceScopeError,
        SignalEvidenceFanOutConsumer,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.market.signal_evidence import (
        EvidenceSourceRef,
        SharedSignalEvidence,
        SignalEvidenceError,
        build_shared_signal_evidence,
    )
    from trading_bot.domain.strategy.registry import StrategyRegistry

    workspace_a, workspace_b = UUID(state["workspace_a"]), UUID(state["workspace_b"])
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    snapshots = fixture_snapshots()

    class Snapshots:
        async def snapshot_for(self, *, evidence, context, evaluated_at):
            return snapshots[context.strategy_release_id]

    exclusions = []
    # I3: gate + recorder bắt buộc như wiring production (probe không giữ lease nào).
    provider = EffectiveBotDecisionContextProvider(
        sessions,
        on_exclusion=exclusions.append,
        runtime_gate=RuntimeFanOutGate(),
        exclusion_recorder=FanOutExclusionRecorder(
            sessions=sessions, workspace_id=workspace_a, accounts=lambda _account: None
        ),
    )
    store = PostgresSignalDecisionStore(sessions)
    dispatcher = BotEvidenceDispatcher(store, strategies=StrategyRegistry(), snapshots=Snapshots())
    consumer = SignalEvidenceFanOutConsumer(store, dispatcher, provider, worker_id="mb03-e2e")
    try:
        # Context thật từ resolver MB-02 dưới login runtime: chỉ bot có config PUBLISHED.
        as_of = DECISION_TIME + timedelta(minutes=5)
        eligibility = await provider.eligibility(workspace_id=workspace_a, as_of=as_of)
        assert [(str(item.bot_id), item.reason_code) for item in eligibility.exclusions] == [
            (state["bot_a3"], "BOT_CONFIG_NOT_PUBLISHED")
        ], eligibility.exclusions
        # Thứ tự theo release để khớp oracle (bot_id do trigger sinh ngẫu nhiên).
        contexts = tuple(sorted(eligibility.contexts, key=lambda item: item.strategy_release_id))
        assert [str(item.bot_id) for item in contexts] == [state["bot_a1"], state["bot_a2"]]
        assert [str(item.bot_config_version_id) for item in contexts] == [
            state["config_a1"],
            state["config_a2"],
        ]
        assert [item.strategy_release_id for item in contexts] == ["h01-v1", "h03-v1"]
        for item in contexts:
            assert item.root_settings_version_id == identity("root-settings-v1")
            assert item.root_settings_hash == state["root_settings_hash"]
            assert item.symbols == ("BTCUSDT",)
            assert item.effective_at == BOT_CONFIG_EFFECTIVE_AT
        # Tất định theo as_of: resolve lại cho cùng context (consumer ghim context hash).
        again = await provider.contexts_for(workspace_id=workspace_a, as_of=as_of)
        assert sorted(again, key=lambda item: item.strategy_release_id) == list(contexts)
        async with UnitOfWork(sessions, workspace_id=workspace_a) as uow:
            current_user = await uow.session.scalar(text("SELECT current_user"))
            feature = await FeatureSnapshotRepository(uow.session).get_as_of(
                workspace_id=workspace_a, symbol="BTCUSDT", timeframe="1h", as_of=DECISION_TIME
            )
        assert current_user == "crypidian_trading_runtime", current_user
        assert feature is not None
        assert feature.snapshot_id == state["feature_snapshot_id"]
        evidence = build_shared_signal_evidence(
            feature_snapshot=feature,
            setup_type="trend_breakout",
            observed_at=DECISION_TIME - timedelta(minutes=1),
            available_at=DECISION_TIME,
            validity_end=DECISION_TIME + timedelta(hours=1),
            sources=(EvidenceSourceRef("market_bar:binance:BTCUSDT:1h:0", DECISION_TIME),),
        )
        first = await dispatcher.dispatch_evidence(evidence, contexts, as_of)
        retry = await dispatcher.dispatch_evidence(evidence, contexts, as_of)
        assert retry == first
        for decision in first:
            candidate = json.loads(decision.candidate_json)
            assert candidate["settings_version_id"] == str(identity("root-settings-v1"))
        # Detector chạy lại: cùng observation nhưng available_at/validity_end wall-clock muộn hơn.
        rerun = build_shared_signal_evidence(
            feature_snapshot=feature,
            setup_type="trend_breakout",
            observed_at=DECISION_TIME - timedelta(minutes=1),
            available_at=DECISION_TIME + timedelta(minutes=2),
            validity_end=DECISION_TIME + timedelta(hours=1, minutes=2),
            sources=(EvidenceSourceRef("market_bar:binance:BTCUSDT:1h:0", DECISION_TIME),),
        )
        assert rerun.evidence_id == evidence.evidence_id
        assert rerun.evidence_hash != evidence.evidence_hash
        assert await dispatcher.dispatch_evidence(rerun, contexts, as_of) == first
        recorded = await dispatcher.record_evidence(rerun, evaluated_at=as_of)
        assert recorded.created is False
        assert recorded.evidence == evidence
        # MB05.8 ruling 17: retry chỉ khác mốc thời gian -> trả job/quyết định đã ghim (ack),
        # không conflict (trước đây làm event outbox DEAD).
        late = await dispatcher.dispatch_evidence(rerun, contexts, as_of + timedelta(minutes=5))
        assert late == first, late
        assert {item.evaluated_at for item in late} == {as_of}
        # Event outbox do dispatch ghi: consumer fan-out lại phải dedupe rồi acknowledge.
        consumed = await consumer.consume_once(workspace_id=workspace_a, now=datetime.now(UTC))
        assert {job.bot_id for job in consumed} == {context.bot_id for context in contexts}
        assert await consumer.consume_once(workspace_id=workspace_a, now=datetime.now(UTC)) == ()
        # Revision muộn: bị từ chối cho đánh giá lịch sử, đi qua outbox ở mốc của chính nó.
        revision_time = DECISION_TIME + timedelta(minutes=30)
        revision = SharedSignalEvidence.create(
            workspace_id=workspace_a,
            symbol=evidence.symbol,
            setup_type=evidence.setup_type,
            observed_at=evidence.observed_at,
            available_at=revision_time,
            validity_end=evidence.validity_end,
            feature_snapshot_id=evidence.feature_snapshot_id,
            source_refs=(*evidence.source_refs, "market_bar:binance:BTCUSDT:1h:0:correction"),
            model_release_id=None,
        )
        try:
            await dispatcher.dispatch_evidence(
                revision, contexts, DECISION_TIME + timedelta(minutes=10)
            )
        except SignalEvidenceError as error:
            if error.code != "EVIDENCE_NOT_YET_AVAILABLE":
                raise AssertionError(error.code) from error
        else:
            raise AssertionError("Revision muộn phải bị từ chối cho đánh giá lịch sử")
        recorded_revision = await dispatcher.record_evidence(revision, evaluated_at=revision_time)
        assert recorded_revision.created is True
        revision_jobs = await consumer.consume_once(workspace_id=workspace_a, now=datetime.now(UTC))
        assert len(revision_jobs) == 2
        revision_decisions = []
        for item in contexts:
            revision_decisions.extend(
                await dispatcher.process_pending(
                    workspace_id=workspace_a, account_id=item.account_id
                )
            )
        assert {item.evidence_id for item in revision_decisions} == {revision.evidence_id}
        assert len(revision_decisions) == 2
        for item in contexts:
            assert (
                await dispatcher.process_pending(
                    workspace_id=workspace_a, account_id=item.account_id
                )
                == ()
            )
        async with UnitOfWork(sessions, workspace_id=workspace_a) as uow:
            evidence_repository = SharedSignalEvidenceRepository(uow.session)
            historical = await evidence_repository.get_as_of(
                workspace_id=workspace_a,
                symbol="BTCUSDT",
                setup_type="trend_breakout",
                observed_at=evidence.observed_at,
                as_of=DECISION_TIME + timedelta(minutes=10),
            )
            latest = await evidence_repository.get_as_of(
                workspace_id=workspace_a,
                symbol="BTCUSDT",
                setup_type="trend_breakout",
                observed_at=evidence.observed_at,
                as_of=revision_time,
            )
        assert historical == evidence
        assert latest == revision
        # Evidence workspace A không được dispatch cho bot workspace B.
        foreign = replace(contexts[0], workspace_id=workspace_b)
        try:
            await dispatcher.dispatch_evidence(evidence, (foreign,), DECISION_TIME)
        except EvidenceScopeError:
            pass
        else:
            raise AssertionError("Context khác workspace phải bị từ chối")
        # RLS qua app: scope bot A1 chỉ thấy quyết định/job của chính nó, không ghi được cho A2.
        async with UnitOfWork(sessions, workspace_id=workspace_a) as uow:
            await uow.bind_account(contexts[0].account_id)
            visible = (await uow.session.scalars(select(BotDecisionRecord))).all()
            assert {row.bot_id for row in visible} == {contexts[0].bot_id}
            assert len(visible) == 2
        cross_bot_error = None
        try:
            async with UnitOfWork(sessions, workspace_id=workspace_a) as uow:
                await uow.bind_account(contexts[0].account_id)
                uow.session.add(
                    BotDecisionJobRecord(
                        id=identity("cross-bot-job"),
                        workspace_id=workspace_a,
                        bot_id=contexts[1].bot_id,
                        account_id=contexts[1].account_id,
                        evidence_id=evidence.evidence_id,
                        evidence_hash=evidence.evidence_hash,
                        bot_config_version_id=identity("cross-bot-config"),
                        evaluated_at=DECISION_TIME,
                        context_hash="0" * 64,
                        context_payload={"forged": True},
                        status="PENDING",
                    )
                )
                await uow.session.flush()
        except DBAPIError as error:
            cross_bot_error = error.orig.sqlstate
        assert cross_bot_error == "42501", cross_bot_error
        # Scope workspace B không đọc được evidence của workspace A (RLS).
        async with UnitOfWork(sessions, workspace_id=workspace_b) as uow:
            hidden = await SharedSignalEvidenceRepository(uow.session).get(
                workspace_id=workspace_a, evidence_id=evidence.evidence_id
            )
        assert hidden is None
    finally:
        await engine.dispose()
    return {
        "evidence_id": str(evidence.evidence_id),
        "revision_id": str(revision.evidence_id),
        "config_a1": str(contexts[0].bot_config_version_id),
        "config_a2": str(contexts[1].bot_config_version_id),
        # Provider loại A3 ở mọi lần resolve (dispatch + mỗi event consumer) cùng lý do.
        "exclusions": sorted({(str(item.bot_id), item.reason_code) for item in exclusions}),
        "decisions": [
            {
                "bot_id": str(item.bot_id),
                "decision_id": str(item.decision_id),
                "status": item.status.value,
                "reason_code": item.reason_code,
                "strategy_release_id": item.strategy_release_id,
                "bot_config_version_id": str(item.bot_config_version_id),
                "candidate_id": item.candidate_id,
                "idempotency_key": item.idempotency_key,
            }
            for item in first
        ],
    }


def verify(connection, state):
    workspace_a, workspace_b = state["workspace_a"], state["workspace_b"]
    bot_a1, bot_a2, bot_b1 = state["bot_a1"], state["bot_a2"], state["bot_b1"]
    evidence_id, revision_id = state["evidence_id"], state["revision_id"]
    # Một hàng evidence cho mỗi sự kiện chung; mỗi bot một job/quyết định cho mỗi evidence.
    for item in (evidence_id, revision_id):
        assert (
            count(connection, "SELECT count(*) FROM shared_signal_evidence WHERE id=%s", item) == 1
        )
        assert (
            count(
                connection,
                "SELECT count(*) FROM bot_decision_job WHERE evidence_id=%s AND status='COMPLETED'",
                item,
            )
            == 2
        )
        rows = connection.execute(
            "SELECT bot_id::text, status, strategy_release_id, bot_config_version_id::text, "
            "candidate_id FROM bot_decision WHERE evidence_id=%s ORDER BY strategy_release_id",
            (item,),
        ).fetchall()
        assert [row[0] for row in rows] == [bot_a1, bot_a2], rows
        assert [row[1] for row in rows] == ["CANDIDATE", "CANDIDATE"], rows
        assert [row[2] for row in rows] == ["h01-v1", "h03-v1"], rows
        assert [row[3] for row in rows] == [state["config_a1"], state["config_a2"]], rows
        assert rows[0][4] != rows[1][4], rows
    # Retry chỉ khác mốc thời gian không tạo hàng mới: một hàng cho mỗi content_hash.
    assert count(connection, "SELECT count(DISTINCT content_hash) FROM shared_signal_evidence") == 2
    # Candidate mang root workspace_settings_version; bot config chỉ ở cột riêng.
    assert [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT payload->'candidate'->>'settings_version_id' FROM bot_decision"
        )
    ] == [str(identity("root-settings-v1"))]
    # DB chặn trực tiếp hàng trùng content_hash dù ID/hash/timestamp khác.
    rejected(
        connection,
        lambda: connection.execute(
            "INSERT INTO shared_signal_evidence (id, workspace_id, symbol, setup_type, "
            "observed_at, available_at, validity_end, feature_snapshot_id, source_refs, "
            "model_release_id, content_hash, evidence_hash, schema_version, canonical_json) "
            "SELECT %s, workspace_id, symbol, setup_type, observed_at, "
            "available_at + interval '3 minutes', validity_end + interval '3 minutes', "
            "feature_snapshot_id, source_refs, model_release_id, content_hash, %s, "
            "schema_version, canonical_json FROM shared_signal_evidence WHERE id=%s",
            (identity("retry-row"), "d" * 64, evidence_id),
        ),
        "23505",
        marker="uq_shared_signal_evidence_content",
    )
    assert count(connection, "SELECT count(*) FROM shared_signal_evidence") == 2
    assert count(connection, "SELECT count(*) FROM bot_decision_job") == 4
    assert count(connection, "SELECT count(*) FROM bot_decision") == 4
    assert (
        count(connection, "SELECT count(DISTINCT (bot_id, idempotency_key)) FROM bot_decision") == 4
    )
    assert connection.execute(
        "SELECT array_agg(status ORDER BY status) FROM outbox_event "
        "WHERE event_type='signal.evidence.recorded.v1'"
    ).fetchone()[0] == ["DELIVERED", "DELIVERED"]
    assert (
        count(
            connection,
            "SELECT count(*) FROM consumer_receipt WHERE consumer_name='bot-evidence-fanout'",
        )
        == 2
    )
    # Evidence/quyết định không tạo plan, reservation, intent hay lệnh.
    for table in EXECUTION_TABLES:
        assert (
            count(connection, sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))) == 0
        )
    # Bất biến DB.
    rejected(
        connection,
        lambda: connection.execute("UPDATE shared_signal_evidence SET symbol='ETHUSDT'"),
        marker="MB03_SIGNAL_DECISION_IMMUTABLE",
    )
    rejected(
        connection,
        lambda: connection.execute("DELETE FROM bot_decision"),
        marker="MB03_SIGNAL_DECISION_IMMUTABLE",
    )
    rejected(
        connection,
        lambda: connection.execute(
            "UPDATE bot_decision_job SET status='PENDING', completed_at=NULL"
        ),
        marker="MB03_BOT_DECISION_JOB_TRANSITION_INVALID",
    )
    rejected(
        connection,
        lambda: connection.execute("UPDATE bot_decision_job SET evaluated_at=now()"),
        marker="MB03_BOT_DECISION_JOB_TRANSITION_INVALID",
    )
    rejected(
        connection,
        lambda: connection.execute("DELETE FROM bot_decision_job"),
        marker="MB03_BOT_DECISION_JOB_TRANSITION_INVALID",
    )
    # FK chặn job workspace B trỏ evidence workspace A và evidence trỏ feature không tồn tại.
    rejected(
        connection,
        lambda: insert(
            connection,
            "bot_decision_job",
            id=identity("foreign-job"),
            workspace_id=workspace_b,
            bot_id=bot_b1,
            account_id=state["account_b1"],
            evidence_id=evidence_id,
            evidence_hash="0" * 64,
            bot_config_version_id=identity("foreign-config"),
            evaluated_at=DECISION_TIME,
            context_hash="0" * 64,
            context_payload=Jsonb({}),
            status="PENDING",
        ),
        "23503",
    )
    # FK ghép config version: job của bot A2 không được ghim config đã publish của bot A1.
    rejected(
        connection,
        lambda: insert(
            connection,
            "bot_decision_job",
            id=identity("cross-config-job"),
            workspace_id=workspace_a,
            bot_id=bot_a2,
            account_id=state["account_a2"],
            evidence_id=evidence_id,
            evidence_hash="0" * 64,
            bot_config_version_id=state["config_a1"],
            evaluated_at=DECISION_TIME,
            context_hash="0" * 64,
            context_payload=Jsonb({}),
            status="PENDING",
        ),
        "23503",
        marker="fk_bot_decision_job_bot_config_version",
    )
    # Bot A3 chưa publish config: không có job/quyết định nào.
    for table in ("bot_decision_job", "bot_decision"):
        assert (
            count(
                connection,
                sql.SQL("SELECT count(*) FROM {} WHERE bot_id=%s").format(sql.Identifier(table)),
                state["bot_a3"],
            )
            == 0
        )
    rejected(
        connection,
        lambda: insert(
            connection,
            "shared_signal_evidence",
            id=identity("orphan-evidence"),
            workspace_id=workspace_a,
            symbol="BTCUSDT",
            setup_type="orphan",
            observed_at=DECISION_TIME,
            available_at=DECISION_TIME,
            validity_end=DECISION_TIME + timedelta(hours=1),
            feature_snapshot_id="e" * 64,
            source_refs=Jsonb(["orphan"]),
            content_hash="e" * 64,
            evidence_hash="e" * 64,
            schema_version="test",
            canonical_json="{}",
        ),
        "23503",
    )
    # Ma trận RLS theo role.
    runtime = "crypidian_trading_runtime"
    admin = "crypidian_admin_web"
    matrix = (
        (runtime, workspace_a, "", "", 2, 0, 0),
        (runtime, workspace_a, bot_a1, "", 2, 2, 2),
        (runtime, workspace_a, bot_a2, "", 2, 2, 2),
        (runtime, workspace_b, bot_b1, "", 0, 0, 0),
        (admin, workspace_a, "", state["owner_member"], 2, 4, 4),
        (admin, workspace_a, "", state["operator_member"], 2, 2, 2),
        (admin, workspace_a, "", "", 2, 0, 0),
    )
    for role, workspace, bot, member, evidence_rows, job_rows, decision_rows in matrix:
        with connection.transaction():
            scope(connection, role, workspace, bot=bot, member=member)
            observed = (
                count(connection, "SELECT count(*) FROM shared_signal_evidence"),
                count(connection, "SELECT count(*) FROM bot_decision_job"),
                count(connection, "SELECT count(*) FROM bot_decision"),
            )
            assert observed == (evidence_rows, job_rows, decision_rows), (
                role,
                bot,
                member,
                observed,
            )
            if bot and decision_rows:
                assert {
                    row[0] for row in connection.execute("SELECT bot_id::text FROM bot_decision")
                } == {bot}
    with connection.transaction():
        scope(connection, runtime, workspace_a, bot=bot_a1)
        # Runtime không sửa được job của bot khác dù có quyền cột status/completed_at.
        assert (
            connection.execute(
                "UPDATE bot_decision_job SET status='COMPLETED' WHERE bot_id=%s", (bot_a2,)
            ).rowcount
            == 0
        )
    # Quyền: grant trực tiếp tối thiểu, không qua crypidian_app.
    privileges = {
        ("crypidian_trading_runtime", "shared_signal_evidence", "INSERT"): True,
        ("crypidian_trading_runtime", "shared_signal_evidence", "UPDATE"): False,
        ("crypidian_trading_runtime", "bot_decision", "INSERT"): True,
        ("crypidian_trading_runtime", "bot_decision", "UPDATE"): False,
        ("crypidian_trading_runtime", "bot_decision_job", "UPDATE"): False,
        ("crypidian_trading_runtime", "bot_decision_job", "DELETE"): False,
        ("crypidian_admin_web", "bot_decision", "SELECT"): True,
        ("crypidian_admin_web", "bot_decision", "INSERT"): False,
        ("crypidian_admin_web", "shared_signal_evidence", "INSERT"): False,
    }
    for table in NEW_TABLES:
        for role in (
            "crypidian_app",
            "crypidian_execution_gateway",
            "crypidian_ingestion_worker",
            "crypidian_trainer_worker",
        ):
            privileges[(role, table, "SELECT")] = False
    for (role, table, privilege), expected in privileges.items():
        actual = count(connection, "SELECT has_table_privilege(%s, %s, %s)", role, table, privilege)
        assert actual is expected, (role, table, privilege, actual)
    for column, expected in (("status", True), ("completed_at", True), ("evaluated_at", False)):
        actual = count(
            connection,
            "SELECT has_column_privilege("
            "'crypidian_trading_runtime', 'bot_decision_job', %s, 'UPDATE')",
            column,
        )
        assert actual is expected, (column, actual)


def pause_controls(connection, state):
    """MB05.8: pause bot A1 (control append-only) và DISABLE account A3 bằng superuser."""
    insert(
        connection,
        "bot_runtime_control",
        id=identity("mb05-pause-a1"),
        workspace_id=identity("workspace-a"),
        bot_id=UUID(state["bot_a1"]),
        account_id=identity("account-a1"),
        revision=1,
        entry_state="PAUSED",
        reason_code="E2E_PAUSE",
        reason_text="Pause để kiểm exclusion fan-out",
        changed_by=identity("owner-member"),
        correlation_id=identity("mb05-pause-correlation"),
    )
    connection.execute(
        "UPDATE trading_account SET status='DISABLED' WHERE id=%s", (identity("account-a3"),)
    )


def verify_queue(connection, state):
    """Đọc DB (superuser) xác nhận DEAD + audit và exclusion lưu bền."""
    jobs = connection.execute(
        "SELECT status, attempts, last_error_code FROM bot_decision_job WHERE id=%s",
        (UUID(state["poison_job_id"]),),
    ).fetchone()
    healthy = connection.execute(
        "SELECT status FROM bot_decision_job WHERE id=%s", (UUID(state["healthy_job_id"]),)
    ).fetchone()
    exclusion = connection.execute(
        "SELECT fanout_exclusion_code FROM bot_runtime_state WHERE bot_id=%s",
        (UUID(state["bot_a1"]),),
    ).fetchone()
    audits = connection.execute(
        "SELECT action, target_id::text, reason FROM audit_event WHERE action IN "
        "('bot.decision_job.dead', 'bot.fanout.excluded') ORDER BY occurred_at"
    ).fetchall()
    return {
        "poison": list(jobs),
        "healthy": healthy[0],
        "a1_exclusion": exclusion[0] if exclusion else None,
        "audits": [list(row) for row in audits],
    }


async def runtime_queue(state):
    """MB05.8 dưới login runtime thật: poison job, backoff/DEAD, exclusion lưu bền."""
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.repositories.bot_decisions import (
        PostgresSignalDecisionStore,
    )
    from trading_bot.adapters.database.repositories.bot_runtime_leases import BotRuntimeLease
    from trading_bot.adapters.database.repositories.feature_snapshots import (
        FeatureSnapshotRepository,
    )
    from trading_bot.application.bot_decision_contexts import (
        EffectiveBotDecisionContextProvider,
        FanOutExclusionRecorder,
        RuntimeFanOutGate,
        load_published_bot_config,
    )
    from trading_bot.application.bot_dispatcher import (
        BotEvidenceDispatcher,
        JobRetryPolicy,
        SignalEvidenceFanOutConsumer,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.execution.runtime_control import RuntimeFence
    from trading_bot.domain.market.signal_evidence import (
        EvidenceSourceRef,
        build_shared_signal_evidence,
    )
    from trading_bot.domain.strategy.registry import StrategyRegistry

    workspace_a = UUID(state["workspace_a"])
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    snapshots = fixture_snapshots()
    out = {}
    try:
        async with UnitOfWork(sessions, workspace_id=workspace_a) as uow:
            feature = await FeatureSnapshotRepository(uow.session).get_as_of(
                workspace_id=workspace_a, symbol="BTCUSDT", timeframe="1h", as_of=DECISION_TIME
            )

        def evidence_for(name, minutes):
            return build_shared_signal_evidence(
                feature_snapshot=feature,
                setup_type=name,
                observed_at=DECISION_TIME - timedelta(minutes=1),
                available_at=DECISION_TIME + timedelta(minutes=minutes),
                validity_end=DECISION_TIME + timedelta(hours=1),
                sources=(
                    EvidenceSourceRef(f"market_bar:binance:BTCUSDT:1h:{name}", DECISION_TIME),
                ),
            )

        poison = evidence_for("mb05_poison", 0)
        healthy = evidence_for("mb05_healthy", 1)

        class Snapshots:
            async def snapshot_for(self, *, evidence, context, evaluated_at):
                if evidence.evidence_id == poison.evidence_id:
                    raise RuntimeError("snapshot provider lỗi cố ý (poison)")
                return snapshots[context.strategy_release_id]

        store = PostgresSignalDecisionStore(sessions)
        dispatcher = BotEvidenceDispatcher(
            store, strategies=StrategyRegistry(), snapshots=Snapshots()
        )
        # Một recorder dùng chung cho mọi provider của pha: khử nhiễu audit theo bộ nhớ process.
        held_accounts: dict = {}
        recorder = FanOutExclusionRecorder(
            sessions=sessions, workspace_id=workspace_a, accounts=held_accounts.get
        )

        async def bypass_gate(_uow, _bot):
            # Chỉ để dựng job cho A1 đã PAUSED (kiểm queue poison/backoff), không phải wiring thật.
            return None

        provider = EffectiveBotDecisionContextProvider(
            sessions, runtime_gate=bypass_gate, exclusion_recorder=recorder
        )
        contexts = await provider.contexts_for(
            workspace_id=workspace_a, as_of=DECISION_TIME + timedelta(minutes=5)
        )
        a1 = next(item for item in contexts if str(item.bot_id) == state["bot_a1"])
        for item, minutes in ((poison, 2), (healthy, 3)):
            await dispatcher.record_evidence(
                item, evaluated_at=DECISION_TIME + timedelta(minutes=minutes)
            )
            (job,) = await dispatcher.fan_out(
                item, (a1,), DECISION_TIME + timedelta(minutes=minutes)
            )
            out["poison_job_id" if item is poison else "healthy_job_id"] = str(job.job_id)
        policy = JobRetryPolicy(max_attempts=2, base_delay_seconds=1, max_delay_seconds=1)
        now = datetime.now(UTC)
        first = await dispatcher.process_pending(
            workspace_id=workspace_a, account_id=a1.account_id, now=now, policy=policy
        )
        out["first_round"] = [str(item.evidence_id) == str(healthy.evidence_id) for item in first]
        second = await dispatcher.process_pending(
            workspace_id=workspace_a,
            account_id=a1.account_id,
            now=now + timedelta(seconds=5),
            policy=policy,
        )
        out["second_round"] = len(second)
        out["after_dead"] = len(
            await dispatcher.process_pending(
                workspace_id=workspace_a,
                account_id=a1.account_id,
                now=now + timedelta(minutes=5),
                policy=policy,
            )
        )
        # Exclusion theo runtime: A1 PAUSED (control), A3 account DISABLED; recorder lưu bền.
        async with UnitOfWork(sessions, workspace_id=workspace_a) as uow:
            bot_a1 = await uow.bind_account(a1.account_id)
            lease = BotRuntimeLease(uow.session)
            db_now = await lease.database_now()
            grant = await lease.acquire_grant(
                workspace_a, bot_a1, "mb05-queue-probe", db_now + timedelta(seconds=120)
            )
        held = SimpleNamespace(
            account_id=a1.account_id,
            bot_id=bot_a1,
            fence=RuntimeFence(grant.owner_id, grant.fencing_token),
        )
        held_accounts[a1.account_id] = held
        gated = EffectiveBotDecisionContextProvider(
            sessions,
            load_config=load_published_bot_config,
            runtime_gate=RuntimeFanOutGate(),
            exclusion_recorder=recorder,
        )
        eligibility = await gated.eligibility(
            workspace_id=workspace_a, as_of=DECISION_TIME + timedelta(minutes=5)
        )
        out["exclusions"] = sorted(
            [str(item.bot_id), item.reason_code] for item in eligibility.exclusions
        )
        # Provider đã ghi qua recorder; ghi lại cùng mã không audit lặp.
        await recorder.record(eligibility)
        await recorder.record(eligibility)
        # Ack event outbox của hai evidence vừa ghi (consumer thật, ngoài gate runtime) để luồng
        # export/downgrade phía sau không còn event PENDING.
        consumer = SignalEvidenceFanOutConsumer(store, dispatcher, gated, worker_id="mb05-queue")
        await consumer.consume_once(workspace_id=workspace_a, now=datetime.now(UTC))
    finally:
        await engine.dispose()
    return out


def prepare_downgrade(connection):
    """Mô phỏng export xong: xóa lịch sử MB-03 bằng superuser để downgrade được phép."""
    # Hàng runtime MB-05 do pha queue tạo (control/lease/state) cũng phải export trước.
    for table, trigger in (
        ("bot_runtime_control", "bot_runtime_control_append_only"),
        ("bot_runtime_state", "bot_runtime_state_identity_guard"),
        ("bot_runtime_lease", "bot_runtime_lease_guard"),
    ):
        connection.execute(
            sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(
                sql.Identifier(table), sql.Identifier(trigger)
            )
        )
        connection.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
        connection.execute(
            sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(
                sql.Identifier(table), sql.Identifier(trigger)
            )
        )
    for table, trigger in (
        ("bot_decision", "bot_decision_immutable"),
        ("bot_decision_job", "bot_decision_job_transition_guard"),
        ("shared_signal_evidence", "shared_signal_evidence_immutable"),
    ):
        connection.execute(
            sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(
                sql.Identifier(table), sql.Identifier(trigger)
            )
        )
        connection.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
        connection.execute(
            sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(
                sql.Identifier(table), sql.Identifier(trigger)
            )
        )


def verify_downgraded(connection):
    for table in NEW_TABLES:
        assert count(connection, "SELECT to_regclass(%s) IS NULL", table) is True, table
    assert count(connection, "SELECT count(*) FROM bot_instance") == 4
    # Downgrade chỉ về MB-02: config bot đã publish vẫn còn nguyên.
    assert count(connection, "SELECT count(*) FROM bot_config_version") == 2


password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
phase = os.environ["MB03_PHASE"]
state = json.loads(os.environ["MB03_STATE"] or "{}")
if phase == "dispatch":
    print(json.dumps(asyncio.run(dispatch(state))))
elif phase == "runtime_queue":
    print(json.dumps(asyncio.run(runtime_queue(state))))
else:
    with psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        autocommit=True,
    ) as database:
        if phase == "seed":
            with database.transaction():
                print(json.dumps(seed(database)))
        elif phase == "verify":
            verify(database, state)
            print(json.dumps({"verified": True}))
        elif phase == "prepare_downgrade":
            with database.transaction():
                prepare_downgrade(database)
        elif phase == "verify_downgraded":
            verify_downgraded(database)
        elif phase == "pause_controls":
            with database.transaction():
                pause_controls(database, state)
            print(json.dumps({"paused": True}))
        elif phase == "verify_queue":
            print(json.dumps(verify_queue(database, state)))
        else:
            raise SystemExit(f"phase không hỗ trợ: {phase}")
