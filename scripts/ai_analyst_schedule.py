"""Lịch tự chạy AI Analyst trên Mac bằng launchd (LaunchAgent người dùng, không cần quyền admin).

Owner yêu cầu 16/11/2026: phân tích AI tự chạy sau mỗi bản tổng kết 4 giờ. Tổng kết theo mốc UTC
00/04/08/12/16/20 giờ; job chạy **10 phút sau mỗi mốc** bằng chế độ ``--scheduled`` của
``scripts/run_ai_analyst.py`` (khóa chạy, bỏ qua chủ đề không đổi, trần lượt gọi AI/ngày) và gửi
thẻ "Phân tích AI" lên Telegram; có đề xuất mới thì backtest luôn (AI-3) và gửi thẻ so sánh.

Chạy trên host vì đăng nhập Claude CLI nằm trong Keychain của người dùng. Lệnh:

    uv run python scripts/ai_analyst_schedule.py install    # cài + nạp
    uv run python scripts/ai_analyst_schedule.py status
    uv run python scripts/ai_analyst_schedule.py uninstall  # gỡ hoàn toàn

Tài liệu launchd: ``man launchd.plist`` (StartCalendarInterval theo giờ địa phương của máy).
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.crypidian.ai-analyst"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = ROOT / "var" / "ai-analyst" / "launchd.log"
UTC_HOURS = (0, 4, 8, 12, 16, 20)
MINUTE = 10


def local_hours(utc_hours: tuple[int, ...] = UTC_HOURS) -> list[int]:
    """Đổi mốc UTC sang giờ địa phương của máy (launchd dùng giờ địa phương)."""
    offset = datetime.now().astimezone().utcoffset()
    hours_offset = int(offset.total_seconds() // 3600) if offset else 0
    return sorted((hour + hours_offset) % 24 for hour in utc_hours)


def build_plist(*, uv: str, claude: str | None, engine: str = "claude") -> dict[str, object]:
    command = (
        f"cd {_quote(str(ROOT))} && "
        + (f"CRYPIDIAN_CLAUDE_BIN={_quote(claude)} " if claude else "")
        + f"{_quote(uv)} run python scripts/run_ai_analyst.py --engine {engine} --hours 24 "
        "--scheduled --send-telegram --max-budget-usd 3 --backtest-proposals"
    )
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", command],
        "StartCalendarInterval": [{"Hour": hour, "Minute": MINUTE} for hour in local_hours()],
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "RunAtLoad": False,
        "ProcessType": "Background",
    }


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("install", "uninstall", "status"))
    parser.add_argument("--engine", choices=("claude", "codex"), default="claude")
    args = parser.parse_args(argv)
    domain = f"gui/{os.getuid()}"
    if args.command == "status":
        loaded = _launchctl("print", f"{domain}/{LABEL}").returncode == 0
        print(f"plist: {PLIST if PLIST.exists() else 'chưa cài'} · loaded: {loaded}")
        print(f"giờ chạy (địa phương): {local_hours()} phút {MINUTE}; log: {LOG}")
        return 0
    if args.command == "uninstall":
        _launchctl("bootout", f"{domain}/{LABEL}")
        PLIST.unlink(missing_ok=True)
        print("Đã gỡ lịch AI Analyst.")
        return 0
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("Không tìm thấy uv trên PATH.")
    claude = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    claude_bin = claude if Path(claude).exists() else None
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", f"{domain}/{LABEL}")
    with PLIST.open("wb") as handle:
        plistlib.dump(build_plist(uv=uv, claude=claude_bin, engine=args.engine), handle)
    loaded = _launchctl("bootstrap", domain, str(PLIST))
    if loaded.returncode != 0:
        print(loaded.stderr.strip(), file=sys.stderr)
        return loaded.returncode
    print(
        f"Đã cài {PLIST.name}: chạy lúc {local_hours()} giờ địa phương, phút {MINUTE} "
        f"(= {list(UTC_HOURS)} giờ UTC). Log: {LOG}. Giờ hiện tại UTC: "
        f"{datetime.now(UTC):%H:%M}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
