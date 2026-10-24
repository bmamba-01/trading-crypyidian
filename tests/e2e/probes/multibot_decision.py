"""Probe MB-03 chạy trong container Compose; mã được gửi qua stdin.

Phase `seed`/`verify`/`prepare_downgrade`/`verify_downgraded` chạy bằng migrator
(superuser, dùng `SET LOCAL ROLE` để kiểm RLS từng role). Phase `dispatch` chạy bằng
login thật của `crypidian_trading_runtime` và gọi đúng code dispatcher/store của app.
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
        "account_b1": str(identity("account-b1")),
        "bot_a1": bots["a1"],
        "bot_a2": bots["a2"],
        "bot_b1": bots["b1"],
        "owner_member": str(identity("owner-member")),
        "operator_member": str(identity("operator-member")),
        "feature_snapshot_id": snapshot.snapshot_id,
    }


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
    from trading_bot.application.bot_dispatcher import (
        BotDecisionContext,
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

    def context(key, release, config):
        return BotDecisionContext(
            workspace_id=workspace_a,
            bot_id=UUID(state["bot_" + key]),
            account_id=UUID(state["account_" + key]),
            bot_config_version_id=identity(config),
            bot_config_hash=hashlib.sha256(config.encode()).hexdigest(),
            root_settings_version_id=identity("root-settings-v1"),
            root_settings_hash="1" * 64,
            strategy_release_id=release,
            symbols=("BTCUSDT",),
            effective_at=DECISION_TIME - timedelta(days=1),
        )

    contexts = (context("a1", "h01-v1", "config-a1-v1"), context("a2", "h03-v1", "config-a2-v1"))

    class Provider:
        async def contexts_for(self, *, workspace_id, as_of):
            assert workspace_id == workspace_a
            return contexts

    store = PostgresSignalDecisionStore(sessions)
    dispatcher = BotEvidenceDispatcher(store, strategies=StrategyRegistry(), snapshots=Snapshots())
    consumer = SignalEvidenceFanOutConsumer(store, dispatcher, Provider(), worker_id="mb03-e2e")
    try:
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
        first = await dispatcher.dispatch_evidence(evidence, contexts, DECISION_TIME)
        retry = await dispatcher.dispatch_evidence(evidence, contexts, DECISION_TIME)
        assert retry == first
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
        assert await dispatcher.record_evidence(revision, evaluated_at=revision_time) is True
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


def prepare_downgrade(connection):
    """Mô phỏng export xong: xóa lịch sử MB-03 bằng superuser để downgrade được phép."""
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
    assert count(connection, "SELECT count(*) FROM bot_instance") == 3


password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
phase = os.environ["MB03_PHASE"]
state = json.loads(os.environ["MB03_STATE"] or "{}")
if phase == "dispatch":
    print(json.dumps(asyncio.run(dispatch(state))))
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
        else:
            raise SystemExit(f"phase không hỗ trợ: {phase}")
