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


STATE_DIR = ROOT / "var" / "ai-analyst"


def topic_fingerprint(topic: dict[str, Any]) -> str:
    """Dấu vân tay các **sự kiện có ý nghĩa** của chủ đề để biết có cần gọi AI lại không.

    Chỉ gồm tín hiệu, lệnh, vị thế, PnL, lý do từ chối theo bot. Số quyết định ABSTAIN tăng mỗi
    phút nên không đưa vào (16/11: băm cả report làm dấu vân tay luôn đổi, AI bị gọi mỗi lượt).
    """
    import hashlib

    stable = [
        {
            "bot": bot.get("bot_name"),
            "candidates": (bot.get("funnel") or {}).get("candidates"),
            "with_intent": (bot.get("funnel") or {}).get("candidates_with_intent"),
            "rejections": (bot.get("funnel") or {}).get("rejection_reasons"),
            "closed": (bot.get("performance") or {}).get("closed_trades"),
            "open": (bot.get("performance") or {}).get("open_trades"),
            "pnl": (bot.get("performance") or {}).get("net_pnl_usdt"),
        }
        for bot in topic.get("bots", [])
    ]
    canonical = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_state(path: Path) -> dict[str, Any]:
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class RunLock:
    """Khóa file không chặn: lượt lịch mới bỏ qua nếu lượt trước còn chạy."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    def __enter__(self) -> bool:
        import fcntl

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("w")
        try:
            fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def __exit__(self, *_exc: object) -> None:
        if self._handle is not None:
            self._handle.close()


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
    skip_fingerprints: dict[str, str] | None = None,
    max_engine_calls: int | None = None,
    max_repairs: int = 2,
) -> list[dict[str, Any]]:
    """Chạy engine cho từng họ chiến lược có trong report; trả bản ghi để lưu."""
    from trading_bot.application.ai_analyst import (
        ANALYST_OUTPUT_SCHEMA,
        ANALYST_OUTPUT_VERSION,
        CONTEXT_KEYS,
        PROMPT_VERSION,
        build_prompt,
        build_repair_prompt,
        input_hash,
        topic_report,
        validate_analysis,
    )
    from trading_bot.ports.analyst import AnalysisRequest

    present = sorted({bot["config"]["strategy_family"] for bot in report.get("bots", [])})
    records = []
    calls = 0

    def record(
        topic_key: str, topic: dict[str, Any], prompt: str, result: Any, resumed: bool
    ) -> dict[str, Any]:
        validation = (
            validate_analysis(result.structured_output, topic)
            if result.structured_output is not None
            else None
        )
        return {
            "topic_key": topic_key,
            "engine": result.engine,
            "session_id": result.session_id,
            "resumed": resumed,
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
            # CRY-384: bối cảnh ngoài Run Report đã đưa cho AI, lưu để tái lập đầu vào.
            "context": {key: topic[key] for key in CONTEXT_KEYS if key in topic} or None,
        }

    for family in families or present:
        if family not in present:
            continue
        topic_key = f"family:{family}"
        topic = topic_report(report, family)
        fingerprint = topic_fingerprint(topic)
        if skip_fingerprints is not None and skip_fingerprints.get(topic_key) == fingerprint:
            # Dữ liệu chủ đề không đổi từ lượt trước: không gọi AI (tiết kiệm hạn mức).
            continue
        if max_engine_calls is not None and calls >= max_engine_calls:
            break
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
        calls += 1
        latest = record(topic_key, topic, prompt, result, session_id is not None)
        latest["fingerprint"] = fingerprint
        records.append(latest)
        # Tối đa `max_repairs` lượt sửa trong cùng session (16/11: một lượt không đủ — AI sửa lỗi
        # này lại bỏ trích dẫn khác). Lỗi validator kèm gợi ý trường có giá trị khớp.
        repairs = 0
        session = result.session_id
        while (
            not latest["valid"]
            and latest["validation_errors"]
            and session
            and repairs < max_repairs
            and (max_engine_calls is None or calls < max_engine_calls)
        ):
            repair_prompt = build_repair_prompt(latest["validation_errors"])
            repaired = engine.analyze(
                AnalysisRequest(
                    topic_key=topic_key,
                    prompt=repair_prompt,
                    schema=ANALYST_OUTPUT_SCHEMA,
                    session_id=session,
                    resume=True,
                    timeout_seconds=timeout,
                    max_budget_usd=budget,
                )
            )
            calls += 1
            repairs += 1
            latest = record(topic_key, topic, repair_prompt, repaired, True)
            latest["fingerprint"] = fingerprint
            records.append(latest)
            session = repaired.session_id or session
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
    from trading_bot.adapters.telegram.card_delivery import send_analysis_card

    if sender is None:
        token = load_telegram_bot_token(ROOT / ".secrets" / "telegram_bot_token")
        sender = TelegramBotApiSender(token)

    async def run() -> int:
        sent = 0
        for record in records:
            if not record["valid"]:
                continue
            for chat in chats:
                # Ảnh thẻ "Phân tích AI" + caption; vẽ lỗi thì tự gửi văn bản. Lỗi gửi một tin
                # chỉ ghi lại, không làm hỏng cả lượt (kết quả đã lưu DB trước đó).
                try:
                    await send_analysis_card(
                        sender,
                        chat,
                        record["topic_key"],
                        record["topic_report"],
                        record["structured_output"],
                    )
                except Exception as error:  # noqa: BLE001
                    print(
                        f"Gửi Telegram lỗi ({record['topic_key']}): {type(error).__name__}",
                        file=sys.stderr,
                    )
                    continue
                sent += 1
                await asyncio.sleep(pause)
        return sent

    return asyncio.run(run())


def backtest_proposals(
    records: list[dict[str, Any]],
    project: str,
    *,
    send: bool,
    runner: Any = subprocess.run,
    max_symbols: int = 12,
) -> dict[str, Any] | None:
    """AI-3: chạy ``run_proposal_backtest.py`` khi lượt này có đề xuất hợp lệ.

    Lỗi backtest chỉ ghi lại, không làm hỏng lượt phân tích (kết quả AI đã lưu và gửi trước đó).
    """
    proposals = sum(
        len((r.get("structured_output") or {}).get("proposals") or [])
        for r in records
        if r.get("valid")
    )
    if proposals == 0:
        return None
    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_proposal_backtest.py"),
        "--project", project,
        "--hours", "6",
        "--max-proposals", str(min(proposals, 5)),
        "--max-symbols", str(max_symbols),
    ]  # fmt: skip
    if send:
        command.append("--send-telegram")
    done = runner(command, cwd=ROOT, capture_output=True, text=True, timeout=7200)
    if done.returncode != 0:
        print(f"Backtest đề xuất lỗi (mã {done.returncode}).", file=sys.stderr)
        print(done.stderr[-2000:], file=sys.stderr)
    return {"proposals": proposals, "exit_code": done.returncode}


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
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Chế độ lịch: khóa chạy, bỏ qua chủ đề không đổi, trần lượt gọi AI mỗi ngày.",
    )
    parser.add_argument("--max-calls-per-day", type=int, default=30)
    parser.add_argument(
        "--rotate-after", type=int, default=8, help="Mở session mới sau N lượt của một chủ đề."
    )
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument(
        "--backtest-proposals",
        action="store_true",
        help="AI-3: sau khi lưu, backtest đề xuất mới (bản gốc vs bản đề xuất) và gửi thẻ.",
    )
    args = parser.parse_args(argv)
    if "live" in args.project.lower() or "prod" in args.project.lower():
        raise SystemExit("Script chỉ dành cho stack local PAPER.")
    if not args.scheduled:
        return _run(args, local_state=None)
    with RunLock(args.state_dir / "run.lock") as acquired:
        if not acquired:
            print(json.dumps({"skipped": "LOCKED"}))
            return 0
        return _run(args, local_state=args.state_dir / "state.json")


def _run(args: argparse.Namespace, *, local_state: Path | None) -> int:
    from datetime import UTC, datetime

    state: dict[str, Any] = {"workspace_id": args.workspace_id}
    saved = load_state(local_state) if local_state else {}
    today = datetime.now(UTC).date().isoformat()
    calls_today = int(saved.get("calls", {}).get(today, 0))
    remaining = None
    if local_state is not None:
        remaining = max(0, args.max_calls_per_day - calls_today)
        if remaining == 0:
            print(json.dumps({"skipped": "DAILY_CALL_CAP", "calls_today": calls_today}))
            return 0
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
    sessions = _phase(
        args.project,
        "migrator",
        "sessions",
        state | {"engine": engine.name, "rotate_after": args.rotate_after},
    )
    fingerprints = dict(saved.get("fingerprints", {})) if local_state else None
    records = analyze_topics(
        report,
        engine,
        sessions["sessions"],
        families=args.family,
        timeout=args.timeout,
        budget=args.max_budget_usd,
        skip_fingerprints=fingerprints,
        max_engine_calls=remaining,
    )
    sent = 0
    if records:
        stored = _phase(
            args.project,
            "migrator",
            "persist",
            state
            | {
                "records": [
                    {k: v for k, v in r.items() if k not in {"topic_report", "fingerprint"}}
                    for r in records
                ]
            },
        )
    if local_state is not None:
        # Lưu trạng thái ngay sau khi kết quả đã vào DB, trước bước gửi Telegram: lỗi gửi không
        # được làm lượt lịch sau phân tích lại (sự cố 16/11: hai bộ thẻ 02:28 và 02:29).
        for record in records:
            if record["valid"]:
                fingerprints[record["topic_key"]] = record["fingerprint"]  # type: ignore[index]
        calls = {today: calls_today + len(records)}
        save_state(local_state, {"fingerprints": fingerprints, "calls": calls})
    if records and args.send_telegram:
        sent = send_telegram(records, stored["chats"])
    backtest = (
        backtest_proposals(records, args.project, send=args.send_telegram)
        if args.backtest_proposals
        else None
    )
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
            {
                "report_hash": report["report_hash"],
                "runs": summary,
                "telegram_sent": sent,
                "proposal_backtest": backtest,
            },
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
            payload = dict(report.payload)
            # CRY-380: lưu report nguyên bản (trước khi gắn bối cảnh AI) — bất biến, idempotent.
            from trading_bot.application.run_report import persist_run_report

            async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                await persist_run_report(uow.session, workspace_id=workspace, payload=payload)
            # AI-4: lịch vĩ mô sắp tới làm bối cảnh; ngoài report_hash (Run Report giữ tất định),
            # nằm trong prompt nên được input_sha256 phủ; dựng lại được từ macro_calendar_fetch.
            from trading_bot.application.derivatives_state import load_derivatives_snapshots
            from trading_bot.application.listings_state import load_listing_fetches
            from trading_bot.application.macro_calendar_state import load_macro_fetches
            from trading_bot.domain.research.derivatives import derivatives_digest
            from trading_bot.domain.research.listings import listings_digest
            from trading_bot.domain.research.macro_calendar import macro_digest

            async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                fetches = await load_macro_fetches(uow.session, workspace_id=workspace, as_of=end)
                snapshots = await load_derivatives_snapshots(
                    uow.session, workspace_id=workspace, as_of=end
                )
                listing_fetches, last_checked = await load_listing_fetches(
                    uow.session, workspace_id=workspace, as_of=end
                )
            payload["macro_calendar"] = macro_digest(fetches, end)
            # AI-4 lát 2: phái sinh Binance; coin bot đã giao dịch trong cửa sổ đứng trước.
            traded = [
                symbol for bot in payload.get("bots", []) for symbol in (bot.get("by_symbol") or {})
            ]
            payload["derivatives"] = derivatives_digest(
                snapshots, end, priority_symbols=list(dict.fromkeys(traded))
            )
            # AI-4 lát 3: coin trong danh mục bot bị lên lịch gỡ/đổi trạng thái trên Binance.
            universe = [
                symbol
                for bot in payload.get("bots", [])
                for symbol in ((bot.get("config") or {}).get("universe") or ())
            ]
            payload["listings"] = listings_digest(
                listing_fetches,
                end,
                last_checked_at=last_checked,
                watch_symbols=list(dict.fromkeys([*traded, *universe])),
            )
            return payload
        if phase == "sessions":
            async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                rows = await uow.session.execute(
                    select(AnalystSessionRecord.topic_key, AnalystSessionRecord.session_id)
                    .where(
                        AnalystSessionRecord.workspace_id == workspace,
                        AnalystSessionRecord.engine == state["engine"],
                        AnalystSessionRecord.status == "ACTIVE",
                        # Xoay session: ngữ cảnh tích lũy làm mỗi lượt đắt dần (16/11: H03 vượt
                        # trần 1 USD/lượt sau ~7 lượt). Quá ngưỡng thì mở session mới.
                        AnalystSessionRecord.run_count < int(state.get("rotate_after", 8)),
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
