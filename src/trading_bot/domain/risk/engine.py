"""Risk gate thuần áp hard cap lên TradePlan trước bước đặt lệnh."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_bot.domain.risk.planner import (
    OpenExposure,
    PlannerSettings,
    TradePlan,
    calculate_plan_hash,
)

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class RiskState:
    """Ảnh chụp point-in-time gồm vị thế mở và reservation đã tồn tại."""

    workspace_id: str
    account_id: str
    settings_version_id: str
    risk_profile_version_id: str
    evaluated_at: datetime
    account_captured_at: datetime
    market_data_as_of: datetime
    equity: Decimal
    available_margin: Decimal
    open_exposures: tuple[OpenExposure, ...]
    realized_loss_today: Decimal
    realized_loss_rolling: Decimal
    open_position_count: int
    is_known: bool
    pending_reservation_initial_margin: Decimal
    scored_plan_hash: str | None = None
    # RiskEngine nhận pending đã lưu; reservation book cộng thêm lượt giữ trong instance của nó.
    pending_reservation_risk: Decimal = ZERO
    pending_reservation_count: int = 0
    pending_reservation_notional: Decimal = ZERO
    pending_symbol_reservation_notional: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Quyết định có lineage và ngân sách để reserve an toàn."""

    approved: bool
    reason_code: str
    detail_vi: str
    plan_hash: str
    workspace_id: str
    account_id: str
    settings_version_id: str
    risk_profile_version_id: str
    requested_risk: Decimal
    open_risk: Decimal
    max_aggregate_open_risk: Decimal
    candidate_expires_at: datetime
    release_content_hash: str = ""
    snapshot_id: str = ""
    open_position_count: int = 0
    max_positions: int = 1_000_000
    pending_reservation_count: int = 0
    pending_reservation_risk: Decimal = ZERO
    symbol: str = ""
    requested_notional: Decimal = ZERO
    open_notional: Decimal = ZERO
    symbol_open_notional: Decimal = ZERO
    max_notional: Decimal = ZERO
    max_symbol_notional: Decimal = ZERO
    pending_reservation_notional: Decimal = ZERO
    pending_symbol_reservation_notional: Decimal = ZERO
    available_margin: Decimal = ZERO
    requested_initial_margin: Decimal = ZERO
    pending_reservation_initial_margin: Decimal = ZERO


class RiskEngine:
    """Từ chối theo fail-closed; candidate và model không chỉnh hard cap."""

    def evaluate(
        self, plan: TradePlan, state: RiskState, settings: PlannerSettings
    ) -> RiskDecision:
        """Đối chiếu plan với version, freshness và mọi hard limit đang có."""
        open_risk = (
            sum((item.risk_amount for item in state.open_exposures), ZERO)
            + state.pending_reservation_risk
        )
        open_notional = sum((item.notional for item in state.open_exposures), ZERO)
        open_notional += state.pending_reservation_notional
        symbol_open_notional = (
            sum(
                (item.notional for item in state.open_exposures if item.symbol == plan.symbol), ZERO
            )
            + state.pending_symbol_reservation_notional
        )
        concentration_cap = min(
            settings.limits.max_symbol_notional,
            state.equity * settings.limits.max_concentration_fraction,
        )
        common: dict[str, Any] = {
            "plan_hash": plan.plan_hash,
            "workspace_id": state.workspace_id,
            "account_id": state.account_id,
            "settings_version_id": settings.settings_version_id,
            "risk_profile_version_id": settings.risk_profile_version_id,
            "requested_risk": plan.estimated_loss,
            "open_risk": open_risk,
            "max_aggregate_open_risk": settings.limits.max_aggregate_open_risk,
            "candidate_expires_at": plan.expires_at,
            "release_content_hash": plan.release_content_hash,
            "snapshot_id": plan.snapshot_id,
            "open_position_count": state.open_position_count,
            "max_positions": settings.limits.max_positions,
            "pending_reservation_count": state.pending_reservation_count,
            "pending_reservation_risk": state.pending_reservation_risk,
            "symbol": plan.symbol,
            "requested_notional": plan.notional,
            "open_notional": open_notional,
            "symbol_open_notional": symbol_open_notional,
            "max_notional": settings.limits.max_notional,
            "max_symbol_notional": concentration_cap,
            "pending_reservation_notional": state.pending_reservation_notional,
            "pending_symbol_reservation_notional": state.pending_symbol_reservation_notional,
            "available_margin": state.available_margin,
            "requested_initial_margin": plan.initial_margin,
            "pending_reservation_initial_margin": state.pending_reservation_initial_margin,
        }

        def reject(code: str, detail: str) -> RiskDecision:
            return RiskDecision(False, code, detail, **common)

        def approve() -> RiskDecision:
            return RiskDecision(True, "APPROVED", "Kế hoạch nằm trong mọi giới hạn cứng.", **common)

        if not settings.published:
            return reject("SETTINGS_UNPUBLISHED", "Cấu hình chưa được publish.")
        if not state.is_known:
            return reject("RISK_STATE_UNKNOWN", "Chưa xác nhận được đầy đủ trạng thái tài khoản.")
        for value in (state.evaluated_at, state.account_captured_at, state.market_data_as_of):
            if value.tzinfo is None or value.utcoffset() is None:
                return reject("RISK_TIMESTAMP_INVALID", "Mốc thời gian risk phải có múi giờ.")
        if plan.expires_at.tzinfo is None or plan.expires_at.utcoffset() is None:
            return reject("RISK_TIMESTAMP_INVALID", "Mốc hết hạn plan phải có múi giờ.")
        if state.evaluated_at >= plan.expires_at:
            return reject("CANDIDATE_EXPIRED", "Candidate đã hết hạn tại thời điểm đánh giá.")
        if (
            plan.workspace_id != state.workspace_id
            or plan.account_id != state.account_id
            or settings.workspace_id != state.workspace_id
        ):
            return reject(
                "RISK_SCOPE_MISMATCH", "Kế hoạch, tài khoản và cấu hình khác workspace/account."
            )
        if (
            plan.settings_version_id != settings.settings_version_id
            or state.settings_version_id != settings.settings_version_id
        ):
            return reject("STALE_SETTINGS_VERSION", "Kế hoạch hoặc trạng thái dùng cấu hình cũ.")
        if (
            plan.risk_profile_version_id != settings.risk_profile_version_id
            or state.risk_profile_version_id != settings.risk_profile_version_id
        ):
            return reject("STALE_RISK_PROFILE", "Kế hoạch hoặc trạng thái dùng risk profile cũ.")
        if plan.strategy_release_id not in settings.active_release_ids:
            return reject("STRATEGY_RELEASE_NOT_SELECTED", "Strategy release không còn được chọn.")
        if (
            dict(settings.release_content_hashes).get(plan.strategy_release_id)
            != plan.release_content_hash
        ):
            return reject(
                "STRATEGY_RELEASE_HASH_MISMATCH", "Release content hash không khớp cấu hình."
            )
        if plan.leverage != settings.leverage or plan.exchange_rules != settings.exchange:
            return reject("PLAN_SETTINGS_MISMATCH", "Đòn bẩy hoặc quy tắc sàn của plan đã đổi.")
        if calculate_plan_hash(plan) != plan.plan_hash:
            return reject("PLAN_HASH_INVALID", "Plan đã đổi sau khi tính content hash.")
        if state.scored_plan_hash is not None and state.scored_plan_hash != plan.plan_hash:
            return reject("PLAN_HASH_INVALID", "Thông số kế hoạch đã đổi sau khi được chấm điểm.")
        limits = settings.limits
        if (
            state.evaluated_at < state.account_captured_at
            or state.evaluated_at - state.account_captured_at > limits.max_account_age
        ):
            return reject("ACCOUNT_STATE_STALE", "Snapshot tài khoản đã cũ hoặc có mốc tương lai.")
        if (
            state.evaluated_at < state.market_data_as_of
            or state.evaluated_at - state.market_data_as_of > limits.max_data_age
        ):
            return reject(
                "MARKET_DATA_STALE", "Snapshot dữ liệu thị trường đã cũ hoặc có mốc tương lai."
            )
        if any(
            item.workspace_id != state.workspace_id
            or item.account_id != state.account_id
            or not item.risk_amount.is_finite()
            or not item.notional.is_finite()
            or item.risk_amount < ZERO
            or item.notional < ZERO
            for item in state.open_exposures
        ):
            return reject("EXPOSURE_INVALID", "Exposure không hợp lệ hoặc sai workspace/account.")
        if (
            state.open_position_count < 0
            or not state.equity.is_finite()
            or state.equity <= ZERO
            or not state.available_margin.is_finite()
            or state.available_margin < ZERO
            or not state.realized_loss_today.is_finite()
            or not state.realized_loss_rolling.is_finite()
            or not state.pending_reservation_risk.is_finite()
            or not state.pending_reservation_notional.is_finite()
            or not state.pending_symbol_reservation_notional.is_finite()
            or not state.pending_reservation_initial_margin.is_finite()
            or state.realized_loss_today < ZERO
            or state.realized_loss_rolling < ZERO
            or state.pending_reservation_risk < ZERO
            or state.pending_reservation_notional < ZERO
            or state.pending_symbol_reservation_notional < ZERO
            or state.pending_reservation_initial_margin < ZERO
            or state.pending_reservation_count < 0
        ):
            return reject("RISK_STATE_INVALID", "Snapshot risk chứa giá trị âm hoặc không hữu hạn.")
        exchange = settings.exchange
        prices = (plan.entry_reference_price, plan.entry_price, plan.stop_price) + (
            (plan.target_price,) if plan.target_price is not None else ()
        )
        if (
            any(not value.is_finite() or value <= ZERO for value in prices)
            or any(value % exchange.tick_size != ZERO for value in prices)
            or (
                plan.direction == "long"
                and (
                    plan.stop_price >= plan.entry_price
                    or (plan.target_price is not None and plan.target_price <= plan.entry_price)
                )
            )
            or (
                plan.direction == "short"
                and (
                    plan.stop_price <= plan.entry_price
                    or (plan.target_price is not None and plan.target_price >= plan.entry_price)
                )
            )
        ):
            return reject(
                "PRICE_PRECISION_OR_DIRECTION_INVALID",
                "Entry, stop hoặc target không dương, lệch tick hoặc sai phía giao dịch.",
            )
        if (
            plan.quantity <= ZERO
            or plan.quantity % exchange.quantity_step != ZERO
            or plan.quantity > exchange.max_quantity
            or plan.notional != plan.quantity * plan.entry_price * exchange.contract_multiplier
            or plan.notional < exchange.min_notional
            or plan.notional > min(exchange.max_notional, limits.max_notional)
        ):
            return reject(
                "EXCHANGE_PRECISION_OR_NOTIONAL_INVALID",
                "Quantity/precision/notional không đạt quy tắc sàn.",
            )
        if plan.leverage > limits.max_leverage:
            return reject("LEVERAGE_CAP_EXCEEDED", "Đòn bẩy vượt hard cap.")
        if plan.estimated_loss > plan.risk_budget:
            return reject("PER_TRADE_RISK_CAP_EXCEEDED", "Mức lỗ dự kiến vượt ngân sách lệnh.")
        if plan.risk_budget > state.equity * limits.per_trade_risk_fraction:
            return reject("PER_TRADE_RISK_CAP_EXCEEDED", "Mức lỗ dự kiến vượt tỷ lệ rủi ro/lệnh.")
        if plan.initial_margin + state.pending_reservation_initial_margin > state.available_margin:
            return reject(
                "MARGIN_UNAVAILABLE",
                "Initial margin của plan và reservation đang giữ vượt margin khả dụng.",
            )
        if open_risk + plan.estimated_loss > limits.max_aggregate_open_risk:
            return reject(
                "AGGREGATE_RISK_CAP_EXCEEDED", "Tổng rủi ro mở và candidate vượt giới hạn."
            )
        if state.realized_loss_today >= limits.max_daily_loss:
            return reject("DAILY_LOSS_CAP_REACHED", "Đã chạm giới hạn lỗ ngày.")
        if state.realized_loss_rolling >= limits.max_rolling_loss:
            return reject("ROLLING_LOSS_CAP_REACHED", "Đã chạm giới hạn lỗ rolling.")
        if state.open_position_count + state.pending_reservation_count + 1 > limits.max_positions:
            return reject("MAX_POSITIONS_REACHED", "Đã đạt số vị thế tối đa.")
        total_notional = (
            sum((item.notional for item in state.open_exposures), ZERO)
            + state.pending_reservation_notional
            + plan.notional
        )
        if total_notional > limits.max_notional:
            return reject("NOTIONAL_CAP_EXCEEDED", "Tổng notional vượt giới hạn.")
        same_symbol = (
            sum(
                (item.notional for item in state.open_exposures if item.symbol == plan.symbol), ZERO
            )
            + state.pending_symbol_reservation_notional
            + plan.notional
        )
        concentration_cap = min(
            limits.max_symbol_notional,
            state.equity * limits.max_concentration_fraction,
        )
        if same_symbol > concentration_cap:
            return reject(
                "SYMBOL_EXPOSURE_CAP_EXCEEDED", "Notional tập trung theo symbol vượt giới hạn."
            )
        if plan.initial_margin <= ZERO:
            return reject("MARGIN_INVALID", "Initial margin phải dương.")
        return approve()
