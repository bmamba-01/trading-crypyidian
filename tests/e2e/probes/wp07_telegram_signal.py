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
        result = {"signal_scope_grants": True, "order_privileges": False}
    else:
        raise AssertionError(phase)
    print(json.dumps(result, default=str))
