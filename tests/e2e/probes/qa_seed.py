"""Probe E2E cho dataset QA; chạy trong container Compose với login DB thật của service.

Phase:
- `snapshot` (migrator, role owner): số hàng + digest nội dung các bảng QA để so idempotency.
- `admin_matrix` (admin-web, `crypidian_admin_web`): RLS theo membership/grant và
  `BotGrantRepository.require` ở tầng ứng dụng.
- `runtime_scope` (trading-runtime, `crypidian_trading_runtime`): RLS theo bot scope.
- `mutate`/`verify_reset` (migrator): làm lệch baseline (gồm publish tay config bot B) và
  kiểm seed lại khôi phục.
- `feature_snapshot` (migrator) + `fanout` (trading-runtime): feature snapshot test-only rồi
  fan-out một shared evidence tới các bot QA có config PUBLISHED qua provider MB-02/MB-03.
Ngữ cảnh (ID dataset, bot ID, danh sách kiểm tra) đến từ test qua stdin, không chứa bí mật.
"""

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

PHASE = os.environ["QA_PHASE"]
CONTEXT = json.loads(os.environ["QA_CONTEXT"])
WORKSPACE_TABLES = (
    "user_membership",
    "role_binding",
    "mfa_factor",
    "secret_reference",
    "trading_account",
    "bot_instance",
    "bot_grant",
    "risk_profile_version",
    "workspace_settings_version",
    "paper_account_seed",
    "strategy_release",
    "bot_config_version",
    "market_instrument",
    "market_bar",
)
# Mốc dữ liệu QA (`MARKET_END` = `SEEDED_AT` của dataset); probe không import tests/.
MARKET_END = datetime(2026, 10, 1, tzinfo=UTC)
FANOUT_AS_OF = MARKET_END + timedelta(minutes=5)


def connect():
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    return psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        autocommit=True,
    )


def digest(connection, table, column, identifiers):
    count, content = connection.execute(
        sql.SQL(
            "SELECT count(*), md5(COALESCE(string_agg(to_jsonb(t)::text, '|' ORDER BY t.id), '')) "
            "FROM {} t WHERE {} = ANY(%s)"
        ).format(sql.Identifier(table), sql.Identifier(column)),
        (identifiers,),
    ).fetchone()
    return [count, content]


def snapshot():
    workspace_ids = [UUID(value) for value in CONTEXT["workspaces"].values()]
    user_ids = [UUID(member["user_id"]) for member in CONTEXT["members"].values()]
    with connect() as connection:
        result = {
            "workspace": digest(connection, "workspace", "id", workspace_ids),
            "user_account": digest(connection, "user_account", "id", user_ids),
        }
        for table in WORKSPACE_TABLES:
            result[table] = digest(connection, table, "workspace_id", workspace_ids)
    return result


def scope(connection, workspace_id, *, membership_id="", bot_id=""):
    connection.execute(
        "SELECT set_config('app.workspace_id', %s, true), set_config('app.membership_id', %s, "
        "true), set_config('app.bot_id', %s, true)",
        (str(workspace_id), str(membership_id), str(bot_id)),
    )


def account_keys(connection, query):
    by_id = {UUID(account["id"]): key for key, account in CONTEXT["accounts"].items()}
    return sorted(by_id.get(row[0], "FOREIGN") for row in connection.execute(query).fetchall())


def admin_matrix():
    observed = {"matrix": {}, "require": {}}
    with connect() as connection:
        identity = connection.execute("SELECT current_user, session_user").fetchone()
        observed["identity"] = list(identity)
        for member_key, member in CONTEXT["members"].items():
            for workspace_key, workspace_id in CONTEXT["workspaces"].items():
                with connection.transaction():
                    scope(connection, workspace_id, membership_id=member["membership_id"])
                    observed["matrix"][f"{member_key}@{workspace_key}"] = {
                        "bot_instance": account_keys(
                            connection, "SELECT trading_account_id FROM bot_instance"
                        ),
                        "paper_account_seed": account_keys(
                            connection, "SELECT trading_account_id FROM paper_account_seed"
                        ),
                        "bot_grant": connection.execute(
                            "SELECT count(*) FROM bot_grant"
                        ).fetchone()[0],
                    }
        with connection.transaction():
            # Không có membership context: role admin_web không thấy hàng bot nào.
            scope(connection, CONTEXT["workspaces"]["qa"])
            observed["matrix"]["anonymous@qa"] = {
                "bot_instance": account_keys(
                    connection, "SELECT trading_account_id FROM bot_instance"
                ),
                "paper_account_seed": account_keys(
                    connection, "SELECT trading_account_id FROM paper_account_seed"
                ),
                "bot_grant": connection.execute("SELECT count(*) FROM bot_grant").fetchone()[0],
            }
    observed["require"] = asyncio.run(require_checks())
    return observed


async def require_checks():
    from sqlalchemy import text

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
    from trading_bot.auth.bot_policy import BotPermission
    from trading_bot.auth.policy import Principal, Role

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    results = {}
    try:
        for member_key, account_key, permission in CONTEXT["require_checks"]:
            member = CONTEXT["members"][member_key]
            workspace_id = UUID(CONTEXT["workspaces"][member["workspace"]])
            principal = Principal(
                user_id=UUID(member["user_id"]),
                membership_id=UUID(member["membership_id"]),
                workspace_id=workspace_id,
                roles=frozenset(Role(role) for role in member["roles"]),
                mfa_verified_at=datetime.now(UTC),
            )
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        "SELECT set_config('app.workspace_id', :workspace, true), "
                        "set_config('app.membership_id', :membership, true)"
                    ),
                    {"workspace": str(workspace_id), "membership": member["membership_id"]},
                )
                try:
                    await BotGrantRepository(session).require(
                        principal,
                        bot_id=UUID(CONTEXT["bots"][account_key]),
                        permission=BotPermission(permission),
                    )
                except PermissionError:
                    allowed = False
                else:
                    allowed = True
            results[f"{member_key}:{account_key}:{permission}"] = allowed
    finally:
        await engine.dispose()
    return results


def runtime_scope():
    observed = {}
    accounts = CONTEXT["accounts"]
    with connect() as connection:
        observed["identity"] = list(
            connection.execute("SELECT current_user, session_user").fetchone()
        )
        for workspace_key, workspace_id in CONTEXT["workspaces"].items():
            for account_key, bot_id in CONTEXT["bots"].items():
                with connection.transaction():
                    scope(connection, workspace_id, bot_id=bot_id)
                    observed[f"{workspace_key}:{account_key}"] = account_keys(
                        connection, "SELECT trading_account_id FROM paper_account_seed"
                    )
        with connection.transaction():
            # Bot A cố đọc seed của account B bằng điều kiện tường minh: RLS vẫn trả 0 hàng.
            scope(connection, CONTEXT["workspaces"]["qa"], bot_id=CONTEXT["bots"]["A"])
            observed["bot_a_reads_account_b"] = connection.execute(
                "SELECT count(*) FROM paper_account_seed WHERE trading_account_id = %s",
                (UUID(accounts["B"]["id"]),),
            ).fetchone()[0]
            observed["bot_a_sees_qa_bots"] = len(
                connection.execute("SELECT id FROM bot_instance").fetchall()
            )
    return observed


def mfa_references(connection):
    """Reference TOTP hiện tại của factor tất định từng thành viên."""
    references = {}
    for member_key, member in CONTEXT["members"].items():
        references[member_key] = connection.execute(
            "SELECT secret_reference FROM mfa_factor WHERE id = %s",
            (UUID(member["mfa_factor_id"]),),
        ).fetchone()[0]
    return references


def bot_config_heads(connection):
    """Version PUBLISHED mới nhất (version, overlay hash, parent) của bot A/B."""
    heads = {}
    for key in ("A", "B"):
        row = connection.execute(
            "SELECT id::text, version, overlay_hash, parent_version_id::text "
            "FROM bot_config_version WHERE bot_id = %s AND status = 'PUBLISHED' "
            "ORDER BY version DESC LIMIT 1",
            (UUID(CONTEXT["bots"][key]),),
        ).fetchone()
        heads[key] = list(row) if row else None
    return heads


def publish_drift_config(connection, workspace, bot_id, member):
    """QA publish tay config bot B với release khác baseline (DRAFT → PUBLISHED như MB-02)."""
    from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash

    head = connection.execute(
        "SELECT id, version, account_id, base_settings_version_id, base_settings_hash "
        "FROM bot_config_version WHERE bot_id = %s AND status = 'PUBLISHED' "
        "ORDER BY version DESC LIMIT 1",
        (bot_id,),
    ).fetchone()
    overlay = canonical_overlay(
        BotConfigOverlay.model_validate(
            {"schema_version": 1, "strategy": {"release_id": "h03-v1", "parameters": {}}}
        )
    )
    drift_id = connection.execute(
        "INSERT INTO bot_config_version (id, workspace_id, bot_id, account_id, version, "
        "parent_version_id, schema_version, overlay_json, overlay_hash, "
        "base_settings_version_id, base_settings_hash, status, revision, created_by, updated_by) "
        "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, 'DRAFT', 1, %s, %s) "
        "RETURNING id",
        (
            workspace,
            bot_id,
            head[2],
            head[1] + 1,
            head[0],
            Jsonb(overlay),
            overlay_hash(overlay),
            head[3],
            head[4],
            member,
            member,
        ),
    ).fetchone()[0]
    connection.execute(
        "UPDATE bot_config_version SET status = 'PUBLISHED', approved_by = %s, "
        "published_at = now(), effective_at = now() WHERE id = %s",
        (member, drift_id),
    )
    return str(drift_id)


def mutate():
    """Mô phỏng thao tác thủ công của QA làm lệch baseline; seed lại phải khôi phục."""
    workspace = UUID(CONTEXT["workspaces"]["qa"])
    members = {key: UUID(member["membership_id"]) for key, member in CONTEXT["members"].items()}
    bots = {key: UUID(value) for key, value in CONTEXT["bots"].items()}
    with connect() as connection, connection.transaction():
        before = mfa_references(connection)
        drift_config = publish_drift_config(connection, workspace, bots["B"], members["qa.owner"])
        revoked = connection.execute(
            "UPDATE bot_grant SET revoked_at = now() WHERE workspace_id = %s AND bot_id = %s "
            "AND membership_id = %s AND permission = 'bot:runtime:pause' AND revoked_at IS NULL",
            (workspace, bots["A"], members["qa.operator"]),
        ).rowcount
        connection.execute(
            "INSERT INTO bot_grant (id, workspace_id, bot_id, membership_id, permission, "
            "granted_by) VALUES (gen_random_uuid(), %s, %s, %s, 'bot:read', %s)",
            (workspace, bots["B"], members["qa.operator"], members["qa.owner"]),
        )
        connection.execute(
            "UPDATE user_membership SET status = 'SUSPENDED' WHERE id = %s",
            (members["qa.analyst_reviewer"],),
        )
        connection.execute(
            "INSERT INTO role_binding (id, workspace_id, membership_id, role) "
            "VALUES (gen_random_uuid(), %s, %s, 'risk_admin')",
            (workspace, members["qa.operator"]),
        )
        connection.execute(
            "UPDATE trading_account SET status = 'DISABLED', name = 'qa-paper-a-mutated' "
            "WHERE id = %s",
            (UUID(CONTEXT["accounts"]["A"]["id"]),),
        )
        # Factor lạ trỏ CÙNG tệp TOTP đang dùng: xóa factor lạ không được xóa tệp còn sống.
        connection.execute(
            "INSERT INTO mfa_factor (id, workspace_id, membership_id, factor_type, "
            "secret_reference, verified_at) VALUES (gen_random_uuid(), %s, %s, 'TOTP', %s, now())",
            (workspace, members["qa.strategy_admin"], before["qa.strategy_admin"]),
        )
        bot_a = connection.execute(
            "SELECT status, name FROM bot_instance WHERE id = %s", (bots["A"],)
        ).fetchone()
        heads = bot_config_heads(connection)
    return {
        "references": before,
        "revoked": revoked,
        "bot_a": list(bot_a),
        "drift_config": drift_config,
        "bot_config_heads": heads,
    }


def verify_reset():
    accounts = {UUID(account["id"]): key for key, account in CONTEXT["accounts"].items()}
    bot_accounts = {UUID(value): key for key, value in CONTEXT["bots"].items()}
    members = CONTEXT["members"]
    with connect() as connection:
        account_a = connection.execute(
            "SELECT status, name FROM trading_account WHERE id = %s",
            (UUID(CONTEXT["accounts"]["A"]["id"]),),
        ).fetchone()
        bot_a = connection.execute(
            "SELECT status, name, trading_account_id FROM bot_instance WHERE id = %s",
            (UUID(CONTEXT["bots"]["A"]),),
        ).fetchone()
        membership_status = {
            key: connection.execute(
                "SELECT status FROM user_membership WHERE id = %s",
                (UUID(member["membership_id"]),),
            ).fetchone()[0]
            for key, member in members.items()
        }
        operator = UUID(members["qa.operator"]["membership_id"])
        operator_roles = sorted(
            row[0]
            for row in connection.execute(
                "SELECT role FROM role_binding WHERE membership_id = %s", (operator,)
            ).fetchall()
        )
        strategy_factors = [
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM mfa_factor WHERE membership_id = %s",
                (UUID(members["qa.strategy_admin"]["membership_id"]),),
            ).fetchall()
        ]
        grants = [
            [str(row[0]), bot_accounts.get(row[1], "FOREIGN"), row[2], row[3]]
            for row in connection.execute(
                "SELECT id, bot_id, permission, revoked_at IS NULL FROM bot_grant "
                "WHERE membership_id = %s ORDER BY id",
                (operator,),
            ).fetchall()
        ]
        risk_admin = UUID(members["qa.risk_admin"]["secret_reference_id"])
        risk_admin_key = connection.execute(
            "SELECT reference_key FROM secret_reference WHERE id = %s", (risk_admin,)
        ).fetchone()[0]
        references = mfa_references(connection)
        counters = {
            key: connection.execute(
                "SELECT last_used_counter FROM mfa_factor WHERE id = %s",
                (UUID(member["mfa_factor_id"]),),
            ).fetchone()[0]
            for key, member in members.items()
        }
        heads = bot_config_heads(connection)
    return {
        "bot_config_heads": heads,
        "account_a": list(account_a),
        "bot_a": [bot_a[0], bot_a[1], accounts[bot_a[2]]],
        "membership_status": membership_status,
        "operator_roles": operator_roles,
        "strategy_factors": strategy_factors,
        "operator_grants": grants,
        "risk_admin_reference_key": risk_admin_key,
        "references": references,
        "counters": counters,
    }


def feature_snapshot():
    """Feature snapshot test-only (không thuộc dataset) làm nguồn cho shared evidence QA."""
    from trading_bot.domain.market.feature_snapshot import (
        DataFreshness,
        FeatureSnapshot,
        MarketRegime,
        RegimeAssessment,
    )

    workspace = UUID(CONTEXT["workspaces"]["qa"])
    provisional = FeatureSnapshot(
        snapshot_id="0" * 64,
        content_hash="0" * 64,
        workspace_id=workspace,
        symbol="BTCUSDT",
        timeframe="1h",
        as_of=MARKET_END - timedelta(minutes=2),
        computed_at=MARKET_END - timedelta(seconds=90),
        schema_hash="a" * 64,
        formula_hash="b" * 64,
        code_hash="c" * 64,
        input_bar_ids=("qa-bar-btcusdt-1h-last",),
        input_event_ids=("qa-event",),
        features={"atr_14": 2.0},
        warmup={"atr_14": True},
        quality_flags=(),
        data_freshness=DataFreshness.FRESH,
        data_coverage=1.0,
        regime=RegimeAssessment(MarketRegime.TREND, 0.8, 1.0, "QA_FIXTURE", "Dữ liệu QA E2E."),
        is_usable=True,
    )
    digest = hashlib.sha256(provisional.canonical_json().encode()).hexdigest()
    snapshot = replace(provisional, snapshot_id=digest, content_hash=digest)
    payload = snapshot.canonical_payload()
    values = {
        "id": uuid5(NAMESPACE_URL, "crypidian-qa-e2e:feature-snapshot:" + digest),
        "workspace_id": workspace,
        "snapshot_id": snapshot.snapshot_id,
        "content_hash": snapshot.content_hash,
        "symbol": snapshot.symbol,
        "timeframe": snapshot.timeframe,
        "as_of": snapshot.as_of,
        "computed_at": snapshot.computed_at,
        "schema_hash": snapshot.schema_hash,
        "formula_hash": snapshot.formula_hash,
        "code_hash": snapshot.code_hash,
        "input_bar_ids": Jsonb(list(snapshot.input_bar_ids)),
        "input_event_ids": Jsonb(list(snapshot.input_event_ids)),
        "features": Jsonb(dict(snapshot.features)),
        "warmup": Jsonb(dict(snapshot.warmup)),
        "quality_flags": Jsonb(list(snapshot.quality_flags)),
        "data_freshness": snapshot.data_freshness.value,
        "data_coverage": snapshot.data_coverage,
        "regime": Jsonb(payload["regime"]),
        "is_usable": True,
        "canonical_json": snapshot.canonical_json(),
    }
    with connect() as connection, connection.transaction():
        connection.execute(
            sql.SQL("INSERT INTO feature_snapshot ({}) VALUES ({})").format(
                sql.SQL(",").join(map(sql.Identifier, values)),
                sql.SQL(",").join(sql.Placeholder() for _ in values),
            ),
            tuple(values.values()),
        )
    return {"feature_snapshot_id": snapshot.snapshot_id}


async def fanout_async():
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.repositories.bot_decisions import (
        PostgresSignalDecisionStore,
    )
    from trading_bot.adapters.database.repositories.feature_snapshots import (
        FeatureSnapshotRepository,
    )
    from trading_bot.application.bot_decision_contexts import (
        EffectiveBotDecisionContextProvider,
        FanOutExclusionRecorder,
        RuntimeFanOutGate,
    )
    from trading_bot.application.bot_dispatcher import BotEvidenceDispatcher
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.market.signal_evidence import (
        EvidenceSourceRef,
        build_shared_signal_evidence,
    )
    from trading_bot.domain.strategy.contracts import DecisionSnapshot
    from trading_bot.domain.strategy.registry import StrategyRegistry

    class EmptySnapshots:
        """Snapshot point-in-time rỗng: strategy tự abstain, chỉ chứng minh fan-out/lineage."""

        async def snapshot_for(self, *, evidence, context, evaluated_at):
            return DecisionSnapshot(
                symbol=evidence.symbol,
                decision_time=evaluated_at,
                snapshot_id=f"qa-empty-{context.bot_id}",
                series=(),
                features=(),
            )

    workspace = UUID(CONTEXT["workspaces"]["qa"])
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        provider = EffectiveBotDecisionContextProvider(
            sessions,
            runtime_gate=RuntimeFanOutGate(),
            exclusion_recorder=FanOutExclusionRecorder(
                sessions=sessions, workspace_id=workspace, accounts=lambda _account: None
            ),
        )
        eligibility = await provider.eligibility(workspace_id=workspace, as_of=FANOUT_AS_OF)
        async with UnitOfWork(sessions, workspace_id=workspace) as uow:
            feature = await FeatureSnapshotRepository(uow.session).get_as_of(
                workspace_id=workspace, symbol="BTCUSDT", timeframe="1h", as_of=MARKET_END
            )
        evidence = build_shared_signal_evidence(
            feature_snapshot=feature,
            setup_type="qa_trend_breakout",
            observed_at=MARKET_END - timedelta(minutes=1),
            available_at=MARKET_END,
            validity_end=MARKET_END + timedelta(hours=1),
            sources=(EvidenceSourceRef("market_bar:qa:BTCUSDT:1h:last", MARKET_END),),
        )
        dispatcher = BotEvidenceDispatcher(
            PostgresSignalDecisionStore(sessions),
            strategies=StrategyRegistry(),
            snapshots=EmptySnapshots(),
        )
        decisions = await dispatcher.dispatch_evidence(evidence, eligibility.contexts, FANOUT_AS_OF)
        retry = await dispatcher.dispatch_evidence(evidence, eligibility.contexts, FANOUT_AS_OF)
    finally:
        await engine.dispose()
    bot_keys = {value: key for key, value in CONTEXT["bots"].items()}
    return {
        "identity": "crypidian_trading_runtime"
        if os.environ["TRADING_BOT_DATABASE_USER"] == "crypidian_trading_runtime"
        else os.environ["TRADING_BOT_DATABASE_USER"],
        "contexts": {
            bot_keys[str(item.bot_id)]: {
                "release": item.strategy_release_id,
                "bot_config_version_id": str(item.bot_config_version_id),
                "root_settings_version_id": str(item.root_settings_version_id),
            }
            for item in eligibility.contexts
        },
        "exclusions": [
            [bot_keys.get(str(item.bot_id), "FOREIGN"), item.reason_code]
            for item in eligibility.exclusions
        ],
        "decisions": {
            bot_keys[str(item.bot_id)]: {
                "release": item.strategy_release_id,
                "status": item.status.value,
                "bot_config_version_id": str(item.bot_config_version_id),
                "root_settings_version_id": str(item.root_settings_version_id),
                "evidence_id": str(item.evidence_id),
            }
            for item in decisions
        },
        "retry_identical": list(retry) == list(decisions),
    }


def fanout():
    return asyncio.run(fanout_async())


def mfa_files():
    """Tồn tại tệp TOTP đã mã hóa theo reference (chạy trong service có volume MFA)."""
    directory = Path(os.environ["TRADING_BOT_MFA_SECRET_DIRECTORY"])
    result = {}
    for label, reference in CONTEXT["mfa_files"].items():
        _, _, identifier = reference.partition(":")
        result[label] = (directory / f"{UUID(identifier).hex}.bin").is_file()
    return result


PHASES = {
    "snapshot": snapshot,
    "admin_matrix": admin_matrix,
    "runtime_scope": runtime_scope,
    "mutate": mutate,
    "verify_reset": verify_reset,
    "mfa_files": mfa_files,
    "feature_snapshot": feature_snapshot,
    "fanout": fanout,
}
print(json.dumps(PHASES[PHASE](), sort_keys=True))
