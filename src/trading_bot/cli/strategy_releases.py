"""CLI đồng bộ ``strategy_release`` trong DB với ``StrategyRegistry`` hiện hành.

Ví dụ (stack local, chạy bằng role có quyền ghi ``strategy_release``):

    docker compose -p crypidian-review -f compose.yaml exec -T admin-web \\
        python -m trading_bot.cli.strategy_releases sync --workspace-slug crypidian-cards-v1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.adapters.database.models.entities import Workspace
from trading_bot.admin.strategy_release_sync import sync_workspace_strategy_releases


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Đồng bộ strategy_release trong PostgreSQL theo StrategyRegistry hiện hành."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync_cmd = sub.add_parser("sync", help="Đồng bộ release đã publish của workspace.")
    scope = sync_cmd.add_mutually_exclusive_group(required=True)
    scope.add_argument("--workspace-id", type=UUID, help="UUID workspace cần đồng bộ.")
    scope.add_argument("--workspace-slug", type=str, help="Slug workspace cần đồng bộ.")
    scope.add_argument(
        "--all-workspaces",
        action="store_true",
        help="Đồng bộ toàn bộ workspace hiện có trong DB.",
    )
    sync_cmd.add_argument(
        "--release",
        action="append",
        dest="releases",
        default=None,
        help="Chỉ định release_id cần đồng bộ/publish (có thể lặp nhiều lần).",
    )
    sync_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Chỉ lập kế hoạch và in JSON, không ghi thay đổi vào DB.",
    )
    return parser


async def _resolve_workspace_ids(
    session_factory: object,
    args: argparse.Namespace,
) -> list[UUID]:
    if args.workspace_id is not None:
        return [args.workspace_id]

    async with session_factory() as session:  # type: ignore[operator]
        if args.workspace_slug is not None:
            workspace_id = await session.scalar(
                select(Workspace.id).where(Workspace.slug == args.workspace_slug)
            )
            if workspace_id is None:
                raise SystemExit(f"Không tìm thấy workspace với slug '{args.workspace_slug}'.")
            return [workspace_id]

        rows = (await session.scalars(select(Workspace.id).order_by(Workspace.created_at))).all()
        return list(rows)


async def _run(args: argparse.Namespace) -> int:
    if args.command != "sync":
        raise SystemExit(f"Lệnh không hỗ trợ: {args.command}")

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    try:
        sessions = create_session_factory(engine)
        workspace_ids = await _resolve_workspace_ids(sessions, args)
        reports = []
        for workspace_id in workspace_ids:
            async with sessions() as session:
                async with session.begin():
                    report = await sync_workspace_strategy_releases(
                        session,
                        workspace_id=workspace_id,
                        release_names=args.releases,
                        dry_run=args.dry_run,
                    )
                    reports.append(report.to_dict())
        sys.stdout.write(
            json.dumps({"workspaces": reports}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
