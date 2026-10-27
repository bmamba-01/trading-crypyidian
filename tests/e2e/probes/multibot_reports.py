"""Probe E2E MB-06 (báo cáo theo bot); chạy trong container Compose với login DB thật.

Phase:
- `prepare` (migrator): feature snapshot test-only + lineage QA (settings, release, config bot).
- `ledger` (trading-runtime, `crypidian_trading_runtime`): tạo trade cho bot A/B qua
  `PaperLedgerRepository` thật (intent → entry fill → funding → exit fill → close). Tiền đề
  plan/decision/reservation ghi bằng ORM dưới RLS runtime đúng bot.
- `oracle` (migrator): tổng ledger độc lập bằng SQL thô theo bot + digest bảng ledger để chứng
  minh báo cáo không ghi/sửa hàng nào.
- `rls` (admin-web, `crypidian_admin_web`): workspace khác/membership giả không thấy trade.
Ngữ cảnh đến từ test qua stdin (không chứa bí mật).
"""

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

PHASE = os.environ["MB06_PHASE"]
STATE = json.loads(os.environ["MB06_STATE"])
LEDGER_TABLES = ("order_intent", "exchange_order", "fill", "position_event", "trade", "trade_cost")


def connect():
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    return psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        autocommit=True,
    )


def sessions_factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        database_url_from_secret,
    )

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def prepare():
    from trading_bot.domain.market.feature_snapshot import (
        DataFreshness,
        FeatureSnapshot,
        MarketRegime,
        RegimeAssessment,
    )

    workspace = UUID(STATE["workspaces"]["qa"])
    as_of = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    provisional = FeatureSnapshot(
        snapshot_id="0" * 64,
        content_hash="0" * 64,
        workspace_id=workspace,
        symbol="BTCUSDT",
        timeframe="1h",
        as_of=as_of,
        computed_at=as_of + timedelta(seconds=30),
        schema_hash="a" * 64,
        formula_hash="b" * 64,
        code_hash="c" * 64,
        input_bar_ids=("mb06-bar",),
        input_event_ids=("mb06-event",),
        features={"atr_14": 2.0},
        warmup={"atr_14": True},
        quality_flags=(),
        data_freshness=DataFreshness.FRESH,
        data_coverage=1.0,
        regime=RegimeAssessment(MarketRegime.TREND, 0.8, 1.0, "MB06_FIXTURE", "Dữ liệu E2E."),
        is_usable=True,
    )
    digest = hashlib.sha256(provisional.canonical_json().encode()).hexdigest()
    snapshot = replace(provisional, snapshot_id=digest, content_hash=digest)
    payload = snapshot.canonical_payload()
    values = {
        "id": uuid5(NAMESPACE_URL, "crypidian-mb06-e2e:feature-snapshot:" + digest),
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
    with connect() as connection:
        with connection.transaction():
            connection.execute(
                sql.SQL("INSERT INTO feature_snapshot ({}) VALUES ({})").format(
                    sql.SQL(",").join(map(sql.Identifier, values)),
                    sql.SQL(",").join(sql.Placeholder() for _ in values),
                ),
                tuple(values.values()),
            )
        settings_id, settings_hash = connection.execute(
            "SELECT id, settings_hash FROM workspace_settings_version "
            "WHERE workspace_id = %s AND status = 'PUBLISHED' ORDER BY version DESC LIMIT 1",
            (workspace,),
        ).fetchone()
        releases = {
            name: {"id": str(release_id), "content_hash": content_hash}
            for release_id, name, content_hash in connection.execute(
                "SELECT id, name, content_hash FROM strategy_release "
                "WHERE workspace_id = %s AND status = 'PUBLISHED'",
                (workspace,),
            ).fetchall()
        }
        configs = {}
        for bot_id, config_id, overlay_hash in connection.execute(
            "SELECT bot_id, id, overlay_hash FROM bot_config_version "
            "WHERE workspace_id = %s AND status = 'PUBLISHED' ORDER BY version DESC",
            (workspace,),
        ).fetchall():
            configs.setdefault(str(bot_id), {"id": str(config_id), "hash": overlay_hash})
    return {
        "feature_snapshot_id": snapshot.snapshot_id,
        "settings": {"id": str(settings_id), "hash": settings_hash},
        "releases": releases,
        "configs": configs,
    }


def _hex(label):
    return hashlib.sha256(label.encode()).hexdigest()


async def ledger_async():
    from sqlalchemy import select

    from trading_bot.adapters.database.models.paper_entities import TradeRecord
    from trading_bot.adapters.database.models.risk_entities import (
        RiskDecisionRecord,
        RiskReservationRecord,
        TradePlanRecord,
    )
    from trading_bot.adapters.database.repositories.paper_ledger import PaperLedgerRepository
    from trading_bot.application.outbox import UnitOfWork

    workspace = UUID(STATE["workspaces"]["qa"])
    prepared = STATE["prepared"]
    base = datetime.now(UTC).replace(microsecond=0)
    repository = PaperLedgerRepository()
    engine, sessions = sessions_factory()
    created = {}
    try:
        for spec in STATE["trades"]:
            label = spec["label"]
            account = UUID(STATE["accounts"][spec["account"]])
            release = prepared["releases"][spec["release"]]
            bot_id = STATE["bots"][spec["account"]]
            config = prepared["configs"][bot_id]
            quantity = Decimal(spec["quantity"])
            plan_hash = _hex(f"mb06-plan:{label}")
            candidate = _hex(f"mb06-candidate:{label}")
            expires = base + timedelta(hours=2)
            async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                await uow.bind_account(account)
                decision_id, reservation_id = uuid4(), uuid4()
                uow.session.add(
                    TradePlanRecord(
                        id=uuid4(),
                        bot_id=uow.bot_id,
                        workspace_id=workspace,
                        account_id=account,
                        plan_hash=plan_hash,
                        symbol="BTCUSDT",
                        notional=quantity * Decimal(spec["entry_price"]),
                        payload={
                            "strategy_release_id": spec["release"],
                            "quantity": str(quantity),
                            "direction": "long",
                            "settings_version_id": prepared["settings"]["id"],
                            "release_content_hash": release["content_hash"],
                            "candidate_id": candidate,
                        },
                    )
                )
                await uow.session.flush()
                uow.session.add(
                    RiskDecisionRecord(
                        id=decision_id,
                        bot_id=uow.bot_id,
                        workspace_id=workspace,
                        account_id=account,
                        decision_key=_hex(f"mb06-decision:{label}"),
                        plan_hash=plan_hash,
                        snapshot_id=f"mb06-{label}",
                        approved=True,
                        reason_code="APPROVED",
                        detail_vi="Duyệt cho E2E báo cáo MB-06.",
                        payload={"candidate_expires_at": expires.isoformat()},
                    )
                )
                await uow.session.flush()
                uow.session.add(
                    RiskReservationRecord(
                        id=reservation_id,
                        bot_id=uow.bot_id,
                        workspace_id=workspace,
                        account_id=account,
                        risk_decision_id=decision_id,
                        idempotency_key=f"mb06-reservation:{label}",
                        plan_hash=plan_hash,
                        symbol="BTCUSDT",
                        reserved_risk=Decimal("10"),
                        reserved_notional=quantity * Decimal(spec["entry_price"]),
                        reserved_initial_margin=Decimal("20"),
                        expires_at=expires,
                        status="RESERVED",
                    )
                )
                await uow.session.flush()
                intent = await repository.create_intent(
                    uow,
                    account_id=account,
                    idempotency_key=f"mb06-intent:{label}",
                    plan_hash=plan_hash,
                    settings_version_id=UUID(prepared["settings"]["id"]),
                    strategy_release_id=UUID(release["id"]),
                    strategy_release_hash=release["content_hash"],
                    feature_snapshot_id=prepared["feature_snapshot_id"],
                    candidate_id=candidate,
                    risk_decision_id=decision_id,
                    risk_reservation_id=reservation_id,
                    symbol="BTCUSDT",
                    side="LONG",
                    quote_asset="USDT",
                    quantity=quantity,
                    created_at=base,
                    evidence={
                        "settings_hash": prepared["settings"]["hash"],
                        "bot_config_version_id": config["id"],
                        "bot_config_hash": config["hash"],
                        "effective_config_hash": _hex(f"mb06-effective:{spec['account']}"),
                        "paper_execution_profile_hash": "e" * 64,
                    },
                )
                opened_at = base + timedelta(seconds=spec["opened_offset"])
                await repository.append_fill(
                    uow,
                    intent_id=intent.intent_id,
                    fill_key=f"mb06-entry:{label}",
                    bar_key=f"mb06-bar-entry:{label}",
                    fill_policy_version="paper-v1",
                    quantity=quantity,
                    price=Decimal(spec["entry_price"]),
                    fee_amount=Decimal(spec["entry_fee"]),
                    slippage_amount=Decimal(spec["entry_slippage"]),
                    occurred_at=opened_at,
                )
                trade = await uow.session.scalar(
                    select(TradeRecord).where(
                        TradeRecord.workspace_id == workspace,
                        TradeRecord.account_id == account,
                        TradeRecord.intent_id == intent.intent_id,
                    )
                )
                if spec.get("funding"):
                    await repository.append_trade_cost(
                        uow,
                        trade_id=trade.id,
                        cost_key=f"mb06-funding:{label}",
                        cost_type="FUNDING",
                        amount=Decimal(spec["funding"]),
                        currency="USDT",
                        source_event_id=f"mb06-funding-event:{label}",
                        occurred_at=opened_at + timedelta(seconds=1),
                    )
                closed_at = None
                if spec.get("closed_offset") is not None:
                    closed_at = base + timedelta(seconds=spec["closed_offset"])
                    await repository.append_exit_fill(
                        uow,
                        intent_id=intent.intent_id,
                        fill_key=f"mb06-exit:{label}",
                        bar_key=f"mb06-bar-exit:{label}",
                        fill_policy_version="paper-v1",
                        quantity=quantity,
                        price=Decimal(spec["exit_price"]),
                        fee_amount=Decimal(spec["exit_fee"]),
                        slippage_amount=Decimal(spec["exit_slippage"]),
                        occurred_at=closed_at,
                    )
                    await repository.close_trade(
                        uow,
                        trade_id=trade.id,
                        gross_pnl=Decimal(spec["gross"]),
                        closed_at=closed_at,
                    )
                created[label] = {
                    "trade_id": str(trade.id),
                    "bot_id": str(uow.bot_id),
                    "closed_at": closed_at.isoformat() if closed_at else None,
                    "net_pnl": str(trade.net_pnl),
                }
    finally:
        await engine.dispose()
    return {
        "identity": os.environ["TRADING_BOT_DATABASE_USER"],
        "base": base.isoformat(),
        "trades": created,
    }


def ledger():
    return asyncio.run(ledger_async())


def oracle():
    workspace = UUID(STATE["workspaces"]["qa"])
    start = datetime.fromisoformat(STATE["window"]["start"])
    end = datetime.fromisoformat(STATE["window"]["end"])
    with connect() as connection:
        per_bot = {
            str(bot_id): {
                "count": count,
                "net_pnl": str(net),
                "gross_pnl": str(gross),
                "total_cost": str(cost),
                "fee_total": str(fee),
                "funding_total": str(funding),
                "slippage_total": str(slippage),
            }
            for bot_id, count, net, gross, cost, fee, funding, slippage in connection.execute(
                "SELECT bot_id, count(*), sum(net_pnl), sum(gross_pnl), sum(total_cost), "
                "sum(fee_total), sum(funding_total), sum(slippage_total) FROM trade "
                "WHERE workspace_id = %s AND status = 'CLOSED' AND closed_at >= %s "
                "AND closed_at < %s GROUP BY bot_id",
                (workspace, start, end),
            ).fetchall()
        }
        workspace_total = connection.execute(
            "SELECT count(*), coalesce(sum(net_pnl), 0) FROM trade WHERE workspace_id = %s "
            "AND status = 'CLOSED' AND closed_at >= %s AND closed_at < %s",
            (workspace, start, end),
        ).fetchone()
        digests = {}
        for table in LEDGER_TABLES:
            digests[table] = list(
                connection.execute(
                    sql.SQL(
                        "SELECT count(*), md5(COALESCE(string_agg(to_jsonb(t)::text, '|' "
                        "ORDER BY t.id), '')) FROM {} t"
                    ).format(sql.Identifier(table))
                ).fetchone()
            )
    return {
        "per_bot": per_bot,
        "workspace": {"count": workspace_total[0], "net_pnl": str(workspace_total[1])},
        "digests": digests,
    }


def _scope(connection, workspace_id, membership_id):
    connection.execute(
        "SELECT set_config('app.workspace_id', %s, true), "
        "set_config('app.membership_id', %s, true), set_config('app.bot_id', '', true)",
        (str(workspace_id), str(membership_id)),
    )


async def _repository_view(workspace_id, membership_id, bot_id, account_id, start, end):
    from trading_bot.adapters.database.repositories.bot_reports import BotReportRepository
    from trading_bot.application.outbox import UnitOfWork

    engine, sessions = sessions_factory()
    try:
        async with UnitOfWork(
            sessions, workspace_id=workspace_id, actor_membership_id=membership_id
        ) as uow:
            repository = BotReportRepository(uow.session)
            await repository.set_read_only()
            snapshot = await repository.load_bot_ledger(
                workspace_id=workspace_id,
                bot_id=bot_id,
                account_id=account_id,
                start=start,
                end=end,
                max_closed=100,
            )
            totals = await repository.workspace_closed_totals(
                workspace_id=workspace_id, start=start, end=end
            )
    finally:
        await engine.dispose()
    return {
        "closed": len(snapshot.closed_trades),
        "open": len(snapshot.open_trades),
        "totals": {asset: [count, str(net)] for asset, (count, net) in totals.items()},
    }


def rls():
    qa = UUID(STATE["workspaces"]["qa"])
    other = UUID(STATE["workspaces"]["qa_other"])
    members = STATE["members"]
    bot_a = UUID(STATE["bots"]["A"])
    account_a = UUID(STATE["accounts"]["A"])
    start = datetime.fromisoformat(STATE["window"]["start"])
    end = datetime.fromisoformat(STATE["window"]["end"])
    observed = {}
    with connect() as connection:
        observed["identity"] = connection.execute("SELECT current_user").fetchone()[0]
        cases = {
            "other_owner@qa_other": (other, members["qa_other.owner"]),
            "other_owner@qa": (qa, members["qa_other.owner"]),
            "qa_owner@qa": (qa, members["qa.owner"]),
            "qa_analyst@qa": (qa, members["qa.analyst_reviewer"]),
            "qa_operator@qa": (qa, members["qa.operator"]),
        }
        for name, (workspace_id, membership_id) in cases.items():
            with connection.transaction():
                _scope(connection, workspace_id, membership_id)
                rows = connection.execute(
                    "SELECT bot_id::text, count(*) FROM trade GROUP BY bot_id"
                ).fetchall()
                cost_rows = connection.execute("SELECT count(*) FROM trade_cost").fetchone()[0]
            observed[name] = {"trades": dict(rows), "costs": cost_rows}
    # Repository báo cáo thật dưới membership W2 nhưng workspace giả = W1: RLS trả rỗng.
    observed["repository_forged"] = asyncio.run(
        _repository_view(qa, UUID(members["qa_other.owner"]), bot_a, account_a, start, end)
    )
    observed["repository_owner"] = asyncio.run(
        _repository_view(qa, UUID(members["qa.owner"]), bot_a, account_a, start, end)
    )
    return observed


PHASES = {"prepare": prepare, "ledger": ledger, "oracle": oracle, "rls": rls}
print(json.dumps(PHASES[PHASE](), sort_keys=True))
