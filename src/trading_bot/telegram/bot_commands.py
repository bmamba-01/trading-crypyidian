"""Lệnh Telegram điều khiển bot (MB-07a, ruling 35–36; SEC-22/23/38).

Chỉ hỗ trợ `/bots`, `/bot status <id>`, `/bot pause <id>`, `/bot resume <id>` và `/report` (TG-3,
tổng hợp kết quả PAPER chỉ đọc). Mọi thao tác đi qua `BotOperationsService`/`BotReportingService`
(cùng service với Admin API); module này không mở kết nối/transaction DB.

- Lệnh đổi trạng thái luôn cần ID bot đầy đủ; thiếu ID → `BOT_TARGET_AMBIGUOUS` hoặc
  `BOT_TARGET_REQUIRED` kèm danh sách ID bot người dùng được đọc. ID lạ/sai định dạng →
  `BOT_NOT_FOUND`, không fallback.
- Telegram không có step-up MFA: principal Telegram mang `mfa_verified_at` nhỏ nhất nên `/bot
  resume` bị chính service từ chối `RECENT_MFA_REQUIRED` (hướng dẫn dùng Admin Web).
- Không có kill/release workspace, flatten, publish hay disable qua Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from trading_bot.application.bot_lifecycle_guard import BotNotFlatError
from trading_bot.application.bot_operations import (
    BotOperationsService,
    BotRuntimeStatus,
    PrincipalBotOperations,
)
from trading_bot.application.bot_reporting import BotReportError
from trading_bot.application.bot_runtime_control import BotRuntimeControlError
from trading_bot.application.telegram_trade_results import (
    PaperReportView,
    format_paper_report_text,
)
from trading_bot.auth.policy import Principal, Role

# Không có step-up qua Telegram: thời điểm MFA không bao giờ "gần đây" (ruling 35).
NO_MFA = datetime.min.replace(tzinfo=UTC)
MAX_COMMAND_LENGTH = 300
_FORBIDDEN_WORDS = frozenset(
    {"flatten", "flatten_all", "kill", "release", "publish", "disable", "enable", "workspace"}
)
_SUBCOMMANDS = {"status": "STATUS", "pause": "PAUSE", "resume": "RESUME"}


class BotCommand(StrEnum):
    LIST = "bots"
    STATUS = "status"
    PAUSE = "pause"
    RESUME = "resume"
    REPORT = "report"


class PaperReportProvider(Protocol):
    """Nguồn tin tổng hợp PAPER đã kiểm quyền theo principal (``TelegramPaperReportService``)."""

    async def build_view(self, principal: Principal) -> PaperReportView: ...


class BotDecisionInsightsProvider(Protocol):
    """Nguồn tường thuật quyết định & điều kiện còn thiếu (MI-2/MI-6) cho ``/bot status``."""

    async def get_latest_signal(self, principal: Principal, *, bot_id: UUID) -> dict[str, Any]: ...


class BotCommandParseError(ValueError):
    """Lệnh không hợp lệ hoặc không được phép qua Telegram; `code` là mã ổn định."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ParsedBotCommand:
    command: BotCommand
    bot_ref: str | None


@dataclass(frozen=True, slots=True)
class BotCommandResult:
    """Kết quả trả về người dùng Telegram; `message` tiếng Việt, không chứa secret."""

    ok: bool
    code: str
    message: str
    bot_ids: tuple[str, ...] = ()
    target_bot_id: UUID | None = None


def telegram_principal(
    *, user_id: UUID, membership_id: UUID, workspace_id: UUID, roles: frozenset[Role]
) -> Principal:
    """Principal của binding Telegram: cùng role/membership, không có MFA gần đây."""
    return Principal(
        user_id=user_id,
        membership_id=membership_id,
        workspace_id=workspace_id,
        roles=roles,
        mfa_verified_at=NO_MFA,
    )


def parse_bot_command(text: str) -> ParsedBotCommand:
    """Phân tích văn bản lệnh; bỏ hậu tố `@TênBot` của Telegram, không đoán lệnh."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_COMMAND_LENGTH:
        raise BotCommandParseError("TELEGRAM_COMMAND_INVALID")
    parts = text.split()
    head = parts[0]
    if not head.startswith("/"):
        raise BotCommandParseError("TELEGRAM_COMMAND_INVALID")
    name = head[1:].split("@", 1)[0].lower()
    args = [part.lower() if index == 0 else part for index, part in enumerate(parts[1:])]
    if name == "bots":
        if args:
            raise BotCommandParseError("TELEGRAM_COMMAND_INVALID")
        return ParsedBotCommand(BotCommand.LIST, None)
    if name == "report":
        if args:
            raise BotCommandParseError("TELEGRAM_COMMAND_INVALID")
        return ParsedBotCommand(BotCommand.REPORT, None)
    if name != "bot":
        raise BotCommandParseError("TELEGRAM_COMMAND_NOT_ALLOWED")
    if not args:
        raise BotCommandParseError("TELEGRAM_COMMAND_INVALID")
    sub = args[0]
    if sub not in _SUBCOMMANDS:
        code = (
            "TELEGRAM_COMMAND_NOT_ALLOWED"
            if sub in _FORBIDDEN_WORDS or sub.isidentifier()
            else "TELEGRAM_COMMAND_INVALID"
        )
        raise BotCommandParseError(code)
    if len(args) > 2:
        raise BotCommandParseError("TELEGRAM_COMMAND_INVALID")
    return ParsedBotCommand(BotCommand(sub), args[1] if len(args) == 2 else None)


_REJECT_MESSAGES = {
    "BOT_NOT_FOUND": "Không tìm thấy bot.",
    "BOT_TARGET_AMBIGUOUS": "Có nhiều bot; hãy chỉ rõ ID bot. Các bot bạn được xem:",
    "BOT_TARGET_REQUIRED": "Lệnh thay đổi trạng thái cần ID bot đầy đủ. Bot bạn được xem:",
    "RECENT_MFA_REQUIRED": (
        "Resume là thao tác tăng rủi ro, cần xác thực MFA gần đây: hãy dùng Admin Web."
    ),
    "CONTROL_REVISION_REQUIRED": "Resume cần revision hiện hành: hãy dùng Admin Web.",
    "CONTROL_REVISION_CONFLICT": "Trạng thái bot đã thay đổi; hãy kiểm tra lại.",
    "CONTROL_PERMISSION_DENIED": "Không đủ quyền cho thao tác này.",
    "RUNTIME_CONTROL_UNAVAILABLE": "Control plane tạm thời không phản hồi; hãy thử lại.",
    "TELEGRAM_COMMAND_NOT_ALLOWED": (
        "Lệnh này không được phép qua Telegram. "
        "Hỗ trợ: /bots, /report, /bot status|pause|resume <id>."
    ),
    "TELEGRAM_COMMAND_INVALID": "Cú pháp: /bots, /report hoặc /bot status|pause|resume <id>.",
    "REPORT_UNAVAILABLE": "Chưa lấy được tổng hợp PAPER; hãy thử lại hoặc xem Admin Web.",
}


def reject(code: str, bot_ids: tuple[str, ...] = ()) -> BotCommandResult:
    message = _REJECT_MESSAGES.get(code, "Thao tác bị từ chối.")
    if bot_ids:
        message = message + "\n" + "\n".join(bot_ids)
    return BotCommandResult(False, code, message, bot_ids)


def _status_line(status: BotRuntimeStatus) -> str:
    return (
        f"{status.bot_id} {status.name}: {status.status}, entry {status.entry_state.value}, "
        f"workspace {status.workspace_kill_state.value}, readiness {status.readiness or '-'}"
    )


def _parse_target(bot_id: UUID | str | None) -> UUID | None:
    if bot_id is None or isinstance(bot_id, UUID):
        return bot_id
    try:
        return UUID(bot_id) if len(bot_id) == 36 else None
    except ValueError:
        return None


class BotCommandHandler:
    """Thực thi lệnh đã phân tích bằng service vận hành dùng chung."""

    def __init__(
        self,
        operations: BotOperationsService,
        paper_report: PaperReportProvider | None = None,
        decision_insights: BotDecisionInsightsProvider | None = None,
    ) -> None:
        self._operations = operations
        self._paper_report = paper_report
        self._decision_insights = decision_insights

    async def handle_bot_command(
        self, principal: Principal, command: BotCommand, bot_id: UUID | str | None
    ) -> BotCommandResult:
        operations = self._operations.for_principal(principal)
        try:
            if command is BotCommand.LIST:
                bots = await operations.list_bots()
                ids = tuple(str(item.bot_id) for item in bots)
                lines = [_status_line(item) for item in bots] or ["Không có bot nào bạn được xem."]
                return BotCommandResult(True, "OK", "\n".join(lines), ids)
            if command is BotCommand.REPORT:
                return await self._report(principal)
            target = _parse_target(bot_id)
            if bot_id is not None and target is None:
                return reject("BOT_NOT_FOUND")
            if target is None:
                visible = tuple(str(item.bot_id) for item in await operations.list_bots())
                if command is BotCommand.STATUS and len(visible) == 1:
                    target = UUID(visible[0])
                elif len(visible) > 1:
                    return reject("BOT_TARGET_AMBIGUOUS", visible)
                elif command is BotCommand.STATUS:
                    return reject("BOT_NOT_FOUND")
                else:
                    return reject("BOT_TARGET_REQUIRED", visible)
            return await self._execute(
                operations,
                command,
                target,
                principal=principal,
                decision_insights=self._decision_insights,
            )
        except (BotRuntimeControlError, BotNotFlatError) as error:
            result = reject(error.reason_code)
            target = _parse_target(bot_id)
            return BotCommandResult(False, result.code, result.message, (), target)

    async def _report(self, principal: Principal) -> BotCommandResult:
        """Tổng hợp PAPER chỉ đọc; lỗi báo cáo không lộ chi tiết, chỉ trả mã ổn định."""
        if self._paper_report is None:
            return reject("REPORT_UNAVAILABLE")
        try:
            view = await self._paper_report.build_view(principal)
        except BotReportError, PermissionError, ValueError:
            return reject("REPORT_UNAVAILABLE")
        ids = tuple(str(row.bot_id) for row in view.rows)
        return BotCommandResult(True, "OK", format_paper_report_text(view), ids)

    @staticmethod
    async def _execute(
        operations: PrincipalBotOperations,
        command: BotCommand,
        target: UUID,
        *,
        principal: Principal | None = None,
        decision_insights: BotDecisionInsightsProvider | None = None,
    ) -> BotCommandResult:
        if command is BotCommand.STATUS:
            status = await operations.bot_status(target)
            text = _status_line(status)
            if decision_insights is not None and principal is not None:
                text = await _append_status_insights(text, decision_insights, principal, target)
            return BotCommandResult(True, "OK", text, (str(target),), target)
        if command is BotCommand.PAUSE:
            paused = await operations.pause_bot(target, "Lệnh Telegram /bot pause")
            note = "đã pause" if paused.changed else "đã ở trạng thái pause"
            return BotCommandResult(
                True,
                "OK",
                f"Bot {target} {note} (revision {paused.revision}); exits bảo vệ vẫn chạy.",
                (str(target),),
                target,
            )
        resumed = await operations.resume_bot(target, "Lệnh Telegram /bot resume", None)
        return BotCommandResult(
            True,
            "OK",
            f"Bot {target} đã resume (revision {resumed.revision}).",
            (str(target),),
            target,
        )


async def _append_status_insights(
    base_line: str,
    provider: BotDecisionInsightsProvider,
    principal: Principal,
    bot_id: UUID,
) -> str:
    """Bổ sung tường thuật quyết định (MI-6) và điều kiện còn thiếu (MI-2) vào ``/bot status``."""
    from trading_bot.application.market_narrative import (
        decision_narrative,
        format_decision_narrative_text,
    )
    from trading_bot.domain.strategy.setup_progress import setup_progress

    try:
        view = await provider.get_latest_signal(principal, bot_id=bot_id)
    except BotReportError, PermissionError, ValueError:
        return base_line
    latest = view.get("latest_decision")
    if not isinstance(latest, dict):
        return base_line
    setup = setup_progress(
        strategy_release_id=str(latest.get("strategy_release_id") or "—"),
        decisions=[latest],
        bonus=(),
        now=datetime.now(UTC),
    )
    narrative = format_decision_narrative_text(
        decision_narrative(latest, setup=setup, card=view.get("card")), max_sentences=2
    )
    return f"{base_line}\n{narrative}" if narrative else base_line
