"""Tạo đợt thử 10 bot PAPER ngẫu nhiên trên 20 coin Binance USDⓈ-M (local/PAPER, 15/11/2026).

Owner yêu cầu: bốc ngẫu nhiên 20 coin trong danh sách Binance USDⓈ-M perpetual để theo dõi, gửi
tín hiệu qua Telegram khi khớp chiến lược; 10 bot chạy tham số ngẫu nhiên, số coin theo dõi khác
nhau và khung nến khác nhau (H03 v3 15m/1h/4h, H01 1h, Strategy Card 1h).

Ba bước, đều idempotent theo ``--seed``:

1. ``plan`` (máy dev): đọc dữ liệu **công khai** ``/fapi/v1/exchangeInfo`` và
   ``/fapi/v1/ticker/24hr`` (không API key, không đặt lệnh), lọc cặp USDT perpetual đang TRADING
   có quote volume 24h ≥ ``--min-quote-volume``, bốc 20 coin bằng ``random.Random(seed)`` và dựng
   tham số 10 bot. Kế hoạch ghi ra JSON để lặp lại/kiểm tra.
2. ``apply`` (container ``migrator``, role admin): root settings v2 (20 coin, khung 15m/1h/4h),
   strategy release cần dùng, 10 trading account → bot (trigger), seed vốn 10.000 USDT, bot config
   đã publish, subscription Telegram của chat owner đang liên kết; tắt các bot cũ để chỉ còn 10.
3. ``genesis`` (container ``trading-runtime``): checkpoint PAPER khởi tạo cho từng account mới.

Chỉ chạy với project Compose local; từ chối workspace không ở chế độ PAPER. Không in secret.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any

# Trong container, mã được exec từ stdin nên không có `__file__`.
ROOT = Path(globals().get("__file__", "/app/x/x")).resolve().parents[1]
BINANCE_FAPI = "https://fapi.binance.com"
DEFAULT_WORKSPACE = "c486b52b-912a-446f-aeed-99685c06cad6"
# 1m là khung execution: lệnh có hiệu lực đúng một nến chiến lược và chỉ khớp ở nến mở sau thời
# điểm submit, nên thiếu nến mịn hơn khung chiến lược thì không lệnh nào khớp được (sự cố 16/11).
TIMEFRAMES = ("1m", "15m", "1h", "4h")
BOT_COUNT = 10
# Collector và bot config chỉ nhận mã ASCII viết hoa (market_bar_collector.py, bot_settings.py);
# Binance có niêm yết mã ký tự không phải Latin nên phải lọc trước khi bốc.
_ASCII_SYMBOL = re.compile(r"^[A-Z0-9]{3,32}$")
COIN_COUNT = 20
# Phân bổ release cho 10 bot: ưu tiên H03 v3 để so khung 15m/1h/4h; H01 và Strategy Card làm
# đối chứng 1h. H06 không dùng vì runtime chưa có dữ liệu lịch vĩ mô (luôn abstain).
RELEASE_POOL = (
    "h03-v3-15m",
    "h03-v3-15m",
    "h03-v3-15m",
    "h03-v3-1h",
    "h03-v3-1h",
    "h03-v3-4h",
    "h03-v3-4h",
    "h01-v1",
    "h01-v1",
    "sc-binance-usdm-vol-breakout-001-v1.0.0",
)
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(SIGTEST_PHASE=p['phase'],SIGTEST_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'seed_signal_test_bots.py','exec'),{'__name__':'__container__'})"
)


# ---------------------------------------------------------------------------
# Máy dev: chọn coin và dựng kế hoạch
# ---------------------------------------------------------------------------


def _get_json(path: str) -> Any:
    request = urllib.request.Request(
        BINANCE_FAPI + path, headers={"User-Agent": "crypidian-local-paper-test"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def eligible_symbols(
    exchange_info: dict[str, Any], tickers: list[dict[str, Any]], *, min_quote_volume: Decimal
) -> list[str]:
    """Cặp USDT perpetual đang TRADING có quote volume 24h đủ lớn, sắp xếp ổn định theo tên."""
    volume = {item["symbol"]: Decimal(str(item.get("quoteVolume", "0"))) for item in tickers}
    symbols = [
        item["symbol"]
        for item in exchange_info.get("symbols", [])
        if item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
        and item.get("status") == "TRADING"
        and volume.get(item["symbol"], Decimal("0")) >= min_quote_volume
        and _ASCII_SYMBOL.fullmatch(item["symbol"])
    ]
    return sorted(set(symbols))


def build_plan(symbols: list[str], *, seed: int) -> dict[str, Any]:
    """Bốc 20 coin và tham số 10 bot bằng RNG có seed (tái lập được)."""
    if len(symbols) < COIN_COUNT:
        raise SystemExit(f"Chỉ có {len(symbols)} coin đủ điều kiện, cần {COIN_COUNT}.")
    rng = random.Random(seed)
    coins = sorted(rng.sample(symbols, COIN_COUNT))
    releases = list(RELEASE_POOL)
    rng.shuffle(releases)
    bots: list[dict[str, Any]] = []
    for index, release in enumerate(releases, start=1):
        count = rng.randint(3, COIN_COUNT)
        universe = sorted(rng.sample(coins, count))
        parameters: dict[str, int] = {}
        if release.startswith("h03-v3"):
            parameters = {
                "donchian_lookback": rng.choice((20, 30, 40, 55, 70, 90)),
                "volume_median_lookback": rng.choice((10, 15, 20, 30)),
            }
        risk = {
            "per_trade_risk_fraction": str(
                rng.choice(("0.002", "0.003", "0.005", "0.0075", "0.01"))
            ),
            "max_positions": rng.randint(1, 6),
            "max_leverage": str(rng.choice((2, 3, 4, 5))),
        }
        timeframe = {"h01-v1": "1h", "sc-binance-usdm-vol-breakout-001-v1.0.0": "1h"}.get(
            release, release.rsplit("-", 1)[-1]
        )
        short = {"h01-v1": "H01", "sc-binance-usdm-vol-breakout-001-v1.0.0": "CARD"}.get(
            release, "H03"
        )
        bots.append(
            {
                "key": f"T{index:02d}",
                "name": f"T{index:02d} {short} {timeframe} · {count} coin",
                "release_id": release,
                "timeframe": timeframe,
                "symbols": universe,
                "parameters": parameters,
                "risk": risk,
            }
        )
    return {"seed": seed, "coins": coins, "timeframes": list(TIMEFRAMES), "bots": bots}


def collector_streams(plan: dict[str, Any]) -> str:
    """Giá trị ``TRADING_BOT_MARKET_COLLECTOR_STREAMS``: 20 coin × 1m/15m/1h/4h = 80 stream."""
    return ";".join(f"{coin}:{','.join(plan['timeframes'])}" for coin in plan["coins"])


def _run_phase(project: str, service: str, phase: str, state: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(
        {"phase": phase, "state": state, "code": Path(__file__).read_text(encoding="utf-8")}
    )
    command = [
        "docker", "compose", "-p", project, "-f", "compose.yaml",
        "run", "--rm", "--no-deps", "-T", service, "python", "-c", PROBE_BOOTSTRAP,
    ]  # fmt: skip
    result = subprocess.run(
        command, cwd=ROOT, input=payload, capture_output=True, text=True, check=False
    )
    lines = result.stdout.strip().splitlines()
    if result.returncode != 0 or not lines:
        print(result.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"Bước {phase} thất bại (mã {result.returncode}).")
    return dict(json.loads(lines[-1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("plan", "apply"))
    parser.add_argument("--seed", type=int, default=20261115)
    parser.add_argument("--min-quote-volume", type=Decimal, default=Decimal("20000000"))
    parser.add_argument(
        "--plan-file", type=Path, default=ROOT / "docs/implementation/signal-test-plan.json"
    )
    parser.add_argument("--project", default="crypidian-review")
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE)
    args = parser.parse_args(argv)
    if "live" in args.project.lower() or "prod" in args.project.lower():
        raise SystemExit("Script chỉ dành cho stack local PAPER.")
    if args.command == "plan":
        symbols = eligible_symbols(
            _get_json("/fapi/v1/exchangeInfo"),
            _get_json("/fapi/v1/ticker/24hr"),
            min_quote_volume=args.min_quote_volume,
        )
        plan = build_plan(symbols, seed=args.seed)
        plan["eligible_count"] = len(symbols)
        plan["min_quote_volume"] = str(args.min_quote_volume)
        plan["collector_streams"] = collector_streams(plan)
        args.plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"coins": plan["coins"], "eligible": len(symbols)}, ensure_ascii=False))
        return 0
    plan = json.loads(args.plan_file.read_text(encoding="utf-8"))
    state = {"workspace_id": args.workspace_id, "plan": plan}
    applied = _run_phase(args.project, "migrator", "apply", state)
    state |= applied
    genesis = _run_phase(args.project, "trading-runtime", "genesis", state)
    print(json.dumps({**applied, **genesis}, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Trong container
# ---------------------------------------------------------------------------


def _connect() -> Any:
    import psycopg

    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    return psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
    )


def _insert(connection: Any, table: str, **values: Any) -> None:
    from psycopg import sql

    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join(sql.Placeholder() for _ in values),
        ),
        tuple(values.values()),
    )


def _container_apply(state: dict[str, Any]) -> dict[str, Any]:
    from uuid import NAMESPACE_URL, UUID, uuid5

    from psycopg.types.json import Jsonb

    from trading_bot.adapters.database.repositories.workspace_settings import (
        canonical_settings,
        settings_hash,
    )
    from trading_bot.application.strategy_catalog import strategy_registry
    from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
    from trading_bot.config.workspace_settings import WorkspaceSettings

    # Container migrator không đặt APP_ENV; chặn khi được đặt khác local/test (staging/prod).
    if os.environ.get("TRADING_BOT_APP_ENV", "local") not in {"local", "test"}:
        raise SystemExit("Chỉ chạy ở môi trường local/test.")
    workspace = UUID(state["workspace_id"])
    plan = state["plan"]

    def det(name: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"crypidian-signal-test:{workspace}:{plan['seed']}:{name}")

    registry = strategy_registry()
    with _connect() as connection, connection.transaction():
        connection.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace),))
        mode = connection.execute("SELECT mode FROM workspace WHERE id=%s", (workspace,)).fetchone()
        if mode is None or mode[0] != "PAPER":
            raise SystemExit("Workspace không tồn tại hoặc không ở chế độ PAPER.")
        current = connection.execute(
            "SELECT id, version, settings_json, created_by FROM workspace_settings_version "
            "WHERE workspace_id=%s AND status='PUBLISHED' ORDER BY version DESC LIMIT 1",
            (workspace,),
        ).fetchone()
        if current is None:
            raise SystemExit("Workspace chưa có root settings đã publish.")
        parent_id, parent_version, parent_json, owner = current
        # Mỗi bộ khung một phiên bản root; đổi khung (vd thêm 1m) tạo phiên bản mới.
        settings_id = det(f"root-settings:{','.join(plan['timeframes'])}")
        exists = connection.execute(
            "SELECT 1 FROM workspace_settings_version WHERE workspace_id=%s AND id=%s",
            (workspace, settings_id),
        ).fetchone()
        if exists is None:
            payload = dict(parent_json)
            payload["market_data"] = {
                **payload["market_data"],
                "symbols": plan["coins"],
                "timeframes": plan["timeframes"],
            }
            settings = WorkspaceSettings.model_validate(payload)
            canonical = canonical_settings(settings)
            _insert(
                connection,
                "workspace_settings_version",
                id=settings_id,
                workspace_id=workspace,
                version=parent_version + 1,
                parent_version_id=parent_id,
                schema_version=settings.schema_version,
                settings_json=Jsonb(canonical),
                settings_hash=settings_hash(canonical),
                status="PUBLISHED",
                revision=1,
                created_by=owner,
                approved_by=owner,
                published_at=connection.execute("SELECT clock_timestamp()").fetchone()[0],
                effective_at=connection.execute("SELECT clock_timestamp()").fetchone()[0],
            )
        root_hash = connection.execute(
            "SELECT settings_hash FROM workspace_settings_version WHERE workspace_id=%s AND id=%s",
            (workspace, settings_id),
        ).fetchone()[0]
        for release in sorted({bot["release_id"] for bot in plan["bots"]}):
            present = connection.execute(
                "SELECT 1 FROM strategy_release WHERE workspace_id=%s AND name=%s",
                (workspace, release),
            ).fetchone()
            if present is None:
                _insert(
                    connection,
                    "strategy_release",
                    id=det(f"release:{release}"),
                    workspace_id=workspace,
                    name=release,
                    version=1,
                    status="PUBLISHED",
                    recipe=Jsonb({}),
                    content_hash=registry.get_release(release).content_hash,
                )
        binding = connection.execute(
            "SELECT membership_id, telegram_binding_id, telegram_chat_id "
            "FROM telegram_signal_subscription WHERE workspace_id=%s ORDER BY created_at LIMIT 1",
            (workspace,),
        ).fetchone()
        new_accounts = [str(det(f"account:{bot['key']}")) for bot in plan["bots"]]
        # Tắt bot cũ (không thuộc đợt thử) để chỉ còn 10 bot chạy; đảo ngược được bằng UPDATE.
        disabled = connection.execute(
            "UPDATE trading_account SET status='DISABLED' WHERE workspace_id=%s "
            "AND status='ENABLED' AND NOT (id::text = ANY(%s)) RETURNING id",
            (workspace, new_accounts),
        ).fetchall()
        bots_out: list[dict[str, Any]] = []
        for bot in plan["bots"]:
            account = det(f"account:{bot['key']}")
            present = connection.execute(
                "SELECT 1 FROM trading_account WHERE workspace_id=%s AND id=%s",
                (workspace, account),
            ).fetchone()
            if present is None:
                _insert(
                    connection,
                    "trading_account",
                    id=account,
                    workspace_id=workspace,
                    name=bot["name"],
                    venue="binance_usdm",
                    mode="PAPER",
                    environment="local",
                    status="ENABLED",
                    max_notional=10000,
                )
            bot_id = connection.execute(
                "SELECT id FROM bot_instance WHERE workspace_id=%s AND trading_account_id=%s",
                (workspace, account),
            ).fetchone()[0]
            connection.execute(
                "UPDATE bot_instance SET name=%s WHERE workspace_id=%s AND id=%s",
                (bot["name"], workspace, bot_id),
            )
            if (
                connection.execute(
                    "SELECT 1 FROM paper_account_seed "
                    "WHERE workspace_id=%s AND trading_account_id=%s",
                    (workspace, account),
                ).fetchone()
                is None
            ):
                _insert(
                    connection,
                    "paper_account_seed",
                    id=det(f"seed:{bot['key']}"),
                    bot_id=bot_id,
                    workspace_id=workspace,
                    trading_account_id=account,
                    quote_asset="USDT",
                    initial_equity=Decimal("10000"),
                    source_settings_version_id=settings_id,
                    source_settings_hash=root_hash,
                    idempotency_key=f"paper-seed:{account}",
                )
            config_id = det(f"config:{bot['key']}:{settings_id}")
            if (
                connection.execute(
                    "SELECT 1 FROM bot_config_version WHERE workspace_id=%s AND id=%s",
                    (workspace, config_id),
                ).fetchone()
                is None
            ):
                overlay = canonical_overlay(
                    BotConfigOverlay.model_validate(
                        {
                            "schema_version": 1,
                            "strategy": {
                                "release_id": bot["release_id"],
                                "parameters": bot["parameters"],
                            },
                            "universe": {"symbols": bot["symbols"]},
                            "risk": bot["risk"],
                        }
                    )
                )
                _insert(
                    connection,
                    "bot_config_version",
                    id=config_id,
                    workspace_id=workspace,
                    bot_id=bot_id,
                    account_id=account,
                    version=connection.execute(
                        "SELECT coalesce(max(version), 0) + 1 FROM bot_config_version "
                        "WHERE workspace_id=%s AND bot_id=%s",
                        (workspace, bot_id),
                    ).fetchone()[0],
                    schema_version=1,
                    overlay_json=Jsonb(overlay),
                    overlay_hash=overlay_hash(overlay),
                    base_settings_version_id=settings_id,
                    base_settings_hash=root_hash,
                    status="DRAFT",
                    revision=1,
                    created_by=owner,
                    updated_by=owner,
                )
                connection.execute(
                    "UPDATE bot_config_version SET status='PUBLISHED', approved_by=%s, "
                    "published_at=clock_timestamp(), effective_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND id=%s",
                    (owner, workspace, config_id),
                )
            if binding is not None:
                connection.execute(
                    "INSERT INTO telegram_signal_subscription (id, workspace_id, membership_id, "
                    "bot_id, telegram_binding_id, telegram_chat_id, enabled) "
                    "VALUES (%s,%s,%s,%s,%s,%s,true) ON CONFLICT DO NOTHING",
                    (
                        det(f"subscription:{bot['key']}"),
                        workspace,
                        binding[0],
                        bot_id,
                        binding[1],
                        binding[2],
                    ),
                )
            bots_out.append(
                {
                    "key": bot["key"],
                    "bot_id": str(bot_id),
                    "account_id": str(account),
                    "name": bot["name"],
                }
            )
    return {
        "settings_version_id": str(settings_id),
        "bots": bots_out,
        "disabled_accounts": [str(row[0]) for row in disabled],
        "telegram_subscriptions": binding is not None,
    }


async def _container_genesis(state: dict[str, Any]) -> dict[str, Any]:
    import hashlib
    from datetime import UTC, datetime
    from uuid import UUID

    from sqlalchemy import select

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models import PaperExecutionCheckpointRecord
    from trading_bot.adapters.database.repositories.paper_checkpoints import (
        PaperCheckpointRepository,
    )
    from trading_bot.adapters.database.repositories.workspace_settings import (
        WorkspaceSettingsRepository,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.paper_execution_profile import (
        default_paper_execution_profile_catalog,
        resolve_paper_execution_profile,
    )
    from trading_bot.domain.execution.paper import PaperExecutionGateway

    workspace = UUID(state["workspace_id"])
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    created = 0
    try:
        for bot in state["bots"]:
            account = UUID(bot["account_id"])
            async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                await uow.bind_account(account)
                existing = await uow.session.scalar(
                    select(PaperExecutionCheckpointRecord.id)
                    .where(
                        PaperExecutionCheckpointRecord.workspace_id == workspace,
                        PaperExecutionCheckpointRecord.account_id == account,
                    )
                    .limit(1)
                )
                if existing is not None:
                    continue
                published = await WorkspaceSettingsRepository(uow.session).load_effective_config(
                    workspace_id=workspace
                )
                profile = resolve_paper_execution_profile(
                    published, default_paper_execution_profile_catalog()
                )
                key = f"signal-test-genesis:{account}"
                await PaperCheckpointRepository().append(
                    uow,
                    account_id=account,
                    event_key=key,
                    event_hash=hashlib.sha256(key.encode()).hexdigest(),
                    snapshot=PaperExecutionGateway(profile.fill_policy).export_state(),
                    occurred_at=datetime.now(UTC),
                )
                created += 1
    finally:
        await engine.dispose()
    return {"genesis_created": created}


if __name__ == "__container__":
    import asyncio

    _phase = os.environ["SIGTEST_PHASE"]
    _state = json.loads(os.environ["SIGTEST_STATE"])
    if _phase == "apply":
        _result = _container_apply(_state)
    elif _phase == "genesis":
        _result = asyncio.run(_container_genesis(_state))
    else:
        raise SystemExit(_phase)
    print(json.dumps(_result, default=str))
elif __name__ == "__main__":
    raise SystemExit(main())
