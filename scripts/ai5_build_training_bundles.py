"""Dựng bundle replay ghim nguồn (manifest v4) từ nến đã lưu cho dataset học (AI-5, CRY-236).

Mỗi coin một bundle (engine replay chỉ một symbol); cohort nhiều coin được gom ở bước split.
Chạy trên host; phần đọc nến/ghi manifest+bundle chạy trong container ``admin-web`` (role DB
``crypidian_admin_web``), kiểm quyền Source Registry ``BACKTEST_RESEARCH`` cho từng child.
Chỉ dùng nến nguồn ``BINANCE_USDM_REST_BACKFILL`` đã lưu; không gọi mạng; không mang funding
(gate TRAINING chỉ nhận bundle schema 1). Idempotent: chạy lại cùng dữ liệu → DUPLICATE.

Ví dụ:
    uv run python scripts/ai5_build_training_bundles.py --symbols-from-plan \
        --release h03-v3-15m --days 185
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
BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(AI5_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'ai5_build_training_bundles.py','exec'),{'__name__':'__container__'})"
)


def _plan_symbols() -> list[str]:
    plan = json.loads((ROOT / "docs/implementation/signal-test-plan.json").read_text())
    return [item.split(":")[0] for item in str(plan["collector_streams"]).split(";") if item]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE)
    parser.add_argument("--project", default="crypidian-review")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--symbols-from-plan", action="store_true")
    parser.add_argument("--release", default="h03-v3-15m")
    parser.add_argument("--days", type=int, default=185, help="Lookback nến đã lưu (ngày).")
    parser.add_argument("--timeout", type=float, default=3600.0)
    args = parser.parse_args(argv)
    if "live" in args.project.lower() or "prod" in args.project.lower():
        raise SystemExit("Script chỉ dành cho stack local PAPER.")
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.symbols_from_plan:
        symbols = list(dict.fromkeys([*symbols, *_plan_symbols()]))
    if not symbols:
        raise SystemExit("Cần --symbols hoặc --symbols-from-plan.")
    if not 30 <= args.days <= 400:
        raise SystemExit("--days phải trong 30..400.")
    payload = json.dumps(
        {
            "state": {
                "workspace_id": args.workspace_id,
                "symbols": symbols,
                "release": args.release,
                "days": args.days,
            },
            "code": Path(__file__).read_text(encoding="utf-8"),
        }
    )
    command = [
        "docker", "compose", "-p", args.project, "-f", "compose.yaml", "--profile", "paper",
        "run", "--rm", "--no-deps", "-T", "admin-web", "python", "-c", BOOTSTRAP,
    ]  # fmt: skip
    done = subprocess.run(
        command, cwd=ROOT, input=payload, capture_output=True, text=True, timeout=args.timeout
    )
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines:
        print(done.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"Dựng bundle thất bại (mã {done.returncode}).")
    result = json.loads(lines[-1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] != "error" for item in result["results"]) else 1


# ---------------------------------------------------------------------------
# Trong container admin-web
# ---------------------------------------------------------------------------


async def _container(state: dict[str, Any]) -> dict[str, Any]:
    from datetime import UTC, datetime
    from uuid import UUID

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
        BacktestDatasetManifestRepository,
    )
    from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
        BacktestDatasetReplayBundleRepository,
    )
    from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
    from trading_bot.adapters.database.repositories.source_registry import (
        SourceRegistryRepository,
    )
    from trading_bot.application.binance_rest_backfill_rights import (
        SourceRegistryBackfillRightsGate,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.proposal_backtest import build_stored_bundle, required_timeframes

    workspace = UUID(state["workspace_id"])
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    sessions = create_session_factory(engine)
    timeframes = required_timeframes(state["release"])
    now = datetime.now(UTC)
    results = []
    try:
        for symbol in state["symbols"]:
            try:
                async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                    gate = SourceRegistryBackfillRightsGate(
                        workspace_id=workspace,
                        repository=SourceRegistryRepository(uow.session),
                    )
                    bundle = await build_stored_bundle(
                        workspace_id=workspace,
                        symbol=symbol,
                        timeframes=timeframes,
                        market_data=MarketDataRepository(uow.session),
                        rights_gate=gate,
                        now=now,
                        manifest_schema_version=4,
                        lookback_days=int(state["days"]),
                    )
                    if isinstance(bundle, str):
                        results.append({"symbol": symbol, "status": "skipped", "detail": bundle})
                        continue
                    manifests = BacktestDatasetManifestRepository(uow.session)
                    for component in bundle.components:
                        await manifests.append(workspace_id=workspace, manifest=component.manifest)
                    written = await BacktestDatasetReplayBundleRepository(uow.session).append(
                        workspace_id=workspace, bundle=bundle
                    )
                results.append(
                    {
                        "symbol": symbol,
                        "status": "duplicate" if written.is_duplicate else "created",
                        "bundle_hash": bundle.dataset_hash,
                        "evaluation_start": bundle.evaluation_start.isoformat(),
                        "range_end": bundle.range_end.isoformat(),
                        "bars": sum(len(item.bars) for item in bundle.components),
                    }
                )
            except Exception as error:  # noqa: BLE001 - báo lỗi theo coin, không dừng cả lượt
                results.append(
                    {
                        "symbol": symbol,
                        "status": "error",
                        "detail": f"{type(error).__name__}: {str(error)[:200]}",
                    }
                )
            print(json.dumps(results[-1]), file=sys.stderr, flush=True)
    finally:
        await engine.dispose()
    return {"release": state["release"], "timeframes": list(timeframes), "results": results}


if __name__ == "__container__":
    import asyncio

    print(json.dumps(asyncio.run(_container(json.loads(os.environ["AI5_STATE"])))))
elif __name__ == "__main__":
    raise SystemExit(main())
