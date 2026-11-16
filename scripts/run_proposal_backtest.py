"""AI-3 trên host: backtest đề xuất của AI Analyst (bản gốc vs bản đề xuất) → lưu → Telegram.

Owner quyết 16/11/2026: đề xuất AI chỉ là gợi ý, phải backtest trước; owner duyệt bằng bản nháp
config rồi tự publish (MFA). Script **không** đổi cấu hình bot nào.

Phần đọc nến/quyền dữ liệu, chạy replay và lưu ``proposal_backtest`` chạy trong container
``migrator`` (role admin, cùng image ứng dụng); host chỉ điều phối và gửi thẻ Telegram (token đọc
từ ``.secrets``, không in). Mỗi đề xuất (``analyst_run`` + vị trí) chỉ được backtest một lần.

    uv run python scripts/run_proposal_backtest.py --hours 48 --send-telegram
    uv run python scripts/run_proposal_backtest.py --max-symbols 3   # chạy thử nhanh
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
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
DEFAULT_WORKSPACE = "c486b52b-912a-446f-aeed-99685c06cad6"
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(AI3_PHASE=p['phase'],AI3_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'run_proposal_backtest.py','exec'),{'__name__':'__container__'})"
)


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------


def _phase(project: str, phase: str, state: dict[str, Any], timeout: float) -> dict[str, Any]:
    payload = json.dumps(
        {"phase": phase, "state": state, "code": Path(__file__).read_text(encoding="utf-8")}
    )
    command = [
        "docker", "compose", "-p", project, "-f", "compose.yaml",
        "run", "--rm", "--no-deps", "-T", "migrator", "python", "-c", PROBE_BOOTSTRAP,
    ]  # fmt: skip
    done = subprocess.run(
        command, cwd=ROOT, input=payload, capture_output=True, text=True, timeout=timeout
    )
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines:
        print(done.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"Bước {phase} thất bại (mã {done.returncode}).")
    return dict(json.loads(lines[-1]))


def send_telegram(
    results: list[dict[str, Any]], chats: list[int], sender: Any = None, pause: float = 1.1
) -> int:
    """Gửi thẻ so sánh tới các chat đã liên kết; lỗi một tin chỉ ghi lại."""
    import asyncio

    from trading_bot.adapters.telegram.bot_api_sender import (
        TelegramBotApiSender,
        load_telegram_bot_token,
    )
    from trading_bot.adapters.telegram.card_delivery import send_backtest_card

    if sender is None:
        sender = TelegramBotApiSender(
            load_telegram_bot_token(ROOT / ".secrets" / "telegram_bot_token")
        )

    async def run() -> int:
        sent = 0
        for item in results:
            for chat in chats:
                try:
                    await send_backtest_card(sender, chat, item["proposal"], item["result"])
                except Exception as error:  # noqa: BLE001
                    print(f"Gửi Telegram lỗi: {type(error).__name__}", file=sys.stderr)
                    continue
                sent += 1
                await asyncio.sleep(pause)
        return sent

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE)
    parser.add_argument("--hours", type=float, default=48.0, help="Đề xuất trong N giờ qua.")
    parser.add_argument("--max-proposals", type=int, default=3)
    parser.add_argument("--max-symbols", type=int, default=20, help="Trần số coin mỗi đề xuất.")
    parser.add_argument("--project", default="crypidian-review")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument(
        "--dry-run", action="store_true", help="Chạy thử: không lưu DB, không gửi Telegram."
    )
    args = parser.parse_args(argv)
    if "live" in args.project.lower() or "prod" in args.project.lower():
        raise SystemExit("Script chỉ dành cho stack local PAPER.")
    done = _phase(
        args.project,
        "backtest",
        {
            "workspace_id": args.workspace_id,
            "hours": args.hours,
            "max_proposals": args.max_proposals,
            "max_symbols": args.max_symbols,
            "persist": not args.dry_run,
        },
        args.timeout,
    )
    send = args.send_telegram and not args.dry_run
    sent = send_telegram(done["results"], done["chats"]) if send else 0
    print(
        json.dumps(
            {
                "backtested": [
                    {
                        "bot": r["result"]["bot_name"],
                        "verdict": r["result"]["verdict"],
                        "baseline": r["result"]["baseline"],
                        "variant": r["result"]["variant"],
                        "skipped": len(r["result"]["skipped_symbols"]),
                        "notes": r["result"]["notes"],
                    }
                    for r in done["results"]
                ],
                "invalid": done.get("invalid", []),
                "telegram_sent": sent,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Trong container migrator (role admin)
# ---------------------------------------------------------------------------


def _sessions_factory() -> tuple[Any, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    return engine, create_session_factory(engine)


async def _container(phase: str, state: dict[str, Any]) -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from sqlalchemy import select

    from trading_bot.adapters.database.models import (
        AnalystRunRecord,
        BotConfigVersion,
        BotInstance,
        ProposalBacktestRecord,
        TelegramBinding,
        TelegramSignalSubscription,
    )
    from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
    from trading_bot.adapters.database.repositories.source_registry import (
        SourceRegistryRepository,
    )
    from trading_bot.application.ai_analyst import ANALYST_OUTPUT_VERSION, validate_proposals
    from trading_bot.application.binance_rest_backfill_rights import (
        SourceRegistryBackfillRightsGate,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.proposal_backtest import (
        PROPOSAL_BACKTEST_VERSION,
        arms_for_proposal,
        backtest_proposal,
        build_stored_bundle,
        required_timeframes,
    )

    if phase != "backtest":
        raise SystemExit(phase)
    workspace = UUID(state["workspace_id"])
    since = datetime.now(UTC) - timedelta(hours=float(state["hours"]))
    engine, sessions = _sessions_factory()
    results: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    try:
        async with UnitOfWork(sessions, workspace_id=workspace) as uow:
            done = {
                (run_id, index)
                for run_id, index in (
                    await uow.session.execute(
                        select(
                            ProposalBacktestRecord.analyst_run_id,
                            ProposalBacktestRecord.proposal_index,
                        ).where(ProposalBacktestRecord.workspace_id == workspace)
                    )
                ).all()
            }
            runs = (
                await uow.session.execute(
                    select(
                        AnalystRunRecord.id,
                        AnalystRunRecord.topic_key,
                        AnalystRunRecord.structured_output,
                    )
                    .where(
                        AnalystRunRecord.workspace_id == workspace,
                        AnalystRunRecord.valid.is_(True),
                        AnalystRunRecord.output_version == ANALYST_OUTPUT_VERSION,
                        AnalystRunRecord.created_at >= since,
                    )
                    .order_by(AnalystRunRecord.created_at.desc())
                )
            ).all()
            bots = {
                name: bot_id
                for bot_id, name in (
                    await uow.session.execute(
                        select(BotInstance.id, BotInstance.name).where(
                            BotInstance.workspace_id == workspace
                        )
                    )
                ).all()
            }
        pending = [
            (run_id, topic, index, proposal)
            for run_id, topic, output in runs
            for index, proposal in enumerate((output or {}).get("proposals") or [])
            if (run_id, index) not in done
        ][: int(state["max_proposals"])]
        for run_id, topic, index, proposal in pending:
            bot_id = bots.get(str(proposal.get("bot_name")))
            async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                overlay = (
                    await uow.session.scalar(
                        select(BotConfigVersion.overlay_json)
                        .where(
                            BotConfigVersion.workspace_id == workspace,
                            BotConfigVersion.bot_id == bot_id,
                            BotConfigVersion.status == "PUBLISHED",
                        )
                        .order_by(BotConfigVersion.version.desc())
                        .limit(1)
                    )
                    if bot_id
                    else None
                ) or {}
            strategy = overlay.get("strategy") or {}
            config = {
                "release_id": strategy.get("release_id"),
                "parameters": strategy.get("parameters") or {},
                "risk": overlay.get("risk") or {},
            }
            symbols = list((overlay.get("universe") or {}).get("symbols") or [])
            symbols = symbols[: int(state["max_symbols"])]
            # Kiểm lại với cấu hình hiện hành (bot có thể đã đổi từ lúc AI phân tích).
            errors = validate_proposals(
                [proposal], {"bots": [{"bot_name": proposal.get("bot_name"), "config": config}]}
            )
            reason = errors[0] if errors else None
            if bot_id is None or not symbols:
                reason = reason or "Bot không còn hoặc universe trống."
            baseline_arm = variant_arm = None
            if reason is None:
                baseline_arm, variant_arm, _ = arms_for_proposal(config, proposal["changes"])
                try:
                    timeframes = sorted(
                        set(required_timeframes(baseline_arm.release_id))
                        | set(required_timeframes(variant_arm.release_id))
                    )
                except ValueError as error:
                    reason = f"Replay chưa hỗ trợ: {error}"
            if reason is not None:
                result_payload = {
                    "version": PROPOSAL_BACKTEST_VERSION,
                    "bot_name": str(proposal.get("bot_name")),
                    "verdict": "UNSUPPORTED",
                    "notes": [reason],
                    "baseline": {},
                    "variant": {},
                    "skipped_symbols": [],
                    "window": None,
                }
                invalid.append({"bot": proposal.get("bot_name"), "reason": reason})
            else:
                now = datetime.now(UTC)
                bundles: dict[str, Any] = {}
                async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                    gate = SourceRegistryBackfillRightsGate(
                        workspace_id=workspace,
                        repository=SourceRegistryRepository(uow.session),
                    )
                    market = MarketDataRepository(uow.session)
                    for symbol in symbols:
                        try:
                            bundles[symbol] = await build_stored_bundle(
                                workspace_id=workspace,
                                symbol=symbol,
                                timeframes=timeframes,
                                market_data=market,
                                rights_gate=gate,
                                now=now,
                            )
                        except (ValueError, PermissionError) as error:
                            bundles[symbol] = f"{type(error).__name__}: {str(error)[:140]}"
                result = backtest_proposal(
                    bot_name=str(proposal["bot_name"]),
                    bot_config=config,
                    changes=proposal["changes"],
                    bundles=bundles,
                )
                result_payload = result.payload()
            if not state.get("persist", True):
                results.append({"proposal": dict(proposal), "result": result_payload})
                continue
            async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                uow.session.add(
                    ProposalBacktestRecord(
                        workspace_id=workspace,
                        analyst_run_id=run_id,
                        proposal_index=index,
                        topic_key=topic,
                        bot_id=bot_id,
                        bot_name=str(proposal.get("bot_name"))[:160],
                        proposal=dict(proposal),
                        result=result_payload,
                        verdict=result_payload["verdict"],
                        version=PROPOSAL_BACKTEST_VERSION,
                    )
                )
            results.append({"proposal": dict(proposal), "result": result_payload})
        async with UnitOfWork(sessions, workspace_id=workspace) as uow:
            chats = await uow.session.execute(
                select(TelegramSignalSubscription.telegram_chat_id)
                .join(
                    TelegramBinding,
                    TelegramBinding.id == TelegramSignalSubscription.telegram_binding_id,
                )
                .where(
                    TelegramSignalSubscription.workspace_id == workspace,
                    TelegramSignalSubscription.enabled.is_(True),
                    TelegramBinding.verified_at.is_not(None),
                    TelegramBinding.revoked_at.is_(None),
                )
                .distinct()
            )
            chat_ids = sorted({int(c) for (c,) in chats})
    finally:
        await engine.dispose()
    return {"results": results, "invalid": invalid, "chats": chat_ids}


if __name__ == "__container__":
    import asyncio

    _state = json.loads(os.environ["AI3_STATE"])
    print(json.dumps(asyncio.run(_container(os.environ["AI3_PHASE"], _state)), default=str))
elif __name__ == "__main__":
    raise SystemExit(main())
