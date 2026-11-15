"""CLI Run Report AI-1: in JSON báo cáo tất định của các bot ENABLED trong cửa sổ thời gian.

Ví dụ (stack local, chạy trong container admin-web để dùng login DB chỉ đọc báo cáo):

    docker compose -p crypidian-review -f compose.yaml exec -T admin-web \\
        python -m trading_bot.cli.run_report --workspace-id <uuid> --hours 24 --label dot-thu-10-bot

Chỉ đọc dữ liệu; không ghi DB, không gọi mạng.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.application.run_report import load_run_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Report AI-1 (JSON tất định).")
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument("--hours", type=float, default=24.0, help="Độ dài cửa sổ tính tới end.")
    parser.add_argument("--start", type=datetime.fromisoformat, default=None)
    parser.add_argument("--end", type=datetime.fromisoformat, default=None)
    parser.add_argument("--label", default="run")
    parser.add_argument("--output", type=Path, default=None, help="Ghi JSON ra file thay stdout.")
    return parser


def _window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    end = args.end or datetime.now(UTC)
    start = args.start or end - timedelta(hours=args.hours)
    if start.tzinfo is None or end.tzinfo is None:
        raise SystemExit("start/end phải có múi giờ.")
    return start.astimezone(UTC), end.astimezone(UTC)


async def _run(args: argparse.Namespace) -> int:
    start, end = _window(args)
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    try:
        report = await load_run_report(
            create_session_factory(engine),
            workspace_id=args.workspace_id,
            start=start,
            end=end,
            run_label=args.label,
        )
    finally:
        await engine.dispose()
    text = json.dumps(report.payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"report_hash": report.report_hash, "output": str(args.output)}))
    else:
        sys.stdout.write(text + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
