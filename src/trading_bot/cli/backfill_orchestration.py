"""CLI quản lý backfill BACKTEST_RESEARCH → manifest → bundle & quality report (CRY-364)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from trading_bot.application.backfill_orchestration import (
    BackfillDatasetOrchestrationService,
    StoredBarPoint,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Điều phối backfill BACKTEST_RESEARCH, manifest READY và quality report."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Tạo job backfill fixture-safe (connector disabled).")
    create.add_argument("--workspace-id", required=True, type=UUID)
    create.add_argument("--symbol", required=True)
    create.add_argument("--interval", required=True)
    create.add_argument("--range-start", required=True)
    create.add_argument("--range-end", required=True)
    create.add_argument("--actor", default="operator-cli")
    return parser


def run_cli(
    argv: list[str],
    *,
    service: BackfillDatasetOrchestrationService | None = None,
    stored_bars: list[StoredBarPoint] | None = None,
    policy_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Thực thi CLI trả về dict JSON-safe để dùng cho script và test tự động."""
    args = build_parser().parse_args(argv)
    svc = service or BackfillDatasetOrchestrationService()
    if args.command == "create":
        job = svc.create_job(
            workspace_id=args.workspace_id,
            symbol=args.symbol,
            interval=args.interval,
            range_start=datetime.fromisoformat(args.range_start),
            range_end=datetime.fromisoformat(args.range_end),
            created_by=args.actor,
            connector_enabled=False,
        )
        if stored_bars is not None and policy_fingerprint is not None:
            job = svc.run_or_resume_job(
                job.job_id,
                stored_bars=stored_bars,
                policy_fingerprint=policy_fingerprint,
            )
        return job.to_dict()
    raise ValueError(f"Lệnh không hỗ trợ: {args.command}")


def main(argv: list[str] | None = None) -> int:
    result = run_cli(argv or [])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
