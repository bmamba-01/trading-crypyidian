"""Gieo ledger PAPER kiểm thử bằng cách chạy mẫu Strategy Card L0–L6 qua đường runtime thật.

Chỉ dùng cho stack Docker Compose local ở chế độ PAPER. Script không gọi Binance, không tạo
lệnh sàn và không đổi registry chiến lược của sản phẩm; release của Strategy Card chỉ tồn tại
trong tiến trình probe (xem `tests/e2e/probes/pipeline_paper_seed.py`).

Ví dụ (stack đang chạy với `-p crypidian-review`):

    uv run python scripts/seed_pipeline_paper_ledger.py \\
        --project crypidian-review --login-email owner@crypidian.local

Kết quả: một workspace PAPER mới, hai bot dùng release của Strategy Card, các giao dịch đã đóng
theo TAKE_PROFIT / STOP_LOSS / TIME_STOP và một vị thế đang mở. Tài khoản `--login-email` được
thêm làm owner của workspace đó để đăng nhập bằng slug in ra ở cuối.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "e2e" / "probes" / "multibot_runtime.py"
EXTENSION = ROOT / "tests" / "e2e" / "probes" / "pipeline_paper_seed.py"
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(MB05B_PHASE=p['phase'],MB05B_STATE=json.dumps(p.get('state',{})));"
    "exec(compile(p['code'],'multibot_runtime.py','exec'),{'__name__':'__main__'})"
)
# Mỗi vòng là một evidence được fan-out tới cả hai bot: A dùng thẻ LONG, B dùng thẻ SHORT.
DEFAULT_ROUNDS: tuple[dict[str, str], ...] = (
    {"a": "TAKE_PROFIT", "b": "STOP_LOSS"},
    {"a": "STOP_LOSS", "b": "TAKE_PROFIT"},
    {"a": "TIME_STOP", "b": "TAKE_PROFIT"},
    {"a": "TAKE_PROFIT", "b": "TIME_STOP"},
    {"a": "OPEN", "b": "STOP_LOSS"},
)


def _probe_code() -> str:
    """Ghép phần mở rộng vào probe gốc ngay trước lời gọi `_main()` như harness E2E vẫn làm."""
    base = PROBE.read_text(encoding="utf-8")
    marker = '\nif __name__ == "__main__":\n    _main()\n'
    if base.count(marker) != 1:
        raise SystemExit("Không tìm thấy điểm ghép `_main()` trong probe multibot_runtime.")
    return base.replace(marker, "\n" + EXTENSION.read_text(encoding="utf-8") + marker)


def _run_phase(project: str, phase: str, service: str, state: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({"phase": phase, "state": state, "code": _probe_code()})
    command = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "-p",
        project,
        "-f",
        str(ROOT / "compose.yaml"),
        "run",
        "--rm",
        "--no-deps",
        "-T",
        service,
        "python",
        "-c",
        PROBE_BOOTSTRAP,
    ]
    result = subprocess.run(  # noqa: S603 - lệnh cố định, không nhận chuỗi shell từ người dùng
        command, input=payload, capture_output=True, text=True, cwd=ROOT, check=False
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr[-4000:])
        raise SystemExit(f"Pha {phase} thất bại (mã {result.returncode}).")
    traces = [line for line in result.stderr.splitlines() if "[trace]" in line]
    if traces:
        sys.stderr.write("\n".join(traces[-20:]) + "\n")
    parsed: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return parsed


def _attach_login(project: str, state: dict[str, Any]) -> None:
    """Login cần TOTP theo từng workspace: mã hóa lại cùng khóa TOTP cho workspace mới."""
    state |= _run_phase(project, "pipeline_login_lookup", "migrator", state)
    state |= _run_phase(project, "pipeline_login_rewrap", "admin-web", state)
    state |= _run_phase(project, "pipeline_login_attach", "migrator", state)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True, help="Tên project Docker Compose đang chạy.")
    parser.add_argument(
        "--login-email",
        required=True,
        help="Email (đã chuẩn hóa) của tài khoản sẽ được thêm làm owner của workspace mới.",
    )
    parser.add_argument(
        "--workspace-slug",
        default=f"crypidian-pipeline-{datetime.now(UTC):%m%d%H%M}",
        help="Slug đăng nhập của workspace mới.",
    )
    parser.add_argument("--debug", action="store_true", help="Ghi lý do khi không tạo được intent.")
    parser.add_argument("--rounds", type=int, default=len(DEFAULT_ROUNDS), help="Số vòng evidence.")
    parser.add_argument(
        "--attach-login-only",
        action="store_true",
        help="Chỉ cấp factor TOTP của tài khoản cho workspace đã có (không gieo thêm dữ liệu).",
    )
    parser.add_argument(
        "--chart-bars-only",
        action="store_true",
        help="Chỉ bổ sung nến biểu đồ nội bộ cho workspace đã có (chạy lại được nhiều lần).",
    )
    args = parser.parse_args()
    if "live" in args.project.lower() or "prod" in args.project.lower():
        raise SystemExit("Script chỉ dành cho stack local PAPER.")

    state: dict[str, Any] = {
        "variants": {"main": "1"},
        "pipeline_login_email": args.login_email.strip().lower(),
        "pipeline_workspace_slug": args.workspace_slug,
        "pipeline_scenarios": list(DEFAULT_ROUNDS[: args.rounds]),
        "pipeline_debug": args.debug,
    }
    if args.chart_bars_only:
        bars = _run_phase(args.project, "pipeline_seed_chart_bars", "migrator", state)
        print(json.dumps({"workspace_slug": args.workspace_slug, **bars}))
        return
    if args.attach_login_only:
        _attach_login(args.project, state)
        print(json.dumps({"workspace_slug": args.workspace_slug, "login": "attached"}))
        return
    state |= _run_phase(args.project, "pipeline_seed_setup", "migrator", state)
    _attach_login(args.project, state)
    _run_phase(args.project, "genesis", "trading-runtime", state)
    trades = _run_phase(args.project, "pipeline_seed_trades", "trading-runtime", state)
    bars = _run_phase(args.project, "pipeline_seed_chart_bars", "migrator", state)

    workspace = state["workspaces"]["main"]
    summary = {
        "workspace_slug": state["pipeline_workspace_slug"],
        "workspace_id": workspace["workspace"],
        "bots": workspace["bots"],
        "releases": state["pipeline_release_directions"],
        "chart_bars": bars["pipeline_chart_bars"],
        "trades": trades["pipeline_trades"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
