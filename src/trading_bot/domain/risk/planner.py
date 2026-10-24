"""Tạo kế hoạch giao dịch thuần, đã tính chi phí và giới hạn sàn."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from trading_bot.domain.risk.sizing import RiskSizingInput, TradingSide, size_position
from trading_bot.domain.strategy.contracts import StrategyCandidate
from trading_bot.domain.strategy.recipes import ExitRecipe

ZERO = Decimal("0")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


@dataclass(frozen=True, slots=True)
class ExchangeRules:
    """Quy tắc giá, khối lượng và giá trị tối thiểu của hợp đồng."""

    tick_size: Decimal
    quantity_step: Decimal
    contract_multiplier: Decimal
    min_notional: Decimal
    max_notional: Decimal
    max_quantity: Decimal

    def __post_init__(self) -> None:
        positive = (
            self.tick_size,
            self.quantity_step,
            self.contract_multiplier,
            self.max_notional,
            self.max_quantity,
        )
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO
            for value in positive
        ):
            raise ValueError("Quy tắc sàn phải là Decimal hữu hạn dương.")
        if (
            not isinstance(self.min_notional, Decimal)
            or not self.min_notional.is_finite()
            or self.min_notional < ZERO
        ):
            raise ValueError("Notional tối thiểu phải là Decimal hữu hạn không âm.")


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Các giới hạn cứng đã được ghim vào phiên bản hồ sơ rủi ro."""

    per_trade_risk_fraction: Decimal
    max_aggregate_open_risk: Decimal
    max_notional: Decimal
    max_leverage: Decimal
    max_positions: int
    max_symbol_notional: Decimal
    max_concentration_fraction: Decimal
    max_daily_loss: Decimal
    max_rolling_loss: Decimal
    max_account_age: timedelta
    max_data_age: timedelta

    def __post_init__(self) -> None:
        fractions = (self.per_trade_risk_fraction, self.max_concentration_fraction)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in fractions):
            raise ValueError("Tỷ lệ risk phải là Decimal hữu hạn.")
        if not ZERO < self.per_trade_risk_fraction <= Decimal("1"):
            raise ValueError("Tỷ lệ risk mỗi lệnh phải nằm trong (0, 1].")
        if not ZERO < self.max_concentration_fraction <= Decimal("1"):
            raise ValueError("Tỷ lệ tập trung phải nằm trong (0, 1].")
        amounts = (
            self.max_aggregate_open_risk,
            self.max_notional,
            self.max_leverage,
            self.max_symbol_notional,
            self.max_daily_loss,
            self.max_rolling_loss,
        )
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO
            for value in amounts
        ):
            raise ValueError("Giới hạn tiền và đòn bẩy phải là Decimal hữu hạn dương.")
        if (
            self.max_positions < 1
            or self.max_account_age < timedelta(0)
            or self.max_data_age < timedelta(0)
        ):
            raise ValueError("Giới hạn số vị thế hoặc tuổi dữ liệu không hợp lệ.")


@dataclass(frozen=True, slots=True)
class PlannerSettings:
    """Cấu hình đã publish, kèm các version dùng để lập và kiểm tra kế hoạch."""

    workspace_id: str
    settings_version_id: str
    risk_profile_version_id: str
    published: bool
    active_release_ids: tuple[str, ...]
    leverage: Decimal
    exchange: ExchangeRules
    limits: RiskLimits
    release_content_hashes: tuple[tuple[str, str], ...]
    settings_hash: str | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.workspace_id, self.settings_version_id, self.risk_profile_version_id)
        ):
            raise ValueError("Thiếu workspace hoặc version cấu hình/risk.")
        if len(set(self.active_release_ids)) != len(self.active_release_ids):
            raise ValueError("Strategy release không được lặp.")
        release_hash_ids = tuple(release_id for release_id, _ in self.release_content_hashes)
        if len(set(release_hash_ids)) != len(release_hash_ids):
            raise ValueError("Hash strategy release không được lặp.")
        if (
            not isinstance(self.leverage, Decimal)
            or not self.leverage.is_finite()
            or self.leverage <= ZERO
        ):
            raise ValueError("Đòn bẩy phải là Decimal hữu hạn dương.")
        if self.settings_hash is not None and (
            len(self.settings_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.settings_hash)
        ):
            raise ValueError("settings_hash phải là SHA-256 dạng hex chữ thường.")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Trạng thái tài khoản đã chụp cùng mốc lấy mẫu và version cấu hình."""

    workspace_id: str
    account_id: str
    settings_version_id: str
    captured_at: datetime
    equity: Decimal
    available_margin: Decimal
    is_known: bool

    def __post_init__(self) -> None:
        _aware(self.captured_at, "captured_at")
        if any(
            not value.strip()
            for value in (self.workspace_id, self.account_id, self.settings_version_id)
        ):
            raise ValueError("Snapshot cần workspace, account và settings version.")
        if any(
            not isinstance(value, Decimal) or not value.is_finite()
            for value in (self.equity, self.available_margin)
        ):
            raise ValueError("Equity và margin phải là Decimal hữu hạn.")


@dataclass(frozen=True, slots=True)
class OpenExposure:
    """Mức notional và rủi ro của vị thế đang mở; pending risk do reservation quản lý."""

    workspace_id: str
    account_id: str
    symbol: str
    notional: Decimal
    risk_amount: Decimal

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (self.workspace_id, self.account_id, self.symbol)):
            raise ValueError("Exposure cần workspace, account và symbol.")
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value < ZERO
            for value in (self.notional, self.risk_amount)
        ):
            raise ValueError("Exposure phải là Decimal hữu hạn không âm.")


@dataclass(frozen=True, slots=True)
class CostModel:
    """Kịch bản chi phí đã version hóa cho entry, exit và các chi phí đã biết."""

    version_id: str
    entry_fee_rate: Decimal
    exit_fee_rate: Decimal
    adverse_funding_per_unit: Decimal
    entry_slippage_rate: Decimal
    exit_slippage_rate: Decimal = ZERO

    def __post_init__(self) -> None:
        if not self.version_id.strip():
            raise ValueError("Cost model cần version.")
        values = (
            self.entry_fee_rate,
            self.exit_fee_rate,
            self.adverse_funding_per_unit,
            self.entry_slippage_rate,
            self.exit_slippage_rate,
        )
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value < ZERO
            for value in values
        ):
            raise ValueError("Chi phí phải là Decimal hữu hạn không âm.")
        if self.entry_slippage_rate >= Decimal("1") or self.exit_slippage_rate >= Decimal("1"):
            raise ValueError("Tỷ lệ slippage phải nhỏ hơn 1.")


@dataclass(frozen=True, slots=True)
class PlanRejected:
    """Kết quả từ chối lập kế hoạch có mã lý do và version cấu hình."""

    reason_code: str
    detail_vi: str
    settings_version_id: str
    strategy_release_id: str


@dataclass(frozen=True, slots=True)
class TradePlan:
    """Kế hoạch bất biến để risk gate kiểm tra trước khi giữ ngân sách."""

    workspace_id: str
    account_id: str
    settings_version_id: str
    settings_hash: str | None
    risk_profile_version_id: str
    strategy_id: str
    strategy_release_id: str
    release_content_hash: str
    snapshot_id: str
    candidate_id: str
    symbol: str
    direction: str
    candidate_reason_code: str
    evidence_feature_ids: tuple[str, ...]
    evidence_bar_keys: tuple[str, ...]
    macro_evidence_ids: tuple[str, ...]
    entry_reference_price: Decimal
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal | None
    quantity: Decimal
    notional: Decimal
    leverage: Decimal
    initial_margin: Decimal
    risk_budget: Decimal
    estimated_loss: Decimal
    entry_fee_cost: Decimal
    exit_fee_cost: Decimal
    adverse_funding_cost: Decimal
    entry_slippage_cost: Decimal
    cost_model_version: str
    exchange_rules: ExchangeRules
    exit_recipe: ExitRecipe
    expires_at: datetime
    source_signal_at: datetime
    plan_hash: str


class TradePlanner:
    """Chuyển candidate thành TradePlan; không gửi lệnh hoặc sửa hard limit."""

    def plan(
        self,
        candidate: StrategyCandidate,
        settings: PlannerSettings,
        account_snapshot: AccountSnapshot,
        open_exposure: tuple[OpenExposure, ...],
        cost_model: CostModel,
        *,
        now: datetime,
    ) -> TradePlan | PlanRejected:
        """Tính entry/stop/target, size, chi phí và các mốc lineage."""
        _aware(now, "now")
        _aware(account_snapshot.captured_at, "account_snapshot.captured_at")
        reject = self._precheck(candidate, settings, account_snapshot, open_exposure, now)
        if reject is not None:
            return reject
        if not settings.published:
            return self._reject(
                "SETTINGS_UNPUBLISHED", "Cấu hình chưa được publish.", settings, candidate
            )
        if candidate.strategy_release_id not in settings.active_release_ids:
            return self._reject(
                "STRATEGY_RELEASE_NOT_SELECTED",
                "Strategy release chưa được chọn.",
                settings,
                candidate,
            )
        if (
            dict(settings.release_content_hashes).get(candidate.strategy_release_id)
            != candidate.release_content_hash
        ):
            return self._reject(
                "STRATEGY_RELEASE_HASH_MISMATCH",
                "Candidate dùng nội dung release khác registry.",
                settings,
                candidate,
            )
        if candidate.settings_version_id != settings.settings_version_id:
            return self._reject(
                "STALE_SETTINGS_VERSION", "Candidate dùng cấu hình khác.", settings, candidate
            )
        if account_snapshot.settings_version_id != settings.settings_version_id:
            return self._reject(
                "STALE_SETTINGS_VERSION",
                "Snapshot tài khoản dùng cấu hình cũ.",
                settings,
                candidate,
            )
        if not account_snapshot.is_known:
            return self._reject(
                "ACCOUNT_STATE_UNKNOWN",
                "Chưa xác nhận được trạng thái tài khoản.",
                settings,
                candidate,
            )
        if (
            now < account_snapshot.captured_at
            or now - account_snapshot.captured_at > settings.limits.max_account_age
        ):
            return self._reject(
                "ACCOUNT_STATE_STALE",
                "Snapshot tài khoản đã cũ hoặc có mốc tương lai.",
                settings,
                candidate,
            )
        if now < candidate.signal_at or now - candidate.signal_at > settings.limits.max_data_age:
            return self._reject(
                "MARKET_DATA_STALE",
                "Tín hiệu thị trường đã cũ hoặc có mốc tương lai.",
                settings,
                candidate,
            )
        if now < candidate.eligible_after:
            return self._reject(
                "ENTRY_NOT_ELIGIBLE",
                "Candidate chưa tới thời điểm được phép vào.",
                settings,
                candidate,
            )
        if now >= candidate.expires_at:
            return self._reject("CANDIDATE_EXPIRED", "Candidate đã hết hạn.", settings, candidate)
        if (
            account_snapshot.workspace_id != settings.workspace_id
            or not account_snapshot.account_id.strip()
        ):
            return self._reject(
                "ACCOUNT_SCOPE_INVALID",
                "Tài khoản không thuộc workspace cấu hình.",
                settings,
                candidate,
            )
        if any(
            item.workspace_id != settings.workspace_id
            or item.account_id != account_snapshot.account_id
            for item in open_exposure
        ):
            return self._reject(
                "EXPOSURE_SCOPE_INVALID",
                "Exposure có workspace hoặc account không khớp.",
                settings,
                candidate,
            )
        if len(open_exposure) + 1 > settings.limits.max_positions:
            return self._reject(
                "MAX_POSITIONS_REACHED", "Đã đạt số vị thế tối đa.", settings, candidate
            )
        if not self._valid_cost_model(cost_model):
            return self._reject(
                "COST_MODEL_INVALID", "Kịch bản chi phí không hợp lệ.", settings, candidate
            )
        if settings.leverage <= ZERO or settings.leverage > settings.limits.max_leverage:
            return self._reject(
                "LEVERAGE_CAP_EXCEEDED", "Đòn bẩy vượt giới hạn cứng.", settings, candidate
            )

        limits, exchange = settings.limits, settings.exchange
        open_risk = sum((item.risk_amount for item in open_exposure), ZERO)
        remaining_risk = limits.max_aggregate_open_risk - open_risk
        if remaining_risk <= ZERO:
            return self._reject(
                "AGGREGATE_RISK_CAP_EXCEEDED", "Ngân sách rủi ro đã dùng hết.", settings, candidate
            )
        symbol_notional = sum(
            (item.notional for item in open_exposure if item.symbol == candidate.symbol), ZERO
        )
        concentration_limit = min(
            limits.max_symbol_notional,
            account_snapshot.equity * limits.max_concentration_fraction,
        )
        remaining_symbol_notional = concentration_limit - symbol_notional
        remaining_account_notional = limits.max_notional - sum(
            (item.notional for item in open_exposure), ZERO
        )
        maximum_notional = min(
            exchange.max_notional,
            limits.max_notional,
            remaining_symbol_notional,
            remaining_account_notional,
        )
        if maximum_notional <= ZERO:
            return self._reject(
                "NOTIONAL_CAP_EXCEEDED",
                "Không còn hạn mức notional cho candidate.",
                settings,
                candidate,
            )

        direction = candidate.direction
        side: TradingSide = "LONG" if direction == "long" else "SHORT"
        multiplier = exchange.contract_multiplier
        reference = candidate.reference_price
        slip_rate = cost_model.entry_slippage_rate
        raw_entry = reference * (
            Decimal("1") + slip_rate if side == "LONG" else Decimal("1") - slip_rate
        )
        entry = _round_price(
            raw_entry, exchange.tick_size, ROUND_CEILING if side == "LONG" else ROUND_FLOOR
        )
        atr = Decimal(str(candidate.atr_value))
        stop_delta = atr * Decimal(str(candidate.recipe.stop_atr_multiple))
        raw_stop = reference - stop_delta if side == "LONG" else reference + stop_delta
        stop = _round_price(
            raw_stop, exchange.tick_size, ROUND_FLOOR if side == "LONG" else ROUND_CEILING
        )
        target = None
        if candidate.recipe.take_profit_atr_multiple is not None:
            target_delta = atr * Decimal(str(candidate.recipe.take_profit_atr_multiple))
            raw_target = reference + target_delta if side == "LONG" else reference - target_delta
            target = _round_price(
                raw_target, exchange.tick_size, ROUND_CEILING if side == "LONG" else ROUND_FLOOR
            )
        if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
            return self._reject(
                "STOP_DIRECTION_INVALID", "Stop không nằm phía lỗ của entry.", settings, candidate
            )
        if target is not None and (
            (side == "LONG" and target <= entry) or (side == "SHORT" and target >= entry)
        ):
            return self._reject(
                "TARGET_DIRECTION_INVALID",
                "Target không nằm phía lợi nhuận của entry.",
                settings,
                candidate,
            )
        prices = (entry, stop) + ((target,) if target is not None else ())
        if any(price <= ZERO for price in prices) or any(
            price % exchange.tick_size != ZERO for price in prices
        ):
            return self._reject(
                "PRICE_PRECISION_OR_DIRECTION_INVALID",
                "Entry, stop hoặc target không dương hoặc không khớp bước giá sàn.",
                settings,
                candidate,
            )

        entry_fee = entry * multiplier * cost_model.entry_fee_rate
        exit_fee = stop * multiplier * cost_model.exit_fee_rate
        slippage_cost = abs(entry - reference) * multiplier
        sizing = size_position(
            RiskSizingInput(
                equity=account_snapshot.equity,
                per_trade_risk_fraction=limits.per_trade_risk_fraction,
                remaining_open_risk_budget=remaining_risk,
                side=side,
                entry_price=entry,
                stop_price=stop,
                contract_multiplier=multiplier,
                entry_fee_per_unit=entry_fee,
                exit_fee_per_unit=exit_fee,
                adverse_funding_per_unit=cost_model.adverse_funding_per_unit,
                slippage_stress_per_unit=(stop * cost_model.exit_slippage_rate * multiplier),
                quantity_step=exchange.quantity_step,
                min_notional=exchange.min_notional,
                max_notional=maximum_notional,
                exchange_max_quantity=exchange.max_quantity,
                leverage=settings.leverage,
                max_leverage=limits.max_leverage,
                available_margin=account_snapshot.available_margin,
            )
        )
        if not sizing.accepted:
            code = sizing.reason or "SIZING_REJECTED"
            return self._reject(code, f"Không thể lập size: {code}.", settings, candidate)
        return _make_plan(
            candidate=candidate,
            settings=settings,
            account=account_snapshot,
            cost_model=cost_model,
            entry=entry,
            stop=stop,
            target=target,
            size=sizing,
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            funding=cost_model.adverse_funding_per_unit,
            slippage=slippage_cost,
        )

    @staticmethod
    def _precheck(
        candidate: StrategyCandidate,
        settings: PlannerSettings,
        account: AccountSnapshot,
        exposures: tuple[OpenExposure, ...],
        now: datetime,
    ) -> PlanRejected | None:
        del now
        if (
            not settings.workspace_id
            or not settings.settings_version_id
            or not settings.risk_profile_version_id
        ):
            return PlanRejected(
                "SETTINGS_LINEAGE_MISSING",
                "Thiếu version cấu hình hoặc hồ sơ rủi ro.",
                settings.settings_version_id,
                candidate.strategy_release_id,
            )
        if account.workspace_id != settings.workspace_id or not account.account_id:
            return PlanRejected(
                "ACCOUNT_SCOPE_INVALID",
                "Tài khoản không thuộc workspace cấu hình.",
                settings.settings_version_id,
                candidate.strategy_release_id,
            )
        if any(
            not item.notional.is_finite()
            or not item.risk_amount.is_finite()
            or item.notional < ZERO
            or item.risk_amount < ZERO
            for item in exposures
        ):
            return PlanRejected(
                "EXPOSURE_INVALID",
                "Exposure phải là số Decimal hữu hạn không âm.",
                settings.settings_version_id,
                candidate.strategy_release_id,
            )
        return None

    @staticmethod
    def _valid_cost_model(model: CostModel) -> bool:
        values = (
            model.entry_fee_rate,
            model.exit_fee_rate,
            model.adverse_funding_per_unit,
            model.entry_slippage_rate,
        )
        return (
            bool(model.version_id.strip())
            and all(value.is_finite() and value >= ZERO for value in values)
            and model.entry_slippage_rate < Decimal("1")
        )

    @staticmethod
    def _reject(
        code: str, detail: str, settings: PlannerSettings, candidate: StrategyCandidate
    ) -> PlanRejected:
        return PlanRejected(
            code, detail, settings.settings_version_id, candidate.strategy_release_id
        )


def _round_price(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    if not tick.is_finite() or tick <= ZERO:
        return ZERO
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _make_plan(
    *,
    candidate: StrategyCandidate,
    settings: PlannerSettings,
    account: AccountSnapshot,
    cost_model: CostModel,
    entry: Decimal,
    stop: Decimal,
    target: Decimal | None,
    size: Any,
    entry_fee: Decimal,
    exit_fee: Decimal,
    funding: Decimal,
    slippage: Decimal,
) -> TradePlan:
    fields: dict[str, Any] = {
        "workspace_id": settings.workspace_id,
        "account_id": account.account_id,
        "settings_version_id": settings.settings_version_id,
        "settings_hash": settings.settings_hash,
        "risk_profile_version_id": settings.risk_profile_version_id,
        "strategy_id": candidate.strategy_id,
        "strategy_release_id": candidate.strategy_release_id,
        "release_content_hash": candidate.release_content_hash,
        "snapshot_id": candidate.snapshot_id,
        "candidate_id": candidate.candidate_id,
        "symbol": candidate.symbol,
        "direction": candidate.direction,
        "candidate_reason_code": candidate.reason_code,
        "evidence_feature_ids": candidate.evidence_feature_ids,
        "evidence_bar_keys": candidate.evidence_bar_keys,
        "macro_evidence_ids": candidate.macro_evidence_ids,
        "entry_reference_price": candidate.reference_price,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "quantity": size.quantity,
        "notional": size.notional,
        "leverage": settings.leverage,
        "initial_margin": size.initial_margin,
        "risk_budget": size.risk_budget,
        "estimated_loss": size.estimated_loss,
        "entry_fee_cost": entry_fee * size.quantity,
        "exit_fee_cost": exit_fee * size.quantity,
        "adverse_funding_cost": funding * size.quantity,
        "entry_slippage_cost": slippage * size.quantity,
        "cost_model_version": cost_model.version_id,
        "exchange_rules": settings.exchange,
        "exit_recipe": candidate.recipe,
        "expires_at": candidate.expires_at,
        "source_signal_at": candidate.signal_at,
    }
    digest = _hash_plan_fields(fields)
    return TradePlan(**fields, plan_hash=digest)


def calculate_plan_hash(plan: TradePlan) -> str:
    """Tính lại hash mọi tham số có thể ảnh hưởng tới fill, risk và exit."""
    plan_fields = {
        item.name: getattr(plan, item.name) for item in fields(plan) if item.name != "plan_hash"
    }
    return _hash_plan_fields(plan_fields)


def _hash_plan_fields(plan_fields: dict[str, Any]) -> str:
    digest_payload = _canonical_plan(plan_fields)
    return hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _canonical_plan(fields: dict[str, Any]) -> dict[str, Any]:
    def canonical(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, ExitRecipe):
            return (
                {key: canonical(item) for key, item in value.__dict__.items()}
                if hasattr(value, "__dict__")
                else {name: canonical(getattr(value, name)) for name in value.__dataclass_fields__}
            )
        if isinstance(value, ExchangeRules):
            return {name: canonical(getattr(value, name)) for name in value.__dataclass_fields__}
        if isinstance(value, tuple):
            return [canonical(item) for item in value]
        return value

    return {key: canonical(value) for key, value in fields.items()}
