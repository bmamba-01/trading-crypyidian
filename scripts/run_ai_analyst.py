"""AI Analyst (WBS AI-2) trên host: Run Report → CLI agent → validator → lưu → Telegram.

Owner quyết 16/11/2026: engine là CLI agent đã cài trên máy (Claude Code chính, Codex dự phòng),
mỗi chủ đề (mặc định họ chiến lược ``family:H03``…) một session được lưu và tiếp tục; kết quả chỉ
là gợi ý, phải backtest trước. Script chạy trên **host** vì credential đăng nhập CLI nằm ở host;
phần đọc/ghi DB chạy qua container ``migrator`` của project Compose local.

Ví dụ:

    uv run python scripts/run_ai_analyst.py --engine claude --hours 24 --send-telegram
    uv run python scripts/run_ai_analyst.py --engine fake --hours 24  # kiểm luồng, không gọi AI

Không in secret; chỉ gửi Telegram khi kết quả qua validator số liệu.
"""

from __future__ import annotations

import argparse
import glob
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
CLAUDE_GLOB = str(
    Path.home()
    / "Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"
)
CODEX_DEFAULT = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(AI2_PHASE=p['phase'],AI2_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'run_ai_analyst.py','exec'),{'__name__':'__container__'})"
)


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------


def find_claude_binary() -> str | None:
    """Binary Claude Code mới nhất trong bundle Claude Desktop (không có trên PATH)."""
    override = os.environ.get("CRYPIDIAN_CLAUDE_BIN")
    if override:
        return override
    found = sorted(
        glob.glob(CLAUDE_GLOB),
        key=lambda path: [int(p) if p.isdigit() else p for p in Path(path).parts[-6].split(".")],
    )
    return found[-1] if found else None


def build_engine(name: str) -> Any:
    from trading_bot.adapters.analyst.cli_engines import ClaudeCliEngine, CodexCliEngine

    if name == "claude":
        binary = find_claude_binary()
        if binary is None:
            raise SystemExit("Không tìm thấy Claude Code CLI; đặt CRYPIDIAN_CLAUDE_BIN.")
        return ClaudeCliEngine(
            binary,
            model=os.environ.get("CRYPIDIAN_ANALYST_MODEL"),
            # Token dài hạn từ `claude setup-token` (gitignored, chmod 600); không có thì CLI
            # dùng đăng nhập Keychain (`claude auth login`).
            oauth_token_file=ROOT / ".secrets" / "claude_code_oauth_token",
        )
    if name == "codex":
        return CodexCliEngine(
            os.environ.get("CRYPIDIAN_CODEX_BIN", CODEX_DEFAULT),
            model=os.environ.get("CRYPIDIAN_ANALYST_MODEL"),
        )
    if name == "fake":
        return FakeEngine()
    raise SystemExit(f"Engine không hỗ trợ: {name}")


class FakeEngine:
    """Engine giả tất định để kiểm luồng (không gọi AI): trích một số có thật từ report."""

    name = "fake"

    def analyze(self, request: Any) -> Any:
        from trading_bot.ports.analyst import AnalysisResult

        report_json = request.prompt.split("<run_report>\n", 1)[1].split("\n</run_report>", 1)[0]
        report = json.loads(report_json)
        bots = report.get("bots") or []
        findings = []
        if bots:
            bot = bots[0]
            findings.append(
                {
                    "statement": (
                        f"{bot['bot_name']} có {bot['funnel']['candidates']} tín hiệu trong cửa sổ."
                    ),
                    "evidence_refs": ["/bots/0/funnel/candidates"],
                }
            )
        output = {
            "summary": "Kết quả kiểm luồng bằng engine giả; không phải phân tích thật.",
            "findings": findings,
            "hypotheses": [],
            "proposals": [],
            "confidence": "low",
        }
        session = request.session_id or "fake-session"
        return AnalysisResult(
            engine=self.name,
            structured_output=output,
            raw_stdout=json.dumps(output, ensure_ascii=False),
            session_id=session,
            exit_code=0,
            cli_version="fake-1",
            model="fake",
        )


def _phase(project: str, service: str, phase: str, state: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(
        {"phase": phase, "state": state, "code": Path(__file__).read_text(encoding="utf-8")}
    )
    command = [
        "docker", "compose", "-p", project, "-f", "compose.yaml",
        "run", "--rm", "--no-deps", "-T", service, "python", "-c", PROBE_BOOTSTRAP,
    ]  # fmt: skip
    done = subprocess.run(command, cwd=ROOT, input=payload, capture_output=True, text=True)
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines:
        print(done.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"Bước {phase} thất bại (mã {done.returncode}).")
    return dict(json.loads(lines[-1]))


def analyze_topics(
    report: dict[str, Any],
    engine: Any,
    sessions: dict[str, str],
    *,
    families: list[str] | None,
    timeout: float,
    budget: float,
) -> list[dict[str, Any]]:
    """Chạy engine cho từng họ chiến lược có trong report; trả bản ghi để lưu."""
    from trading_bot.application.ai_analyst import (
        ANALYST_OUTPUT_SCHEMA,
        ANALYST_OUTPUT_VERSION,
        PROMPT_VERSION,
        build_prompt,
        input_hash,
        topic_report,
        validate_analysis,
    )
    from trading_bot.ports.analyst import AnalysisRequest

    present = sorted({bot["config"]["strategy_family"] for bot in report.get("bots", [])})
    records = []
    for family in families or present:
        if family not in present:
            continue
        topic_key = f"family:{family}"
        topic = topic_report(report, family)
        session_id = sessions.get(topic_key)
        prompt = build_prompt(topic, resumed=session_id is not None)
        result = engine.analyze(
            AnalysisRequest(
                topic_key=topic_key,
                prompt=prompt,
                schema=ANALYST_OUTPUT_SCHEMA,
                session_id=session_id,
                resume=session_id is not None,
                timeout_seconds=timeout,
                max_budget_usd=budget,
            )
        )
        validation = (
            validate_analysis(result.structured_output, topic)
            if result.structured_output is not None
            else None
        )
        records.append(
            {
                "topic_key": topic_key,
                "engine": result.engine,
                "session_id": result.session_id,
                "resumed": session_id is not None,
                "run_report_hash": report["report_hash"],
                "input_sha256": input_hash(prompt),
                "prompt_version": PROMPT_VERSION,
                "output_version": ANALYST_OUTPUT_VERSION,
                "cli_version": result.cli_version,
                "model": result.model,
                "usage": dict(result.usage),
                "cost_usd": result.cost_usd,
                "exit_code": result.exit_code,
                "error": result.error,
                "raw_stdout": result.raw_stdout[-200_000:],
                "structured_output": dict(result.structured_output)
                if result.structured_output is not None
                else None,
                "validation_errors": list(validation.errors) if validation else [],
                "valid": bool(validation and validation.ok and result.error is None),
                "topic_report": topic,
            }
        )
    return records


def send_telegram(
    records: list[dict[str, Any]], chats: list[int], sender: Any = None, pause: float = 1.1
) -> int:
    """Gửi kết quả hợp lệ tới các chat đã liên kết; token đọc từ .secrets (không in)."""
    import asyncio

    from trading_bot.adapters.telegram.bot_api_sender import (
        TelegramBotApiSender,
        load_telegram_bot_token,
    )
    from trading_bot.application.ai_analyst import format_analysis_message

    if sender is None:
        token = load_telegram_bot_token(ROOT / ".secrets" / "telegram_bot_token")
        sender = TelegramBotApiSender(token)

    async def run() -> int:
        sent = 0
        for record in records:
            if not record["valid"]:
                continue
            text = format_analysis_message(
                record["topic_key"], record["topic_report"], record["structured_output"]
            )
            for chat in chats:
                await sender.send_message(chat, text)
                sent += 1
                await asyncio.sleep(pause)
        return sent

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", choices=("claude", "codex", "fake"), default="claude")
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--label", default="ai-analyst")
    parser.add_argument("--family", action="append", help="Chỉ chạy họ chiến lược này (lặp được).")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--max-budget-usd", type=float, default=1.0)
    parser.add_argument("--project", default="crypidian-review")
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument(
        "--report-file", type=Path, help="Dùng report JSON có sẵn thay vì sinh mới."
    )
    args = parser.parse_args(argv)
    if "live" in args.project.lower() or "prod" in args.project.lower():
        raise SystemExit("Script chỉ dành cho stack local PAPER.")
    state: dict[str, Any] = {"workspace_id": args.workspace_id}
    if args.report_file:
        report = json.loads(args.report_file.read_text(encoding="utf-8"))
    else:
        report = _phase(
            args.project,
            "migrator",
            "report",
            state | {"hours": args.hours, "label": args.label},
        )
    engine = build_engine(args.engine)
    sessions = _phase(args.project, "migrator", "sessions", state | {"engine": engine.name})
    records = analyze_topics(
        report,
        engine,
        sessions["sessions"],
        families=args.family,
        timeout=args.timeout,
        budget=args.max_budget_usd,
    )
    stored = _phase(
        args.project,
        "migrator",
        "persist",
        state | {"records": [{k: v for k, v in r.items() if k != "topic_report"} for r in records]},
    )
    sent = 0
    if args.send_telegram:
        sent = send_telegram(records, stored["chats"])
    summary = [
        {
            "topic": r["topic_key"],
            "engine": r["engine"],
            "valid": r["valid"],
            "error": r["error"],
            "errors": r["validation_errors"][:3],
            "session": r["session_id"],
            "resumed": r["resumed"],
        }
        for r in records
    ]
    print(
        json.dumps(
            {"report_hash": report["report_hash"], "runs": summary, "telegram_sent": sent},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Trong container migrator (role admin, đọc/ghi DB)
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
    from decimal import Decimal
    from uuid import UUID

    from sqlalchemy import select, update

    from trading_bot.adapters.database.models import (
        AnalystRunRecord,
        AnalystSessionRecord,
        TelegramBinding,
        TelegramSignalSubscription,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.run_report import load_run_report

    workspace = UUID(state["workspace_id"])
    engine, sessions = _sessions_factory()
    try:
        if phase == "report":
            end = datetime.now(UTC)
            report = await load_run_report(
                sessions,
                workspace_id=workspace,
                start=end - timedelta(hours=float(state["hours"])),
                end=end,
                run_label=str(state["label"]),
            )
            return dict(report.payload)
        if phase == "sessions":
            async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                rows = await uow.session.execute(
                    select(AnalystSessionRecord.topic_key, AnalystSessionRecord.session_id)
                    .where(
                        AnalystSessionRecord.workspace_id == workspace,
                        AnalystSessionRecord.engine == state["engine"],
                        AnalystSessionRecord.status == "ACTIVE",
                    )
                    .order_by(AnalystSessionRecord.created_at)
                )
                return {"sessions": {str(topic): str(session) for topic, session in rows.all()}}
        if phase == "persist":
            now = datetime.now(UTC)
            async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                session = uow.session
                for record in state["records"]:
                    session.add(
                        AnalystRunRecord(
                            workspace_id=workspace,
                            **{
                                k: (Decimal(str(v)) if k == "cost_usd" and v is not None else v)
                                for k, v in record.items()
                            },
                        )
                    )
                    if record["session_id"] and record["exit_code"] == 0:
                        existing = await session.scalar(
                            select(AnalystSessionRecord).where(
                                AnalystSessionRecord.workspace_id == workspace,
                                AnalystSessionRecord.topic_key == record["topic_key"],
                                AnalystSessionRecord.engine == record["engine"],
                                AnalystSessionRecord.session_id == record["session_id"],
                            )
                        )
                        if existing is None:
                            # Session mới của chủ đề: các session cũ của cùng engine nghỉ hưu.
                            await session.execute(
                                update(AnalystSessionRecord)
                                .where(
                                    AnalystSessionRecord.workspace_id == workspace,
                                    AnalystSessionRecord.topic_key == record["topic_key"],
                                    AnalystSessionRecord.engine == record["engine"],
                                )
                                .values(status="RETIRED")
                            )
                            session.add(
                                AnalystSessionRecord(
                                    workspace_id=workspace,
                                    topic_key=record["topic_key"],
                                    engine=record["engine"],
                                    session_id=record["session_id"],
                                    status="ACTIVE",
                                    run_count=1,
                                    last_used_at=now,
                                )
                            )
                        else:
                            existing.run_count += 1
                            existing.last_used_at = now
                chats = await session.execute(
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
                return {
                    "stored": len(state["records"]),
                    "chats": sorted({int(c) for (c,) in chats}),
                }
        raise SystemExit(phase)
    finally:
        await engine.dispose()


if __name__ == "__container__":
    import asyncio

    _state = json.loads(os.environ["AI2_STATE"])
    print(json.dumps(asyncio.run(_container(os.environ["AI2_PHASE"], _state)), default=str))
elif __name__ == "__main__":
    raise SystemExit(main())
