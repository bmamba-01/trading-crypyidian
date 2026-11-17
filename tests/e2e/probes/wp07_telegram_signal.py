"""Probe WP-07B chạy dưới migrator và login signal-worker thật trong Compose."""

import json
import os
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg

STATE = json.loads(os.environ["WP07_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
MEMBERSHIP = UUID(STATE["membership_id"])
BINDING = UUID(STATE["binding_id"])
SUBSCRIPTION = UUID(STATE["subscription_id"])
BOT = UUID(STATE["bot_id"])
ACCOUNT = UUID(STATE["account_id"])


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, "crypidian-wp07-signal-e2e:" + name)


def _signal_targets(connection: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    # Probe chạy autocommit, nên GUC phải có hiệu lực ở cấp session giữa hai câu lệnh.
    connection.execute(
        "SELECT set_config('app.workspace_id', %s, false), set_config('app.bot_id', %s, false)",
        (str(WORKSPACE), str(BOT)),
    )
    return cast(
        list[tuple[Any, ...]],
        connection.execute(
            "SELECT s.id, s.bot_id, s.telegram_chat_id, s.symbols, s.timeframes, "
            "s.directions, s.strategy_ids, s.setup_codes "
            "FROM telegram_signal_subscription s "
            "JOIN telegram_binding b ON b.workspace_id=s.workspace_id "
            "AND b.id=s.telegram_binding_id AND b.membership_id=s.membership_id "
            "JOIN user_membership m ON m.workspace_id=s.workspace_id "
            "AND m.id=s.membership_id "
            "WHERE s.workspace_id=%s AND s.enabled=true AND b.verified_at IS NOT NULL "
            "AND b.revoked_at IS NULL AND m.status='ACTIVE'",
            (WORKSPACE,),
        ).fetchall(),
    )


def _scalar(connection: psycopg.Connection[Any], query: str, params: tuple[Any, ...] = ()) -> Any:
    row = connection.execute(query, params).fetchone()
    assert row is not None
    return row[0]


password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
with psycopg.connect(
    host="postgres",
    dbname="crypidian",
    user=os.environ["TRADING_BOT_DATABASE_USER"],
    password=password,
    autocommit=True,
) as connection:
    phase = os.environ["WP07_PHASE"]
    result: dict[str, Any]
    if phase == "seed":
        connection.execute(
            "INSERT INTO workspace (id, name, slug, mode, status) "
            "VALUES (%s, 'WP07 signal E2E', %s, 'PAPER', 'ACTIVE')",
            (WORKSPACE, "wp07-signal-" + WORKSPACE.hex[:12]),
        )
        user_id = _id("user")
        connection.execute(
            "INSERT INTO user_account (id, email_normalized, password_hash, status) "
            "VALUES (%s, %s, 'e2e-placeholder', 'ACTIVE')",
            (user_id, f"wp07-{WORKSPACE.hex[:12]}@example.invalid"),
        )
        connection.execute(
            "INSERT INTO user_membership (id, workspace_id, user_id, status) "
            "VALUES (%s, %s, %s, 'ACTIVE')",
            (MEMBERSHIP, WORKSPACE, user_id),
        )
        connection.execute(
            "INSERT INTO trading_account (id, workspace_id, name, venue, mode, environment, "
            "status, max_notional) VALUES (%s, %s, 'signal-e2e', 'binance_usdm', 'PAPER', "
            "'local', 'ENABLED', 1000)",
            (ACCOUNT, WORKSPACE),
        )
        bot_row = connection.execute(
            "SELECT id FROM bot_instance WHERE workspace_id=%s AND trading_account_id=%s",
            (WORKSPACE, ACCOUNT),
        ).fetchone()
        assert bot_row is not None
        bot_id = bot_row[0]
        connection.execute(
            "INSERT INTO telegram_binding (id, workspace_id, membership_id, telegram_user_id, "
            "verified_at, revoked_at) VALUES (%s, %s, %s, '987654321', now(), NULL)",
            (BINDING, WORKSPACE, MEMBERSHIP),
        )
        connection.execute(
            "INSERT INTO telegram_signal_subscription (id, workspace_id, membership_id, bot_id, "
            "telegram_binding_id, telegram_chat_id, enabled, symbols, timeframes, directions, "
            "strategy_ids, setup_codes) "
            "VALUES (%s, %s, %s, %s, %s, 987654321, true, '[\"BTCUSDT\"]'::jsonb, "
            "'[\"1h\"]'::jsonb, '[\"LONG\"]'::jsonb, '[\"H01\"]'::jsonb, "
            "'[\"H01_BREAKOUT\"]'::jsonb)",
            (SUBSCRIPTION, WORKSPACE, MEMBERSHIP, bot_id, BINDING),
        )
        result = {"seeded": True, "bot_id": str(bot_id)}
    elif phase == "worker_active_binding":
        targets = _signal_targets(connection)
        assert targets == [
            (SUBSCRIPTION, BOT, 987654321, ["BTCUSDT"], ["1h"], ["LONG"], ["H01"], ["H01_BREAKOUT"])
        ], targets
        assert _scalar(connection, "SELECT current_user = 'crypidian_signal_worker'")
        assert not _scalar(
            connection,
            "SELECT has_table_privilege(current_user, %s, %s)",
            ("order_intent", "INSERT"),
        )
        result = {"active_targets": len(targets), "order_insert": False}
    elif phase == "admin_set_topics":
        # TG-4: admin-web thật (role crypidian_admin_web) lưu topic qua store của ứng dụng.
        import asyncio

        from trading_bot.adapters.database.engine import (
            create_database_engine,
            create_session_factory,
            database_url_from_secret,
        )
        from trading_bot.telegram.webhook import DatabaseTelegramStore

        async def _admin_topics() -> dict[str, Any]:
            engine = create_database_engine(
                database_url_from_secret(
                    username=os.environ["TRADING_BOT_DATABASE_USER"],
                    password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
                    host="postgres",
                    database="crypidian",
                )
            )
            try:
                store = DatabaseTelegramStore(create_session_factory(engine))
                chat = await store.binding_chat(WORKSPACE, MEMBERSHIP)
                saved = await store.set_topics(
                    WORKSPACE, MEMBERSHIP, MEMBERSHIP, signal_thread_id=55, report_thread_id=66
                )
                listed = await store.list_bindings(WORKSPACE)
                return {"chat": chat, "saved": saved, "topics": [b["topics"] for b in listed]}
            finally:
                await engine.dispose()

        result = asyncio.run(_admin_topics())
        assert _scalar(connection, "SELECT current_user = 'crypidian_admin_web'")
        assert not _scalar(
            connection,
            "SELECT has_column_privilege(current_user, 'telegram_binding', "
            "'telegram_user_id', 'UPDATE')",
        )
        try:
            connection.execute(
                "SELECT set_config('app.workspace_id', %s, false)", (str(WORKSPACE),)
            )
            connection.execute(
                "UPDATE telegram_binding SET report_thread_id=0 WHERE workspace_id=%s AND id=%s",
                (WORKSPACE, BINDING),
            )
        except psycopg.errors.CheckViolation:
            result["zero_thread_rejected"] = True
    elif phase == "worker_topic_routing":
        # TG-4: repository + worker-side routing thật dưới role signal-worker.
        import asyncio

        from trading_bot.adapters.database.engine import (
            create_database_engine,
            create_session_factory,
            database_url_from_secret,
        )
        from trading_bot.adapters.database.repositories.telegram_signals import (
            TelegramSignalRepository,
        )
        from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
        from trading_bot.adapters.telegram.topic_sender import bind_topic
        from trading_bot.application.outbox import UnitOfWork

        async def _routing() -> dict[str, Any]:
            engine = create_database_engine(
                database_url_from_secret(
                    username=os.environ["TRADING_BOT_DATABASE_USER"],
                    password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
                    host="postgres",
                    database="crypidian",
                )
            )
            try:
                async with UnitOfWork(
                    create_session_factory(engine), workspace_id=WORKSPACE, bot_id=BOT
                ) as uow:
                    targets = await TelegramSignalRepository(
                        uow.session
                    ).list_enabled_subscriptions(workspace_id=WORKSPACE)
            finally:
                await engine.dispose()
            sender = RecordingTelegramSender()
            for target in targets:
                await bind_topic(sender, target.report_thread_id).send_message(
                    target.chat_id, "PAPER report"
                )
            return {"threads": [list(item) for item in sender.threads]}

        result = asyncio.run(_routing())
        connection.execute(
            "SELECT set_config('app.workspace_id', %s, false), set_config('app.bot_id', %s, false)",
            (str(WORKSPACE), str(BOT)),
        )
        result["signal_thread"] = _scalar(
            connection,
            "SELECT b.signal_thread_id FROM telegram_binding b WHERE b.workspace_id=%s AND b.id=%s",
            (WORKSPACE, BINDING),
        )
        for column in ("signal_thread_id", "report_thread_id"):
            assert not _scalar(
                connection,
                "SELECT has_column_privilege(current_user, 'telegram_binding', %s, 'UPDATE')",
                (column,),
            ), column
    elif phase == "revoke_binding":
        connection.execute(
            "UPDATE telegram_binding SET revoked_at=now() WHERE workspace_id=%s AND id=%s",
            (WORKSPACE, BINDING),
        )
        result = {"revoked": True}
    elif phase == "worker_revoked_binding":
        targets = _signal_targets(connection)
        assert targets == [], targets
        result = {"active_targets": 0, "revoked_binding_blocked": True}
    elif phase == "deactivate_member":
        connection.execute(
            "UPDATE telegram_binding SET revoked_at=NULL WHERE workspace_id=%s AND id=%s",
            (WORKSPACE, BINDING),
        )
        connection.execute(
            "UPDATE user_membership SET status='DISABLED' WHERE workspace_id=%s AND id=%s",
            (WORKSPACE, MEMBERSHIP),
        )
        result = {"membership_disabled": True}
    elif phase == "worker_inactive_member":
        targets = _signal_targets(connection)
        assert targets == [], targets
        result = {"active_targets": 0, "inactive_membership_blocked": True}
    elif phase == "acl":
        assert _scalar(
            connection,
            "SELECT has_column_privilege(current_user, 'telegram_signal_subscription', "
            "'telegram_chat_id', 'SELECT')",
        )
        assert _scalar(
            connection,
            "SELECT has_column_privilege(current_user, 'telegram_signal_delivery', "
            "'status', 'UPDATE')",
        )
        assert _scalar(
            connection,
            "SELECT has_column_privilege(current_user, 'bot_decision', 'payload', 'SELECT')",
        )
        assert _scalar(
            connection,
            "SELECT has_column_privilege(current_user, 'feature_snapshot', 'timeframe', 'SELECT')",
        )
        for column in ("symbols", "timeframes", "directions", "strategy_ids", "setup_codes"):
            assert _scalar(
                connection,
                "SELECT has_column_privilege('crypidian_signal_worker', "
                "'telegram_signal_subscription', %s, 'SELECT')",
                (column,),
            ), column
            assert _scalar(
                connection,
                "SELECT has_column_privilege('crypidian_admin_web', "
                "'telegram_signal_subscription', %s, 'UPDATE')",
                (column,),
            ), column
        assert _scalar(
            connection,
            "SELECT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conrelid='telegram_signal_delivery'::regclass AND contype='c' "
            "AND position('FILTERED' in pg_get_constraintdef(oid)) > 0)",
        )
        # TG-2: delivery bị chống spam ghi SUPPRESSED (migration c3e5a7b9d1f4).
        assert _scalar(
            connection,
            "SELECT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conrelid='telegram_signal_delivery'::regclass AND contype='c' "
            "AND position('SUPPRESSED' in pg_get_constraintdef(oid)) > 0)",
        )
        role_state = connection.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls, rolinherit, "
            "pg_has_role(current_user, 'crypidian_app', 'MEMBER') "
            "FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert role_state == (True, False, False, False, False), role_state
        assert not _scalar(
            connection,
            "SELECT has_column_privilege(current_user, 'telegram_binding', "
            "'telegram_user_id', 'SELECT')",
        )
        for table in (
            "order_intent",
            "fill",
            "trade",
            "trade_cost",
            "position_event",
            "risk_reservation",
            "venue_order_intent",
            "exchange_order",
        ):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not _scalar(
                    connection,
                    "SELECT has_table_privilege(current_user, %s, %s)",
                    (table, privilege),
                ), (table, privilege)
        # TG-3 (migration d5f7b9c1e3a6): chỉ SELECT theo cột ledger PAPER cần cho tin kết quả,
        # ghi delivery ledger riêng; không có quyền mức bảng hay quyền ghi ledger nào.
        for table, column in (
            ("trade", "net_pnl"),
            ("fill", "price"),
            ("position_event", "payload"),
            ("risk_reservation", "reserved_risk"),
            ("bot_instance", "name"),
            ("telegram_trade_notification", "status"),
        ):
            assert _scalar(
                connection,
                "SELECT has_column_privilege(current_user, %s, %s, 'SELECT')",
                (table, column),
            ), (table, column)
        for table, column in (
            ("trade", "lineage"),
            ("trade", "gross_pnl"),
            ("fill", "lineage"),
            ("risk_reservation", "reserved_notional"),
        ):
            assert not _scalar(
                connection,
                "SELECT has_column_privilege(current_user, %s, %s, 'SELECT')",
                (table, column),
            ), (table, column)
        for table in ("trade", "fill", "position_event", "risk_reservation", "bot_instance"):
            for privilege in ("INSERT", "UPDATE"):
                assert not _scalar(
                    connection,
                    "SELECT has_any_column_privilege(current_user, %s, %s)",
                    (table, privilege),
                ), (table, privilege)
        for table in ("telegram_trade_notification", "telegram_paper_report_delivery"):
            assert _scalar(
                connection,
                "SELECT has_column_privilege(current_user, %s, 'status', 'UPDATE')",
                (table,),
            ), table
            assert not _scalar(
                connection, "SELECT has_table_privilege(current_user, %s, 'DELETE')", (table,)
            ), table
        result = {"signal_scope_grants": True, "order_privileges": False}
    else:
        raise AssertionError(phase)
    print(json.dumps(result, default=str))
