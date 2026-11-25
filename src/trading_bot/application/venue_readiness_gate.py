"""Cổng readiness startup/reconnect cho venue (WP-03D / Task 7 / CRY-294).

Đóng mặc định cho tới khi đối soát trả `MATCHED`, chính sách rủi ro còn hiệu lực
và không còn `account_activity_incident` nào đang `OPEN`.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_bot.domain.execution.venue_reconciliation import (
    VenueReconcileDecision,
    VenueReconcileOutcome,
)


@dataclass(slots=True)
class VenueStartupReadinessGate:
    """Trạng thái cổng mở lệnh (entry gate) của runtime venue."""

    is_open: bool = False
    reason_code: str = "STARTUP_RECONCILIATION_REQUIRED"

    def evaluate(
        self,
        *,
        reconcile: VenueReconcileDecision,
        risk_policy_approved: bool,
        open_incidents_count: int,
    ) -> bool:
        """Cập nhật trạng thái cổng theo nguyên tắc fail-closed."""
        if not risk_policy_approved:
            self.is_open = False
            self.reason_code = "RISK_POLICY_NOT_APPROVED"
            return False
        if open_incidents_count > 0:
            self.is_open = False
            self.reason_code = "OPEN_ACTIVITY_INCIDENTS_EXIST"
            return False
        if reconcile.outcome is not VenueReconcileOutcome.MATCHED or not reconcile.entry_allowed:
            self.is_open = False
            self.reason_code = reconcile.reason_code
            return False
        self.is_open = True
        self.reason_code = "VENUE_READY"
        return True
