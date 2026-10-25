"""Probe độc lập chạy trong migrator; seed trước MB-01 không dùng ORM mới."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

TABLES = (
    "paper_account_seed",
    "config_version",
    "approval_request",
    "trade_plan",
    "risk_decision",
    "risk_reservation",
    "order_intent",
    "exchange_order",
    "fill",
    "position_event",
    "trade",
    "trade_cost",
    "paper_execution_checkpoint",
)
# Bảng bot-scoped do migration sau MB-01 thêm (MB-02); vẫn kiểm chặt không có index lạ.
LATER_BOT_SCOPED_TABLES = ("bot_config_version", "bot_config_request")
BOT_SCOPE_INDEXES = {f"ix_{table}_bot_scope" for table in (*TABLES, *LATER_BOT_SCOPED_TABLES)}
NOW = datetime(2026, 10, 24, tzinfo=UTC)


def identity(name):
    return uuid5(NAMESPACE_URL, "crypidian-mb01-e2e:" + name)


def insert(connection, table, **values):
    values.setdefault("id", identity(table))
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, values)),
        sql.SQL(",").join(sql.Placeholder() for _ in values),
    )
    connection.execute(query, tuple(values.values()))


def snapshot(connection):
    return {
        table: connection.execute(
            sql.SQL(
                "SELECT COALESCE(jsonb_agg(to_jsonb(t) - 'bot_id' ORDER BY id), "
                "'[]'::jsonb) FROM {} t"
            ).format(sql.Identifier(table))
        ).fetchone()[0]
        for table in TABLES
    }


def seed(connection):
    workspace, account, member = identity("workspace"), identity("account"), identity("member")
    insert(
        connection,
        "workspace",
        id=workspace,
        name="MB01",
        slug="mb01-e2e",
        mode="PAPER",
        status="ACTIVE",
    )
    insert(
        connection,
        "user_account",
        email_normalized="mb01@example.test",
        password_hash="unused",
        status="ACTIVE",
    )
    insert(
        connection,
        "user_membership",
        id=member,
        workspace_id=workspace,
        user_id=identity("user_account"),
        status="ACTIVE",
    )
    insert(connection, "role_binding", workspace_id=workspace, membership_id=member, role="owner")
    insert(
        connection,
        "trading_account",
        id=account,
        workspace_id=workspace,
        name="MB01 account",
        venue="binance_usdm",
        mode="PAPER",
        environment="local",
        status="ENABLED",
        max_notional=10000,
    )
    insert(
        connection,
        "trading_account",
        id=identity("disabled_account"),
        workspace_id=workspace,
        name="Disabled account",
        venue="binance_usdm",
        mode="PAPER",
        environment="local",
        status="DISABLED",
        max_notional=10000,
    )
    insert(
        connection,
        "workspace_settings_version",
        workspace_id=workspace,
        version=1,
        schema_version=1,
        settings_json=Jsonb({"historical": True}),
        settings_hash="a" * 64,
        status="DRAFT",
        revision=1,
        created_by=member,
    )
    insert(
        connection,
        "strategy_release",
        workspace_id=workspace,
        name="historical",
        version=1,
        status="PUBLISHED",
        recipe=Jsonb({}),
        content_hash="a" * 64,
    )
    insert(
        connection,
        "feature_snapshot",
        workspace_id=workspace,
        snapshot_id="f" * 64,
        content_hash="f" * 64,
        symbol="BTCUSDT",
        timeframe="1h",
        as_of=NOW,
        computed_at=NOW,
        schema_hash="a" * 64,
        formula_hash="b" * 64,
        code_hash="c" * 64,
        input_bar_ids=Jsonb([]),
        input_event_ids=Jsonb([]),
        features=Jsonb({}),
        warmup=Jsonb({}),
        quality_flags=Jsonb([]),
        data_freshness="FRESH",
        data_coverage=1,
        regime=Jsonb({"label": "UNKNOWN"}),
        is_usable=True,
        canonical_json='{"historical":true}',
    )
    insert(
        connection,
        "paper_account_seed",
        workspace_id=workspace,
        trading_account_id=account,
        quote_asset="USDT",
        initial_equity=10000,
        source_settings_version_id=identity("workspace_settings_version"),
        source_settings_hash="a" * 64,
        idempotency_key="historical-seed",
    )
    insert(
        connection,
        "config_version",
        workspace_id=workspace,
        trading_account_id=account,
        version=1,
        config=Jsonb({"legacy": True}),
        config_hash="a" * 64,
    )
    insert(
        connection,
        "approval_request",
        workspace_id=workspace,
        trading_account_id=account,
        subject_type="config",
        subject_id=identity("config_version"),
        requested_by=member,
        status="PENDING",
    )
    insert(
        connection,
        "approval_request",
        id=identity("workspace_approval"),
        workspace_id=workspace,
        subject_type="workspace",
        subject_id=workspace,
        requested_by=member,
        status="PENDING",
    )
    scope = {"workspace_id": workspace, "account_id": account}
    insert(
        connection,
        "trade_plan",
        **scope,
        plan_hash="b" * 64,
        symbol="BTCUSDT",
        notional=100,
        payload=Jsonb({"canonical": "preserve"}),
    )
    insert(
        connection,
        "risk_decision",
        **scope,
        decision_key="c" * 64,
        plan_hash="b" * 64,
        snapshot_id="f" * 64,
        approved=True,
        reason_code="APPROVED",
        detail_vi="Đã kiểm tra",
        payload=Jsonb({"historical": True}),
    )
    insert(
        connection,
        "risk_reservation",
        **scope,
        risk_decision_id=identity("risk_decision"),
        idempotency_key="historical-reservation",
        plan_hash="b" * 64,
        symbol="BTCUSDT",
        reserved_risk=10,
        reserved_notional=100,
        reserved_initial_margin=20,
        expires_at=NOW + timedelta(days=1),
        created_at=NOW,
        status="RESERVED",
    )
    lineage = {
        "settings_version_id": identity("workspace_settings_version"),
        "strategy_release_id": identity("strategy_release"),
        "strategy_release_hash": "a" * 64,
        "feature_snapshot_id": "f" * 64,
        "candidate_id": "d" * 64,
        "risk_decision_id": identity("risk_decision"),
        "risk_reservation_id": identity("risk_reservation"),
    }
    insert(
        connection,
        "order_intent",
        **scope,
        **lineage,
        mode="PAPER",
        idempotency_key="historical-intent",
        plan_hash="b" * 64,
        symbol="BTCUSDT",
        quote_asset="USDT",
        side="LONG",
        quantity=1,
        filled_quantity=1,
        status="FILLED",
        created_at=NOW,
        evidence=Jsonb({"historical": True}),
    )
    insert(
        connection,
        "exchange_order",
        **scope,
        mode="PAPER",
        intent_id=identity("order_intent"),
        client_order_id="historical-order",
        status="FILLED",
    )
    insert(
        connection,
        "fill",
        **scope,
        mode="PAPER",
        intent_id=identity("order_intent"),
        fill_role="ENTRY",
        fill_key="historical-fill",
        bar_key="bar",
        fill_policy_version="paper-v1",
        quantity=1,
        price=100,
        fee_amount=1,
        slippage_amount=0,
        occurred_at=NOW,
        lineage=Jsonb({"historical": True}),
    )
    insert(
        connection,
        "position_event",
        **scope,
        mode="PAPER",
        intent_id=identity("order_intent"),
        event_key="historical-open",
        event_type="OPENED",
        quantity=1,
        occurred_at=NOW,
        payload=Jsonb({}),
        lineage=Jsonb({"historical": True}),
    )
    insert(
        connection,
        "trade",
        **scope,
        **lineage,
        mode="PAPER",
        intent_id=identity("order_intent"),
        status="OPEN",
        symbol="BTCUSDT",
        quote_asset="USDT",
        side="LONG",
        gross_pnl=0,
        fee_total=0,
        funding_total=0,
        slippage_total=0,
        total_cost=0,
        net_pnl=0,
        opened_at=NOW,
        lineage=Jsonb({"historical": True}),
    )
    insert(
        connection,
        "trade_cost",
        **scope,
        mode="PAPER",
        trade_id=identity("trade"),
        cost_key="historical-fee",
        cost_type="FEE",
        amount=1,
        currency="USDT",
        occurred_at=NOW,
        source_event_id="historical-fill",
        lineage=Jsonb({"historical": True}),
    )
    insert(
        connection,
        "paper_execution_checkpoint",
        **scope,
        mode="PAPER",
        revision=1,
        event_key="historical-event",
        event_hash="a" * 64,
        event_at=NOW,
        schema_version=2,
        policy_hash="b" * 64,
        snapshot_hash="c" * 64,
        snapshot_payload='{"schema_version":2,"historical":true}',
    )
    insert(
        connection,
        "fill",
        id=identity("exit_fill"),
        **scope,
        mode="PAPER",
        intent_id=identity("order_intent"),
        fill_role="EXIT",
        fill_key="historical-exit",
        bar_key="exit-bar",
        fill_policy_version="paper-v1",
        quantity=1,
        price=110,
        fee_amount=0,
        slippage_amount=0,
        occurred_at=NOW + timedelta(hours=1),
        lineage=Jsonb({"historical": True}),
    )
    connection.execute(
        "UPDATE trade SET status='CLOSED', closed_at=%s, gross_pnl=10, "
        "fee_total=1, total_cost=1, net_pnl=9 WHERE id=%s",
        (NOW + timedelta(hours=1), identity("trade")),
    )
    return snapshot(connection)


def scope(connection, role, workspace, bot="", member=""):
    connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    connection.execute(
        "SELECT set_config('app.workspace_id', %s, true), "
        "set_config('app.bot_id',%s,true), set_config('app.membership_id',%s,true)",
        (str(workspace), str(bot), str(member)),
    )


def rejected(connection, action, state=None):
    try:
        with connection.transaction():
            action()
    except psycopg.Error as error:
        if state is not None and error.sqlstate != state:
            raise AssertionError((error.sqlstate, str(error))) from error
        return
    raise AssertionError("DB phải từ chối thao tác sai scope/quyền")


def verify(connection, previous):
    assert snapshot(connection) == previous
    workspace, account, member = identity("workspace"), identity("account"), identity("member")
    rows = connection.execute(
        "SELECT id, trading_account_id, status FROM bot_instance ORDER BY trading_account_id"
    ).fetchall()
    assert len(rows) == 2
    bots = {row[1]: row[0] for row in rows}
    bot, sibling = bots[account], bots[identity("disabled_account")]
    assert {row[2] for row in rows} == {"ENABLED", "DISABLED"}
    # Bot mirror account: bật/đổi tên account cập nhật bot, rồi khôi phục trạng thái ban đầu.
    sibling_account = identity("disabled_account")
    connection.execute(
        "UPDATE trading_account SET status='ENABLED', name='Renamed account' WHERE id=%s",
        (sibling_account,),
    )
    assert connection.execute(
        "SELECT name, status FROM bot_instance WHERE trading_account_id=%s", (sibling_account,)
    ).fetchone() == ("Renamed account", "ENABLED")
    connection.execute(
        "UPDATE trading_account SET status='DISABLED', name='Disabled account' WHERE id=%s",
        (sibling_account,),
    )
    assert connection.execute(
        "SELECT name, status FROM bot_instance WHERE trading_account_id=%s", (sibling_account,)
    ).fetchone() == ("Disabled account", "DISABLED")
    for table in TABLES:
        column = (
            "trading_account_id"
            if table in {"paper_account_seed", "config_version", "approval_request"}
            else "account_id"
        )
        assert (
            connection.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE {} IS NOT NULL AND bot_id <> %s").format(
                    sql.Identifier(table), sql.Identifier(column)
                ),
                (bot,),
            ).fetchone()[0]
            == 0
        )
    assert (
        connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgenabled = 'D'"
        ).fetchone()[0]
        == 0
    )
    # Mỗi bảng bot-scoped có index hợp lệ cho predicate RLS và FK ghép.
    indexed = {
        row[0]
        for row in connection.execute(
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE i.indisvalid AND c.relname LIKE 'ix_%%_bot_scope'"
        ).fetchall()
    }
    assert indexed == BOT_SCOPE_INDEXES, indexed
    rejected(connection, lambda: connection.execute("UPDATE fill SET price=101"))
    rejected(
        connection,
        lambda: connection.execute(
            "UPDATE bot_instance SET trading_account_id=%s WHERE id=%s",
            (identity("disabled_account"), bot),
        ),
    )
    rejected(
        connection,
        lambda: insert(
            connection,
            "bot_instance",
            id=identity("duplicate"),
            workspace_id=workspace,
            trading_account_id=account,
            name="duplicate",
            status="ENABLED",
        ),
        "23505",
    )
    for role in ("crypidian_trading_runtime", "crypidian_admin_web"):
        with connection.transaction():
            scope(connection, role, workspace)
            assert connection.execute("SELECT count(*) FROM trade_plan").fetchone()[0] == 0
            # Approval workspace-only (bot NULL) giữ hành vi trước MB-01 cho runtime.
            approvals = connection.execute("SELECT count(*) FROM approval_request").fetchone()[0]
            assert approvals == (1 if role == "crypidian_trading_runtime" else 0), (role, approvals)
        with connection.transaction():
            scope(connection, role, workspace, bot=bot)
            expected = 1 if role == "crypidian_trading_runtime" else 0
            assert connection.execute("SELECT count(*) FROM trade_plan").fetchone()[0] == expected
    with connection.transaction():
        scope(connection, "crypidian_trading_runtime", workspace, bot=bot)
        rejected(
            connection,
            lambda: insert(
                connection,
                "trade_plan",
                id=identity("wrongbot"),
                workspace_id=workspace,
                bot_id=sibling,
                account_id=account,
                plan_hash="e" * 64,
                symbol="BTCUSDT",
                notional=1,
                payload=Jsonb({}),
            ),
            "42501",
        )
        rejected(
            connection,
            lambda: insert(
                connection,
                "trade_plan",
                id=identity("wrongaccount"),
                workspace_id=workspace,
                bot_id=bot,
                account_id=identity("disabled_account"),
                plan_hash="e" * 64,
                symbol="BTCUSDT",
                notional=1,
                payload=Jsonb({}),
            ),
            "23503",
        )
        assert connection.execute("SELECT count(*) FROM approval_request").fetchone()[0] == 2
        # Hàng workspace-only chỉ đọc; bot không được sửa hoặc chuyển hàng của mình thành NULL.
        assert (
            connection.execute(
                "UPDATE approval_request SET status=status WHERE bot_id IS NULL"
            ).rowcount
            == 0
        )
        rejected(
            connection,
            lambda: connection.execute(
                "UPDATE approval_request SET bot_id=NULL, trading_account_id=NULL "
                "WHERE bot_id IS NOT NULL"
            ),
            "42501",
        )
    with connection.transaction():
        scope(connection, "crypidian_trading_runtime", workspace, bot=sibling)
        assert (
            connection.execute(
                "SELECT count(*) FROM approval_request WHERE bot_id IS NULL"
            ).fetchone()[0]
            == connection.execute("SELECT count(*) FROM approval_request").fetchone()[0]
            == 1
        )
    with connection.transaction():
        scope(connection, "crypidian_admin_web", workspace, member=member)
        assert connection.execute("SELECT count(*) FROM trade_plan").fetchone()[0] == 1
    # Operator chỉ đọc bot có grant; membership/role khác không suy từ request bot_id.
    insert(
        connection,
        "user_account",
        id=identity("operator_user"),
        email_normalized="operator@example.test",
        password_hash="unused",
        status="ACTIVE",
    )
    insert(
        connection,
        "user_membership",
        id=identity("operator_member"),
        workspace_id=workspace,
        user_id=identity("operator_user"),
        status="ACTIVE",
    )
    insert(
        connection,
        "role_binding",
        id=identity("operator_role"),
        workspace_id=workspace,
        membership_id=identity("operator_member"),
        role="operator",
    )
    insert(
        connection,
        "bot_grant",
        workspace_id=workspace,
        bot_id=bot,
        membership_id=identity("operator_member"),
        permission="bot:read",
        granted_by=member,
    )
    with connection.transaction():
        scope(
            connection,
            "crypidian_admin_web",
            workspace,
            bot=sibling,
            member=identity("operator_member"),
        )
        assert connection.execute("SELECT count(*) FROM trade_plan").fetchone()[0] == 1
    connection.execute("UPDATE bot_grant SET revoked_at=now()")
    with connection.transaction():
        scope(
            connection,
            "crypidian_admin_web",
            workspace,
            bot=bot,
            member=identity("operator_member"),
        )
        assert connection.execute("SELECT count(*) FROM trade_plan").fetchone()[0] == 0
        # Grant đã thu hồi: chỉ còn approval workspace-only nhờ membership ACTIVE.
        assert (
            connection.execute(
                "SELECT count(*) FROM approval_request WHERE bot_id IS NULL"
            ).fetchone()[0]
            == connection.execute("SELECT count(*) FROM approval_request").fetchone()[0]
            == 1
        )
    rejected(
        connection, lambda: connection.execute("UPDATE bot_grant SET permission='bot:report:read'")
    )
    rejected(connection, lambda: connection.execute("UPDATE bot_grant SET revoked_at=NULL"))
    insert(
        connection,
        "bot_grant",
        id=identity("reissued_grant"),
        workspace_id=workspace,
        bot_id=bot,
        membership_id=identity("operator_member"),
        permission="bot:read",
        granted_by=member,
    )
    assert (
        connection.execute("SELECT count(*) FROM bot_grant WHERE revoked_at IS NULL").fetchone()[0]
        == 1
    )
    for role, expected in (("strategy_admin", 1), ("risk_admin", 1), ("OWNER", 0)):
        user, role_member = identity(role + "-user"), identity(role + "-member")
        insert(
            connection,
            "user_account",
            id=user,
            email_normalized=role.lower() + "-matrix@example.test",
            password_hash="unused",
            status="ACTIVE",
        )
        insert(
            connection,
            "user_membership",
            id=role_member,
            workspace_id=workspace,
            user_id=user,
            status="ACTIVE",
        )
        insert(
            connection,
            "role_binding",
            id=identity(role + "-binding"),
            workspace_id=workspace,
            membership_id=role_member,
            role=role,
        )
        with connection.transaction():
            scope(connection, "crypidian_admin_web", workspace, bot=bot, member=role_member)
            assert connection.execute("SELECT count(*) FROM trade_plan").fetchone()[0] == expected
    assert not connection.execute(
        "SELECT has_table_privilege('crypidian_ingestion_worker','bot_instance','INSERT')"
    ).fetchone()[0]
    assert not connection.execute(
        "SELECT has_table_privilege('crypidian_trading_runtime','bot_grant','UPDATE')"
    ).fetchone()[0]
    assert connection.execute(
        "SELECT proconfig FROM pg_proc WHERE proname='provision_account_bot'"
    ).fetchone()[0] == ["search_path=pg_catalog, pg_temp"]


def actual_process_role(connection, *, runtime):
    """Dùng login process thật để kiểm cả session_user và quyền SET ROLE."""
    role = "crypidian_trading_runtime" if runtime else "crypidian_admin_web"
    assert connection.execute("SELECT current_user,session_user").fetchone() == (role, role)
    workspace, account = identity("workspace"), identity("account")
    with connection.transaction():
        scope(connection, role, workspace, member="" if runtime else identity("member"))
        assert connection.execute(
            "SELECT has_table_privilege(current_user,'trade_plan','SELECT')"
        ).fetchone()[0]
        assert connection.execute("SELECT count(*) FROM trade_plan").fetchone()[0] == (
            0 if runtime else 1
        )
        bot = connection.execute(
            "SELECT id FROM bot_instance WHERE trading_account_id=%s", (account,)
        ).fetchone()[0]
        connection.execute("SELECT set_config('app.bot_id',%s,true)", (str(bot),))
        assert connection.execute("SELECT count(*) FROM trade_plan").fetchone()[0] == 1
        if runtime:
            sibling = connection.execute(
                "SELECT id FROM bot_instance WHERE trading_account_id=%s",
                (identity("disabled_account"),),
            ).fetchone()[0]
            connection.execute("SELECT set_config('app.bot_id',%s,true)", (str(sibling),))
            assert connection.execute("SELECT count(*) FROM trade_plan").fetchone()[0] == 0
            assert (
                connection.execute(
                    "UPDATE order_intent SET status='UNKNOWN' WHERE id=%s",
                    (identity("order_intent"),),
                ).rowcount
                == 0
            )
            rejected(
                connection,
                lambda: connection.execute("SET LOCAL ROLE crypidian_admin_web"),
                "42501",
            )
            rejected(
                connection,
                lambda: connection.execute("SET LOCAL ROLE crypidian_bot_provisioner"),
                "42501",
            )
            rejected(
                connection,
                lambda: connection.execute("SELECT public.provision_account_bot()"),
                "42501",
            )
            connection.execute("SET LOCAL ROLE crypidian_app")
            assert connection.execute("SELECT count(*) FROM trade_plan").fetchone()[0] == 0
    with connection.transaction():
        # Transaction kế tiếp không được thừa hưởng membership/bot/workspace GUC trước đó.
        assert connection.execute("SELECT count(*) FROM trade_plan").fetchone()[0] == 0
    if runtime:
        with connection.transaction():
            scope(connection, role, workspace)
            values = {
                "workspace_id": workspace,
                "name": "runtime-provisioned",
                "venue": "binance_usdm",
                "mode": "PAPER",
                "environment": "local",
                "status": "DISABLED",
                "max_notional": 1000,
            }
            insert(connection, "trading_account", id=identity("runtime-created-account"), **values)
            assert (
                connection.execute(
                    "SELECT count(*) FROM bot_instance WHERE trading_account_id=%s",
                    (identity("runtime-created-account"),),
                ).fetchone()[0]
                == 1
            )
            rejected(
                connection,
                lambda: insert(
                    connection,
                    "trading_account",
                    id=identity("unsupported-runtime-account"),
                    **{**values, "name": "unsupported", "mode": "DEMO"},
                ),
            )


password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
with psycopg.connect(
    host="postgres",
    dbname="crypidian",
    user=os.environ["TRADING_BOT_DATABASE_USER"],
    password=password,
    autocommit=True,
) as database:
    phase = os.environ["MB01_PHASE"]
    if phase == "seed":
        with database.transaction():
            print(json.dumps(seed(database), default=str))
    elif phase == "verify":
        verify(database, json.loads(os.environ["MB01_SNAPSHOT"]))
    elif phase == "prepare_downgrade":
        database.execute("ALTER TABLE bot_grant DISABLE TRIGGER bot_grant_history_guard")
        database.execute("DELETE FROM bot_grant")
        database.execute("ALTER TABLE bot_grant ENABLE TRIGGER bot_grant_history_guard")
    elif phase == "actual_runtime":
        actual_process_role(database, runtime=True)
    elif phase == "actual_admin":
        actual_process_role(database, runtime=False)
    elif phase == "unsupported_account":
        database.execute(
            "UPDATE trading_account SET mode='DEMO' WHERE id=%s", (identity("disabled_account"),)
        )
    elif phase == "restore_account":
        assert database.execute("SELECT to_regclass('bot_instance')").fetchone()[0] is None
        database.execute(
            "UPDATE trading_account SET mode='PAPER' WHERE id=%s", (identity("disabled_account"),)
        )
    elif phase == "precreate_provisioner_role":
        assert not database.execute(
            "SELECT 1 FROM pg_roles WHERE rolname='crypidian_bot_provisioner'"
        ).fetchone()
        # Role có sẵn nhưng LOGIN phải làm upgrade dừng; sau đó hạ về NOLOGIN an toàn.
        database.execute("CREATE ROLE crypidian_bot_provisioner LOGIN")
    elif phase == "harden_provisioner_role":
        database.execute("ALTER ROLE crypidian_bot_provisioner NOLOGIN")
    elif phase == "verify_reupgrade":
        # Sau re-upgrade với role có sẵn: mỗi account đúng một bot, trigger/index đầy đủ.
        assert (
            database.execute(
                "SELECT count(*) FROM trading_account a LEFT JOIN bot_instance b "
                "ON (b.workspace_id, b.trading_account_id) = (a.workspace_id, a.id) "
                "GROUP BY a.id HAVING count(b.id) <> 1"
            ).fetchall()
            == []
        )
        assert (
            database.execute(
                "SELECT pg_get_userbyid(proowner) FROM pg_proc "
                "WHERE proname='provision_account_bot'"
            ).fetchone()[0]
            == "crypidian_bot_provisioner"
        )
        assert (
            database.execute(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgenabled = 'D'"
            ).fetchone()[0]
            == 0
        )
        assert database.execute(
            "SELECT count(*) FROM pg_class WHERE relname LIKE 'ix_%%_bot_scope'"
        ).fetchone()[0] == len(BOT_SCOPE_INDEXES)
    elif phase == "verify_legacy":
        assert snapshot(database) == json.loads(os.environ["MB01_SNAPSHOT"])
    else:
        raise AssertionError(phase)
