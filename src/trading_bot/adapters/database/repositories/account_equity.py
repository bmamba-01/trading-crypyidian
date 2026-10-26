"""Account equity repository cho PAPER account point-in-time.

MB-04 fix (review I2/I3): vốn account mục tiêu và aggregate workspace dùng **một** phép tính SQL
(`public.workspace_account_risk_state`, `SECURITY INVOKER` nên chạy dưới RLS của role gọi). Phép
tính gồm seed + realized đã chốt + PnL exit một phần + UPNL phần còn mở tại mark hợp lệ − chi phí
đã ghi; mark lấy nến đóng mới nhất và độ tươi đo theo `close_time` (thời điểm quan sát). Không còn
đường Python riêng có thể lệch với SQL (giá entry đầu tiên, bỏ qua exit một phần/chi phí).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.domain.risk.equity import (
    AccountEquitySnapshot,
    EquityReasonCode,
    EquityValidationError,
    PaperLedgerEquityBasis,
    mark_to_market_equity,
)
from trading_bot.domain.risk.workspace_budget import AccountRiskState

LEDGER_EQUITY_SOURCE = "paper_ledger_sql"
LEDGER_EQUITY_SOURCE_VERSION = "mb04-account-risk-state-v2"
_STATE_QUERY = text(
    "SELECT bot_id, account_id, is_enabled, seed_equity_usdt, realized_pnl_usdt, "
    "open_position_pnl_usdt, open_position_count, equity_usdt, has_unknown_equity, "
    "open_risk_usdt, pending_risk_usdt, has_unknown_exposure "
    "FROM public.workspace_account_risk_state(:workspace_id, :as_of, :max_mark_age_seconds)"
)


@dataclass(frozen=True, slots=True)
class AccountLedgerState:
    """Một hàng của `workspace_account_risk_state` (đã chuyển sang Decimal/UUID)."""

    bot_id: UUID
    account_id: UUID
    is_enabled: bool
    seed_equity_usdt: Decimal | None
    realized_pnl_usdt: Decimal
    open_position_pnl_usdt: Decimal
    open_position_count: int
    equity_usdt: Decimal | None
    has_unknown_equity: bool
    open_risk_usdt: Decimal
    pending_risk_usdt: Decimal
    has_unknown_exposure: bool

    def to_domain(self) -> AccountRiskState:
        return AccountRiskState(
            bot_id=str(self.bot_id),
            account_id=str(self.account_id),
            enabled=self.is_enabled,
            equity_usdt=self.equity_usdt,
            equity_known=not self.has_unknown_equity,
            open_risk_usdt=self.open_risk_usdt,
            pending_risk_usdt=self.pending_risk_usdt,
            exposure_known=not self.has_unknown_exposure,
        )


def mark_age_seconds(max_mark_age: timedelta) -> int:
    """Freshness policy phải là số giây nguyên dương (khớp tham số hàm SQL)."""
    if not isinstance(max_mark_age, timedelta) or max_mark_age <= timedelta(0):
        raise ValueError("max_mark_age phải là thời hạn dương.")
    seconds = int(max_mark_age.total_seconds())
    if seconds <= 0 or max_mark_age.total_seconds() != seconds:
        raise ValueError("max_mark_age phải được biểu diễn bằng giây nguyên dương.")
    return seconds


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Giá trị numeric từ ledger SQL không hữu hạn.")
    return result


class AccountEquityRepository:
    """Nạp vốn/exposure point-in-time từ ledger SQL dùng chung; fail-closed khi thiếu dữ kiện."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_account_states(
        self, *, workspace_id: UUID, as_of: datetime, max_mark_age: timedelta
    ) -> list[AccountLedgerState]:
        """Các account nhìn thấy dưới RLS của role hiện tại, tính tại `as_of`."""
        _validate_aware(as_of, "as_of")
        result = await self._session.execute(
            _STATE_QUERY,
            {
                "workspace_id": workspace_id,
                "as_of": as_of,
                "max_mark_age_seconds": mark_age_seconds(max_mark_age),
            },
        )
        states: list[AccountLedgerState] = []
        for row in result.all():
            states.append(
                AccountLedgerState(
                    bot_id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
                    account_id=row[1] if isinstance(row[1], UUID) else UUID(str(row[1])),
                    is_enabled=bool(row[2]),
                    seed_equity_usdt=_decimal(row[3]),
                    realized_pnl_usdt=_decimal(row[4]) or Decimal("0"),
                    open_position_pnl_usdt=_decimal(row[5]) or Decimal("0"),
                    open_position_count=int(row[6]),
                    equity_usdt=_decimal(row[7]),
                    has_unknown_equity=bool(row[8]),
                    open_risk_usdt=_decimal(row[9]) or Decimal("0"),
                    pending_risk_usdt=_decimal(row[10]) or Decimal("0"),
                    has_unknown_exposure=bool(row[11]),
                )
            )
        return states

    async def load_paper_equity_snapshot(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        as_of: datetime,
        max_equity_age: timedelta,
        max_mark_age: timedelta,
    ) -> AccountEquitySnapshot:
        """Snapshot vốn của đúng bot/account mục tiêu từ cùng phép tính với aggregate."""
        _validate_aware(as_of, "as_of")
        states = await self.load_account_states(
            workspace_id=workspace_id, as_of=as_of, max_mark_age=max_mark_age
        )
        state = next(
            (item for item in states if item.account_id == account_id and item.bot_id == bot_id),
            None,
        )
        basis = PaperLedgerEquityBasis(
            workspace_id=str(workspace_id),
            bot_id=str(bot_id),
            account_id=str(account_id),
            asset="USDT",
            as_of=as_of,
            captured_at=as_of,
            available_at=as_of,
            source=LEDGER_EQUITY_SOURCE,
            source_version=LEDGER_EQUITY_SOURCE_VERSION,
            max_mark_age=max_mark_age,
            max_equity_age=max_equity_age,
            initial_capital_before_realized_pnl=(
                state.seed_equity_usdt
                if state is not None and state.seed_equity_usdt is not None
                else Decimal("0")
            ),
            cumulative_realized_pnl=state.realized_pnl_usdt if state else Decimal("0"),
            open_position_pnl=state.open_position_pnl_usdt if state else Decimal("0"),
            open_position_count=state.open_position_count if state else 0,
        )
        if state is None or state.seed_equity_usdt is None:
            return _unknown_snapshot(basis, EquityReasonCode.BASIS_UNKNOWN)
        if state.has_unknown_equity or state.equity_usdt is None:
            return _unknown_snapshot(basis, EquityReasonCode.SNAPSHOT_UNKNOWN)
        try:
            snapshot = mark_to_market_equity(basis, (), {}, as_of)
        except EquityValidationError as error:
            return _unknown_snapshot(basis, error.reason_code)
        if snapshot.equity_amount != state.equity_usdt:
            # Hai cách cộng cùng thành phần phải khớp tuyệt đối; lệch nghĩa là lineage hỏng.
            return _unknown_snapshot(basis, EquityReasonCode.LINEAGE_INVALID)
        return snapshot


def _validate_aware(dt: datetime, name: str) -> None:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError(f"{name} phải là datetime nhận biết múi giờ UTC.")


def _unknown_snapshot(
    basis: PaperLedgerEquityBasis, reason: EquityReasonCode
) -> AccountEquitySnapshot:
    return AccountEquitySnapshot(
        workspace_id=basis.workspace_id,
        bot_id=basis.bot_id,
        account_id=basis.account_id,
        asset=basis.asset,
        equity_amount=Decimal("0"),
        equity_basis="PAPER",
        seed_capital=basis.initial_capital_before_realized_pnl,
        cumulative_realized_pnl=basis.cumulative_realized_pnl,
        unrealized_pnl=Decimal("0"),
        captured_at=basis.captured_at,
        as_of=basis.as_of,
        available_at=basis.available_at,
        source=basis.source,
        source_version=basis.source_version,
        source_hash="",
        mark_refs=(),
        basis=basis,
        positions=(),
        known=False,
        unknown_reason=reason.value,
    )
