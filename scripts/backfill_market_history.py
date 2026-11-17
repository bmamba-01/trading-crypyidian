"""Backfill lùi lịch sử nến Binance USDⓈ-M (AI-5, CRY-236) trên stack Compose local.

Chạy trên host; phần tải/ghi chạy trong container ``ingestion-worker`` (role DB
``crypidian_ingestion_worker``, mạng ``source_egress``), kiểm quyền Source Registry
(``BACKTEST_RESEARCH``) trước mọi request mạng. Chỉ gọi endpoint klines công khai, không API key.

Ví dụ:
    uv run python scripts/backfill_market_history.py --symbols-from-plan --intervals 1h,4h \
        --days 365
    uv run python scripts/backfill_market_history.py --symbols BTCUSDT --intervals 15m --days 180
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(globals().get("__file__", "/app/x/x")).resolve().parents[1]
DEFAULT_WORKSPACE = "c486b52b-912a-446f-aeed-99685c06cad6"
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(HB_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'backfill_market_history.py','exec'),{'__name__':'__container__'})"
)


def _plan_symbols() -> list[str]:
    plan = json.loads((ROOT / "docs/implementation/signal-test-plan.json").read_text())
    return [item.split(":")[0] for item in str(plan["collector_streams"]).split(";") if item]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE)
    parser.add_argument("--project", default="crypidian-review")
    parser.add_argument("--symbols", default="", help="Danh sách cách nhau bởi dấu phẩy.")
    parser.add_argument("--symbols-from-plan", action="store_true")
    parser.add_argument("--intervals", default="1h,4h")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--timeout", type=float, default=7200.0)
    args = parser.parse_args(argv)
    if "live" in args.project.lower() or "prod" in args.project.lower():
        raise SystemExit("Script chỉ dành cho stack local PAPER.")
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.symbols_from_plan:
        symbols = list(dict.fromkeys([*symbols, *_plan_symbols()]))
    if not symbols:
        raise SystemExit("Cần --symbols hoặc --symbols-from-plan.")
    intervals = [i.strip() for i in args.intervals.split(",") if i.strip()]
    if not 1 <= args.days <= 1000:
        raise SystemExit("--days phải trong 1..1000.")
    payload = json.dumps(
        {
            "state": {
                "workspace_id": args.workspace_id,
                "symbols": symbols,
                "intervals": intervals,
                "days": args.days,
            },
            "code": Path(__file__).read_text(encoding="utf-8"),
        }
    )
    command = [
        "docker", "compose", "-p", args.project, "-f", "compose.yaml", "--profile", "paper",
        "run", "--rm", "--no-deps", "-T", "ingestion-worker", "python", "-c", PROBE_BOOTSTRAP,
    ]  # fmt: skip
    done = subprocess.run(
        command, cwd=ROOT, input=payload, capture_output=True, text=True, timeout=args.timeout
    )
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines:
        print(done.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"Backfill thất bại (mã {done.returncode}).")
    result = json.loads(lines[-1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] != "error" for item in result["results"]) else 1


# ---------------------------------------------------------------------------
# Trong container ingestion-worker
# ---------------------------------------------------------------------------


async def _container(state: dict[str, Any]) -> dict[str, Any]:
    from datetime import UTC, datetime
    from uuid import UUID

    from sqlalchemy import func, select

    from trading_bot.adapters.binance_usdm.market_data import BinanceUsdMMarketDataAdapter
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models import MarketBar
    from trading_bot.application.market_history_backfill import (
        BACKFILL_BAR_SOURCE,
        MarketHistoryBackfill,
    )
    from trading_bot.workers.market_bar_collector_worker import database_collector_scope

    workspace = UUID(state["workspace_id"])
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )

    async def earliest(session: Any, symbol: str, interval: str) -> Any:
        return await session.market._session.scalar(
            select(func.min(MarketBar.open_time)).where(
                MarketBar.workspace_id == workspace,
                MarketBar.symbol == symbol,
                MarketBar.timeframe == interval,
                MarketBar.source == BACKFILL_BAR_SOURCE,
            )
        )

    adapter = BinanceUsdMMarketDataAdapter()
    results = []
    try:
        backfill = MarketHistoryBackfill(
            workspace_id=workspace,
            market_data=adapter,
            session_scope=database_collector_scope(create_session_factory(engine), workspace),
            earliest_open_time=earliest,
        )
        for symbol in state["symbols"]:
            for interval in state["intervals"]:
                result = await backfill.backfill(
                    symbol, interval, days=int(state["days"]), now=datetime.now(UTC)
                )
                results.append(result.payload())
                print(json.dumps(result.payload()), file=sys.stderr, flush=True)
    finally:
        await adapter.aclose()
        await engine.dispose()
    return {"results": results}


if __name__ == "__container__":
    import asyncio

    print(json.dumps(asyncio.run(_container(json.loads(os.environ["HB_STATE"])))))
elif __name__ == "__main__":
    raise SystemExit(main())
