"""MB-06 E2E: báo cáo theo bot và tổng hợp workspace qua Admin API thật trên Compose.

Dataset QA (bot A/B cùng workspace `qa`, bot OTHER ở `qa_other`) + trade tạo bằng
`PaperLedgerRepository` thật dưới role runtime. Kiểm: mỗi báo cáo bot chỉ có trade của bot
đó; tổng hợp workspace = A + B = SQL ledger độc lập (Decimal chính xác); ma trận quyền theo
role/grant QA (404 không phân biệt); RLS chặn workspace khác; báo cáo không ghi ledger.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyotp
import pytest
from scripts.docker_image import build_image_reference, ensure_image_available
from tests.fixtures.qa_dataset import build_qa_dataset

ROOT = Path(__file__).parents[2]
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(MB06_PHASE=p['phase'],MB06_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'multibot_reports.py','exec'),{'__name__':'__main__'})"
)
NO_COST = {"entry_fee": "0", "entry_slippage": "0", "exit_fee": "0", "exit_slippage": "0"}
# Offset (giây) so với mốc `base` probe trả về; cửa sổ báo cáo [base, base + 600s).
TRADES: list[dict[str, Any]] = [
    {
        "label": "A1",
        "account": "A",
        "release": "h03-v1",
        "quantity": "1",
        "entry_price": "100",
        "exit_price": "120",
        "gross": "20",
        "entry_fee": "0.05",
        "entry_slippage": "0.01",
        "exit_fee": "0.06",
        "exit_slippage": "0.02",
        "opened_offset": 10,
        "closed_offset": 60,
    },
    {
        "label": "A2",
        "account": "A",
        "release": "h03-v1",
        "quantity": "1",
        "entry_price": "100",
        "exit_price": "92.5",
        "gross": "-7.5",
        "entry_fee": "0.05",
        "entry_slippage": "0.01",
        "exit_fee": "0.04",
        "exit_slippage": "0.01",
        "funding": "0.25",
        "opened_offset": 70,
        "closed_offset": 180,
    },
    # Đóng đúng mốc `end`: thuộc cửa sổ kế tiếp (khoảng nửa mở).
    {
        "label": "A3",
        "account": "A",
        "release": "h03-v1",
        "quantity": "1",
        "entry_price": "100",
        "exit_price": "105",
        "gross": "5",
        **NO_COST,
        "opened_offset": 200,
        "closed_offset": 600,
    },
    # Còn mở tại `end`: chỉ liệt kê định danh, không tính PnL.
    {
        "label": "A4",
        "account": "A",
        "release": "h03-v1",
        "quantity": "1",
        "entry_price": "100",
        **NO_COST,
        "opened_offset": 120,
        "closed_offset": None,
    },
    {
        "label": "B1",
        "account": "B",
        "release": "h01-v1",
        "quantity": "2",
        "entry_price": "50",
        "exit_price": "51.65",
        "gross": "3.3",
        "entry_fee": "0.02",
        "entry_slippage": "0",
        "exit_fee": "0.02",
        "exit_slippage": "0",
        "funding": "-0.1",
        "opened_offset": 30,
        "closed_offset": 120,
    },
    {
        "label": "B2",
        "account": "B",
        "release": "h01-v1",
        "quantity": "1",
        "entry_price": "50",
        "exit_price": "48.9",
        "gross": "-1.1",
        "entry_fee": "0.01",
        "entry_slippage": "0.01",
        "exit_fee": "0.01",
        "exit_slippage": "0.01",
        "opened_offset": 130,
        "closed_offset": 240,
    },
]
MEMBERS = (
    "qa.owner",
    "qa.strategy_admin",
    "qa.risk_admin",
    "qa.operator",
    "qa.analyst_reviewer",
    "qa_other.owner",
)


def _login(port: int, account: dict[str, Any]) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = json.dumps(
        {
            "workspace_slug": account["workspace_slug"],
            "email": account["email"],
            "password": account["password"],
            "code": pyotp.TOTP(str(account["totp_secret"])).now(),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=20) as response:
        assert response.status == 200
    return opener


def _get(opener: urllib.request.OpenerDirector, port: int, params: dict[str, str]):
    url = f"http://127.0.0.1:{port}/api/v1/reports?{urllib.parse.urlencode(params)}"
    try:
        with opener.open(url, timeout=30) as response:
            return response.status, json.loads(response.read()), response.headers
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}"), error.headers


def test_bot_reports_isolate_bots_and_workspace_aggregate_reconciles() -> None:
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=ROOT,
        capture_output=True,
    )
    project = f"crypidian-reports-e2e-{uuid.uuid4().hex[:10]}"
    credentials_file = ROOT / ".secrets" / f"qa_accounts-{project}.json"
    image = build_image_reference(ROOT)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image, "TRADING_BOT_WEB_PORT": str(port)}
    base = ["docker", "compose", "--profile", "paper", "--project-name", project]
    base += ["-f", "compose.yaml"]

    def run(command: list[str], *, stdin: str | None = None, timeout: int = 300):
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        assert result.returncode == 0, (command[:8], result.stdout[-5000:], result.stderr[-5000:])
        return result

    dataset = build_qa_dataset()
    state: dict[str, Any] = {
        "workspaces": {workspace.key: str(workspace.id) for workspace in dataset.workspaces},
        "accounts": {account.key: str(account.id) for account in dataset.accounts},
        "members": {member.key: str(member.membership_id) for member in dataset.members},
        "trades": TRADES,
    }
    probe_code = (ROOT / "tests/e2e/probes/multibot_reports.py").read_text(encoding="utf-8")

    def probe(phase: str, service: str) -> dict[str, Any]:
        payload = json.dumps({"phase": phase, "state": state, "code": probe_code})
        result = run(
            base + ["run", "--rm", "--no-deps", "-T", service, "python", "-c", PROBE_BOOTSTRAP],
            stdin=payload,
        )
        parsed: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
        return parsed

    info = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if info.returncode or not info.stdout.strip():
        pytest.fail(f"Docker daemon cần chạy cho E2E Compose: {info.stderr[-500:]}")
    ensure_image_available(ROOT, image)
    try:
        run(base + ["up", "--detach", "--wait", "--wait-timeout", "180"])
        run(base + ["run", "--rm", "--no-deps", "migrator", "alembic", "check"])
        seeded = run(
            [
                sys.executable,
                "scripts/seed_qa_data.py",
                "seed",
                "--local-only",
                "--project-name",
                project,
                "--credentials-file",
                str(credentials_file),
                "--json",
            ],
            timeout=900,
        )
        state["bots"] = json.loads(seeded.stdout.strip().splitlines()[-1])["bots"]
        bot_a, bot_b = state["bots"]["A"], state["bots"]["B"]
        state["prepared"] = probe("prepare", "migrator")

        ledger = probe("ledger", "trading-runtime")
        assert ledger["identity"] == "crypidian_trading_runtime"
        trades = ledger["trades"]
        assert {label: item["bot_id"] for label, item in trades.items()} == {
            spec["label"]: state["bots"][spec["account"]] for spec in TRADES
        }
        start = datetime.fromisoformat(ledger["base"])
        end = start + timedelta(seconds=600)
        state["window"] = {"start": start.isoformat(), "end": end.isoformat()}
        window = {"start": state["window"]["start"], "end": state["window"]["end"]}
        oracle_before = probe("oracle", "migrator")
        per_bot = oracle_before["per_bot"]
        assert per_bot[bot_a]["count"] == 2
        assert per_bot[bot_b]["count"] == 2
        assert Decimal(per_bot[bot_a]["net_pnl"]) == Decimal("12")
        assert Decimal(per_bot[bot_b]["net_pnl"]) == Decimal("2.22")

        credentials = json.loads(credentials_file.read_text(encoding="utf-8"))["accounts"]
        sessions = {key: _login(port, credentials[key]) for key in MEMBERS}

        def report(member: str, **params: str):
            return _get(sessions[member], port, {**window, **params})

        # (1) Báo cáo bot A/B chỉ chứa trade của đúng bot, đối soát SQL ledger chính xác.
        prepared = state["prepared"]
        bot_reports = {}
        for key, bot_id, labels, release in (
            ("A", bot_a, {"A1", "A2"}, "h03-v1"),
            ("B", bot_b, {"B1", "B2"}, "h01-v1"),
        ):
            status, body, headers = report("qa.owner", scope="bot", bot_id=bot_id)
            assert status == 200, body
            assert headers["Cache-Control"] == "no-store"
            data = body["report"]
            bot_reports[key] = data
            assert data["scope"] == "bot"
            assert data["bot_id"] == bot_id
            assert {row["trade_id"] for row in data["trades"]} == {
                trades[label]["trade_id"] for label in labels
            }
            assert {row["bot_id"] for row in data["trades"]} == {bot_id}
            usdt = data["totals"]["USDT"]
            expected = per_bot[bot_id]
            assert usdt["closed_trade_count"] == expected["count"]
            for field in (
                "net_pnl",
                "gross_pnl",
                "total_cost",
                "fee_total",
                "funding_total",
                "slippage_total",
            ):
                assert Decimal(usdt[field]) == Decimal(expected[field]), field
            assert data["lineage"]["strategy_releases"] == [
                {
                    "id": prepared["releases"][release]["id"],
                    "hash": prepared["releases"][release]["content_hash"],
                }
            ]
            assert data["lineage"]["bot_config_versions"] == [
                {
                    "id": prepared["configs"][bot_id]["id"],
                    "hash": prepared["configs"][bot_id]["hash"],
                }
            ]
            assert data["lineage"]["settings_version_ids"] == [prepared["settings"]["id"]]
            assert data["lineage"]["trades_without_bot_config_lineage"] == 0
            assert len(data["report_hash"]) == 64
        # Mở tại `end` (R2): A4 chưa đóng và A3 đóng đúng lúc `end`; sắp theo opened_at.
        assert [row["trade_id"] for row in bot_reports["A"]["open_trades"]] == [
            trades["A4"]["trade_id"],
            trades["A3"]["trade_id"],
        ]
        assert bot_reports["B"]["open_trades"] == []
        # A1: 4 dòng cost (fee/slippage entry+exit); A2: 4 + funding.
        assert bot_reports["A"]["reconciliation"] == {"closed_trade_rows": 2, "trade_cost_rows": 9}
        assert Decimal(bot_reports["A"]["totals"]["USDT"]["max_drawdown"]) == Decimal("7.86")
        assert (
            bot_reports["A"]["totals"]["USDT"]["drawdown_trough_trade_id"]
            == (trades["A2"]["trade_id"])
        )

        # (2) Tất định: gọi lại và principal khác cho cùng hash.
        for member in ("qa.owner", "qa.strategy_admin", "qa.risk_admin", "qa.analyst_reviewer"):
            status, body, _ = report(member, scope="bot", bot_id=bot_a)
            assert status == 200, (member, body)
            assert body["report"]["report_hash"] == bot_reports["A"]["report_hash"]

        # (3) Tổng hợp workspace tường minh = A + B = ledger SQL; role workspace cho cùng hash.
        hashes = set()
        for member in ("qa.owner", "qa.strategy_admin", "qa.risk_admin"):
            status, body, _ = report(member, scope="workspace")
            assert status == 200, (member, body)
            aggregate = body["report"]
            hashes.add(aggregate["report_hash"])
        assert len(hashes) == 1
        usdt = aggregate["totals"]["USDT"]
        assert usdt["closed_trade_count"] == oracle_before["workspace"]["count"] == 4
        assert Decimal(usdt["net_pnl"]) == Decimal(oracle_before["workspace"]["net_pnl"])
        assert Decimal(usdt["net_pnl"]) == Decimal(per_bot[bot_a]["net_pnl"]) + Decimal(
            per_bot[bot_b]["net_pnl"]
        )
        by_bot = {row["bot_id"]: row["report_hash"] for row in aggregate["bots"]}
        assert by_bot[bot_a] == bot_reports["A"]["report_hash"]
        assert by_bot[bot_b] == bot_reports["B"]["report_hash"]
        assert state["bots"]["OTHER"] not in by_bot

        # (4) Ma trận quyền QA: 404 như nhau cho chưa grant/không tồn tại/workspace khác.
        not_found = []
        for member, bot_id in (
            ("qa.analyst_reviewer", bot_b),
            ("qa.operator", bot_a),  # có bot:read/pause nhưng không có bot:report:read
            ("qa.operator", bot_b),
            ("qa_other.owner", bot_a),
            ("qa.owner", str(uuid.uuid4())),
            ("qa.owner", state["bots"]["OTHER"]),
        ):
            status, body, _ = report(member, scope="bot", bot_id=bot_id)
            assert status == 404, (member, bot_id, body)
            not_found.append(body)
        assert all(body == not_found[0] for body in not_found)
        assert not_found[0]["detail"]["code"] == "REPORT_NOT_FOUND"
        for member in ("qa.analyst_reviewer", "qa.operator"):
            status, body, _ = report(member, scope="workspace")
            assert status == 403, (member, body)
            assert body["detail"]["code"] == "WORKSPACE_REPORT_FORBIDDEN"
        status, body, _ = report("qa_other.owner", scope="workspace")
        assert status == 200, body
        assert body["report"]["workspace_id"] == state["workspaces"]["qa_other"]
        assert body["report"]["totals"] == {}

        # (5) Không có đường mặc định gộp bot.
        for params, code in (
            ({}, "REPORT_SCOPE_REQUIRED"),
            ({"bot_id": bot_a}, "REPORT_SCOPE_REQUIRED"),
            ({"scope": "bot"}, "BOT_ID_REQUIRED"),
            ({"scope": "workspace", "bot_id": bot_a}, "BOT_ID_NOT_ALLOWED"),
        ):
            status, body, _ = report("qa.owner", **params)
            assert status == 422, (params, body)
            assert body["detail"]["code"] == code

        # (6) Báo cáo chỉ đọc: ledger không đổi sau mọi lời gọi.
        assert probe("oracle", "migrator") == oracle_before

        # (7) RLS dưới crypidian_admin_web: workspace khác/membership giả không thấy trade.
        rls = probe("rls", "admin-web")
        assert rls["identity"] == "crypidian_admin_web"
        assert rls["other_owner@qa_other"] == {"trades": {}, "costs": 0}
        assert rls["other_owner@qa"] == {"trades": {}, "costs": 0}
        assert rls["qa_owner@qa"]["trades"] == {bot_a: 4, bot_b: 2}
        assert rls["qa_analyst@qa"]["trades"] == {bot_a: 4}
        # Operator thấy hàng bot A ở RLS (có grant bất kỳ) nhưng API vẫn 404 ở (4):
        # quyền theo hành động do ứng dụng kiểm, RLS chỉ là lớp scope.
        assert rls["qa_operator@qa"]["trades"] == {bot_a: 4}
        assert rls["repository_forged"] == {"closed": 0, "open": 0, "totals": {}}
        owner_view = rls["repository_owner"]
        assert (owner_view["closed"], owner_view["open"]) == (2, 2)
        assert owner_view["totals"]["USDT"][0] == 4
        assert Decimal(owner_view["totals"]["USDT"][1]) == Decimal("14.22")
    finally:
        subprocess.run(
            base + ["--profile", "qa", "down", "--volumes", "--remove-orphans"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
        credentials_file.unlink(missing_ok=True)
