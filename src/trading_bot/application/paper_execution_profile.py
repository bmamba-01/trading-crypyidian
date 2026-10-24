"""Ghép cấu hình PAPER đã publish với bộ phí và fill bất biến có version."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from trading_bot.config.workspace_settings import PublishedWorkspaceSettings
from trading_bot.domain.execution.paper import PaperExecutionPolicy
from trading_bot.domain.risk.planner import AccountSnapshot, CostModel


class ExecutionProfileResolutionError(ValueError):
    """Tham chiếu hoặc dữ liệu thực thi PAPER không thể được diễn giải an toàn."""


@dataclass(frozen=True, slots=True)
class FeeScheduleDefinition:
    """Bảng phí đã version hóa theo tài sản định giá."""

    version_id: str
    quote_asset: str
    entry_fee_rate: Decimal
    exit_fee_rate: Decimal

    def __post_init__(self) -> None:
        if not self.version_id.strip():
            raise ValueError("Fee schedule cần version.")
        if (
            not self.quote_asset.isalnum()
            or not self.quote_asset.isupper()
            or len(self.quote_asset) > 16
        ):
            raise ValueError("Fee schedule cần quote asset viết hoa hợp lệ.")
        _validate_rate(self.entry_fee_rate, "entry_fee_rate")
        _validate_rate(self.exit_fee_rate, "exit_fee_rate")


@dataclass(frozen=True, slots=True)
class FillModelDefinition:
    """Tham số fill cụ thể, được chọn bằng tên trong root settings."""

    reference: str
    version_id: str
    latency: timedelta
    spread_rate: Decimal
    slippage_rate: Decimal
    adverse_funding_per_unit: Decimal
    max_fill_quantity_per_event: Decimal | None

    def __post_init__(self) -> None:
        if not self.reference.strip() or not self.version_id.strip():
            raise ValueError("Fill model cần reference và version.")
        if self.latency < timedelta(0):
            raise ValueError("Fill latency không được âm.")
        _validate_rate(self.spread_rate, "spread_rate")
        _validate_rate(self.slippage_rate, "slippage_rate")
        _validate_nonnegative(self.adverse_funding_per_unit, "adverse_funding_per_unit")
        if self.spread_rate + self.slippage_rate >= Decimal("1"):
            raise ValueError("Spread cộng slippage phải nhỏ hơn 100%.")
        if self.max_fill_quantity_per_event is not None:
            _validate_nonnegative(self.max_fill_quantity_per_event, "max_fill_quantity_per_event")
            if self.max_fill_quantity_per_event == 0:
                raise ValueError("Giới hạn khớp mỗi event phải dương.")


@dataclass(frozen=True, slots=True)
class PaperExecutionProfileCatalog:
    """Ảnh chụp bất biến các fee schedule và fill model được hỗ trợ."""

    fee_schedules: tuple[FeeScheduleDefinition, ...]
    fill_models: tuple[FillModelDefinition, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.fee_schedules, tuple) or not isinstance(self.fill_models, tuple):
            raise ValueError("Catalog cần tuple bất biến cho fee schedule và fill model.")
        fee_ids = tuple(item.version_id for item in self.fee_schedules)
        fill_refs = tuple(item.reference for item in self.fill_models)
        if len(set(fee_ids)) != len(fee_ids):
            raise ValueError("Fee schedule version bị trùng trong catalog.")
        if len(set(fill_refs)) != len(fill_refs):
            raise ValueError("Fill model reference bị trùng trong catalog.")


def default_paper_execution_profile_catalog() -> PaperExecutionProfileCatalog:
    """Tạo catalog PAPER composition-ready với mức phí và stress được ghim rõ.

    Fee 0,10% mỗi chiều bằng hai lần mức taker regular-user 0,05% được Binance
    nêu trong trang công thức phí; đây là stress mặc định, không phải báo giá phí
    cá nhân. Fill stress dùng độ trễ 5 giây, spread 0,05%, slippage 0,10% và
    funding adverse 0,05 đơn vị quote trên một contract unit. Các mức fill này
    là giả định kỹ thuật bảo thủ để replay PAPER, không phải thống kê thị trường.
    """
    return PaperExecutionProfileCatalog(
        fee_schedules=(
            FeeScheduleDefinition(
                version_id="paper-fees-v1",
                quote_asset="USDT",
                entry_fee_rate=Decimal("0.001"),
                exit_fee_rate=Decimal("0.001"),
            ),
        ),
        fill_models=(
            FillModelDefinition(
                reference="next_bar_with_adverse_costs",
                version_id="next-bar-adverse-v1",
                latency=timedelta(seconds=5),
                spread_rate=Decimal("0.0005"),
                slippage_rate=Decimal("0.001"),
                adverse_funding_per_unit=Decimal("0.05"),
                max_fill_quantity_per_event=None,
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class ResolvedPaperExecutionProfile:
    """Thông số thực thi duy nhất đã ghép từ root settings và catalog."""

    workspace_id: UUID
    settings_version_id: UUID
    settings_hash: str
    quote_asset: str
    new_account_initial_equity: Decimal
    fee_schedule_version: str
    fee_schedule_hash: str
    fill_model_reference: str
    fill_model_version: str
    fill_model_hash: str
    profile_hash: str
    cost_model: CostModel
    fill_policy: PaperExecutionPolicy

    def validate_account_snapshot(
        self, account: AccountSnapshot, *, account_quote_asset: str
    ) -> None:
        """Chặn snapshot sai scope/currency mà không đặt lại equity tài khoản."""
        if account.workspace_id != str(self.workspace_id) or account.settings_version_id != str(
            self.settings_version_id
        ):
            raise ExecutionProfileResolutionError(
                "Account snapshot không thuộc workspace/version settings đã publish."
            )
        if account_quote_asset != self.quote_asset:
            raise ExecutionProfileResolutionError(
                "Quote asset tài khoản không khớp PAPER settings đã publish."
            )
        if not account.is_known or account.equity <= 0 or account.available_margin < 0:
            raise ExecutionProfileResolutionError(
                "Account snapshot không xác định hoặc equity/margin không hợp lệ."
            )


def resolve_paper_execution_profile(
    published: PublishedWorkspaceSettings,
    catalog: PaperExecutionProfileCatalog,
) -> ResolvedPaperExecutionProfile:
    """Giải tham chiếu đã publish; thiếu catalog entry thì từ chối PAPER cycle."""
    settings = published.settings
    if settings.runtime.mode != "PAPER":
        raise ExecutionProfileResolutionError("Chỉ hỗ trợ PAPER execution profile.")

    paper_settings = settings.paper_execution
    fee_schedule = next(
        (
            item
            for item in catalog.fee_schedules
            if item.version_id == paper_settings.fee_schedule_version
        ),
        None,
    )
    if fee_schedule is None:
        raise ExecutionProfileResolutionError(
            f"Fee schedule '{paper_settings.fee_schedule_version}' chưa được hỗ trợ."
        )
    fill_model = next(
        (item for item in catalog.fill_models if item.reference == paper_settings.fill_model),
        None,
    )
    if fill_model is None:
        raise ExecutionProfileResolutionError(
            f"Fill model '{paper_settings.fill_model}' chưa được hỗ trợ."
        )
    if fee_schedule.quote_asset != paper_settings.quote_asset:
        raise ExecutionProfileResolutionError(
            "Quote asset của fee schedule không khớp root PAPER settings."
        )
    if fee_schedule.entry_fee_rate != fee_schedule.exit_fee_rate:
        raise ExecutionProfileResolutionError(
            "PaperExecutionPolicy hiện chỉ hỗ trợ một mức fee; entry/exit fee phải bằng nhau."
        )

    fee_hash = _hash_fee_schedule(fee_schedule)
    fill_hash = _hash_fill_model(fill_model)
    profile_hash = _hash_profile(
        published,
        fee_schedule=fee_schedule,
        fee_hash=fee_hash,
        fill_model=fill_model,
        fill_hash=fill_hash,
    )
    policy = PaperExecutionPolicy(
        version_id=profile_hash,
        latency=fill_model.latency,
        spread_rate=fill_model.spread_rate,
        slippage_rate=fill_model.slippage_rate,
        fee_rate=fee_schedule.entry_fee_rate,
        max_fill_quantity_per_event=fill_model.max_fill_quantity_per_event,
    )
    costs = CostModel(
        version_id=profile_hash,
        entry_fee_rate=fee_schedule.entry_fee_rate,
        exit_fee_rate=fee_schedule.exit_fee_rate,
        adverse_funding_per_unit=fill_model.adverse_funding_per_unit,
        entry_slippage_rate=fill_model.spread_rate + fill_model.slippage_rate,
        exit_slippage_rate=fill_model.spread_rate + fill_model.slippage_rate,
    )
    return ResolvedPaperExecutionProfile(
        workspace_id=published.workspace_id,
        settings_version_id=published.settings_version_id,
        settings_hash=published.settings_hash,
        quote_asset=paper_settings.quote_asset,
        new_account_initial_equity=paper_settings.starting_equity,
        fee_schedule_version=fee_schedule.version_id,
        fee_schedule_hash=fee_hash,
        fill_model_reference=fill_model.reference,
        fill_model_version=fill_model.version_id,
        fill_model_hash=fill_hash,
        profile_hash=profile_hash,
        cost_model=costs,
        fill_policy=policy,
    )


def _hash_fee_schedule(schedule: FeeScheduleDefinition) -> str:
    return _hash(
        {
            "version_id": schedule.version_id,
            "quote_asset": schedule.quote_asset,
            "entry_fee_rate": str(schedule.entry_fee_rate),
            "exit_fee_rate": str(schedule.exit_fee_rate),
        }
    )


def _hash_fill_model(model: FillModelDefinition) -> str:
    return _hash(
        {
            "reference": model.reference,
            "version_id": model.version_id,
            "latency_microseconds": str(model.latency // timedelta(microseconds=1)),
            "spread_rate": str(model.spread_rate),
            "slippage_rate": str(model.slippage_rate),
            "adverse_funding_per_unit": str(model.adverse_funding_per_unit),
            "max_fill_quantity_per_event": (
                str(model.max_fill_quantity_per_event)
                if model.max_fill_quantity_per_event is not None
                else None
            ),
        }
    )


def _hash_profile(
    published: PublishedWorkspaceSettings,
    *,
    fee_schedule: FeeScheduleDefinition,
    fee_hash: str,
    fill_model: FillModelDefinition,
    fill_hash: str,
) -> str:
    return _hash(
        {
            "workspace_id": str(published.workspace_id),
            "quote_asset": published.settings.paper_execution.quote_asset,
            "starting_equity": str(published.settings.paper_execution.starting_equity),
            "fee_schedule_version": fee_schedule.version_id,
            "fee_schedule_hash": fee_hash,
            "fill_model_reference": fill_model.reference,
            "fill_model_version": fill_model.version_id,
            "fill_model_hash": fill_hash,
        }
    )


def _hash(value: dict[str, str | None]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_rate(value: Decimal, name: str) -> None:
    _validate_nonnegative(value, name)
    if value >= Decimal("1"):
        raise ValueError(f"{name} phải nhỏ hơn 1.")


def _validate_nonnegative(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} phải là Decimal hữu hạn không âm.")
