"""Use-case tạo và theo dõi job replay trong phạm vi workspace."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord
from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
    BacktestDatasetReplayBundleRepository,
)
from trading_bot.adapters.database.repositories.backtest_replay_jobs import (
    BacktestReplayJobRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.auth.policy import Principal, Role

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class BacktestReplayJobSubmission:
    """Giả định PAPER được lưu bất biến cùng yêu cầu chạy."""

    bundle_hash: str
    strategy_release_id: str
    execution_interval: str
    starting_equity: Decimal
    per_trade_risk_fraction: Decimal
    fee_rate: Decimal
    slippage_rate: Decimal
    seed: int
    learning_intent: str = "RESEARCH"
    # AI-3 (16/11/2026): tham số chiến lược của bot (H03 lookback) để so bản gốc/bản đề xuất.
    # Rỗng = tham số mặc định của release (hành vi cũ, hash cũ giữ nguyên).
    strategy_parameters: tuple[tuple[str, int], ...] = ()
    # CRY-428: ghi đè thoát lệnh theo bot (JSON canonical của ``ExitOverride.payload``). Rỗng =
    # recipe của release (hash cũ giữ nguyên).
    exit_override_json: str = ""
    # BT-1: hồ sơ chi phí khớp PAPER (spread, funding, đòn bẩy) và số lượt thử để chiết khấu.
    spread_rate: Decimal = Decimal("0")
    adverse_funding_rate: Decimal = Decimal("0")
    leverage: Decimal = Decimal("1")
    trial_count: int = 1
    trial_index: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.bundle_hash, str) or _SHA256.fullmatch(self.bundle_hash) is None:
            raise ValueError("Hash bundle phải là SHA-256 dạng hex thường.")
        if not isinstance(self.strategy_release_id, str) or not self.strategy_release_id.strip():
            raise ValueError("Cần chọn bản phát hành chiến lược.")
        if not isinstance(self.execution_interval, str) or not self.execution_interval.strip():
            raise ValueError("Cần chọn khung thời gian khớp PAPER.")
        strategy_registry().get_release(self.strategy_release_id)
        if self.strategy_parameters:
            from trading_bot.domain.strategy.contracts import StrategySelection

            StrategySelection(
                "replay-parameter-check",
                (self.strategy_release_id,),
                True,
                tuple(sorted(self.strategy_parameters)),
            )
        if self.exit_override_json:
            self.exit_override()
        for name in (
            "starting_equity",
            "per_trade_risk_fraction",
            "fee_rate",
            "slippage_rate",
            "spread_rate",
            "adverse_funding_rate",
            "leverage",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} phải là Decimal hữu hạn.")
        if self.starting_equity <= 0:
            raise ValueError("Vốn mô phỏng phải lớn hơn 0.")
        if not 0 < self.per_trade_risk_fraction <= Decimal("0.1"):
            raise ValueError("Rủi ro mỗi lệnh phải nằm trong khoảng (0, 10%].")
        if not 0 <= self.fee_rate < 1:
            raise ValueError("Phí mỗi chiều phải nằm trong [0, 100%).")
        if not 0 <= self.slippage_rate < 1:
            raise ValueError("Trượt giá phải nằm trong [0, 100%).")
        if not 0 <= self.spread_rate < 1:
            raise ValueError("Spread phải nằm trong [0, 100%).")
        if not 0 <= self.adverse_funding_rate < 1:
            raise ValueError("Adverse funding rate phải nằm trong [0, 100%).")
        if not Decimal("1") <= self.leverage <= Decimal("125"):
            raise ValueError("Đòn bẩy phải nằm trong [1, 125].")
        if (
            type(self.trial_count) is not int
            or type(self.trial_index) is not int
            or self.trial_count < 1
            or not 0 <= self.trial_index < self.trial_count
        ):
            raise ValueError("trial_index phải thuộc [0, trial_count) với trial_count >= 1.")
        if type(self.seed) is not int or not 0 <= self.seed <= 2**31 - 1:
            raise ValueError("Seed phải là số nguyên từ 0 đến 2,147,483,647.")
        if type(self.learning_intent) is not str or self.learning_intent not in {
            "RESEARCH",
            "TRAINING_BASE",
        }:
            raise ValueError("Mục đích replay không hợp lệ.")

    def exit_override(self) -> Any:
        """Ghi đè thoát đã kiểm (chỉ trường release vốn có); None nếu không ghi đè."""
        if not self.exit_override_json:
            return None
        import json

        from trading_bot.application.exit_policy_catalog import validate_exit_override
        from trading_bot.domain.strategy.exit_override import exit_override_from_payload

        override = exit_override_from_payload(json.loads(self.exit_override_json))
        if override is None:
            raise ValueError("Ghi đè thoát lệnh rỗng.")
        problems = validate_exit_override(self.strategy_release_id, override)
        if problems:
            raise ValueError("; ".join(f"{field}: {message}" for field, message in problems))
        return override

    @classmethod
    def from_paper_profile(
        cls,
        profile: Any,
        *,
        bundle_hash: str,
        strategy_release_id: str,
        execution_interval: str,
        per_trade_risk_fraction: Decimal,
        seed: int,
        leverage: Decimal = Decimal("1"),
        trial_count: int = 1,
        trial_index: int = 0,
        learning_intent: str = "RESEARCH",
        strategy_parameters: tuple[tuple[str, int], ...] = (),
        exit_override_json: str = "",
    ) -> BacktestReplayJobSubmission:
        """Khởi tạo submission replay dùng đúng hồ sơ chi phí của PAPER."""
        return cls(
            bundle_hash=bundle_hash,
            strategy_release_id=strategy_release_id,
            execution_interval=execution_interval,
            starting_equity=profile.new_account_initial_equity,
            per_trade_risk_fraction=per_trade_risk_fraction,
            fee_rate=profile.fill_policy.fee_rate,
            slippage_rate=profile.fill_policy.slippage_rate,
            seed=seed,
            learning_intent=learning_intent,
            strategy_parameters=strategy_parameters,
            exit_override_json=exit_override_json,
            spread_rate=profile.fill_policy.spread_rate,
            adverse_funding_rate=profile.cost_model.adverse_funding_rate,
            leverage=leverage,
            trial_count=trial_count,
            trial_index=trial_index,
        )

    def to_payload(self) -> dict[str, str | int]:
        """Chuẩn hóa Decimal thành chuỗi để payload JSON không đi qua float."""
        payload: dict[str, str | int] = {
            "bundle_hash": self.bundle_hash,
            "strategy_release_id": self.strategy_release_id,
            "execution_interval": self.execution_interval,
            "starting_equity": str(self.starting_equity),
            "per_trade_risk_fraction": str(self.per_trade_risk_fraction),
            "fee_rate": str(self.fee_rate),
            "slippage_rate": str(self.slippage_rate),
            "seed": self.seed,
            "learning_intent": self.learning_intent,
        }
        if self.spread_rate != Decimal("0"):
            payload["spread_rate"] = str(self.spread_rate)
        if self.adverse_funding_rate != Decimal("0"):
            payload["adverse_funding_rate"] = str(self.adverse_funding_rate)
        if self.leverage != Decimal("1"):
            payload["leverage"] = str(self.leverage)
        if self.trial_count != 1 or self.trial_index != 0:
            payload["trial_count"] = self.trial_count
            payload["trial_index"] = self.trial_index
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> BacktestReplayJobSubmission:
        """Đọc payload worker và từ chối field thiếu/lạ hoặc sai kiểu."""
        required_fields = {
            "bundle_hash",
            "strategy_release_id",
            "execution_interval",
            "starting_equity",
            "per_trade_risk_fraction",
            "fee_rate",
            "slippage_rate",
            "seed",
        }
        optional_fields = {
            "learning_intent",
            "spread_rate",
            "adverse_funding_rate",
            "leverage",
            "trial_count",
            "trial_index",
        }
        if (
            type(payload) is not dict
            or not required_fields.issubset(payload)
            or not set(payload).issubset(required_fields | optional_fields)
        ):
            raise ValueError("Payload job replay có field thiếu hoặc không nhận diện.")
        decimal_fields = (
            "starting_equity",
            "per_trade_risk_fraction",
            "fee_rate",
            "slippage_rate",
        )
        if any(type(payload[name]) is not str for name in decimal_fields):
            raise ValueError("Giá trị Decimal trong payload phải là chuỗi.")
        for opt_decimal in ("spread_rate", "adverse_funding_rate", "leverage"):
            if opt_decimal in payload and type(payload[opt_decimal]) is not str:
                raise ValueError("Giá trị Decimal trong payload phải là chuỗi.")
        return cls(
            bundle_hash=payload["bundle_hash"],
            strategy_release_id=payload["strategy_release_id"],
            execution_interval=payload["execution_interval"],
            starting_equity=Decimal(payload["starting_equity"]),
            per_trade_risk_fraction=Decimal(payload["per_trade_risk_fraction"]),
            fee_rate=Decimal(payload["fee_rate"]),
            slippage_rate=Decimal(payload["slippage_rate"]),
            seed=payload["seed"],
            learning_intent=payload.get("learning_intent", "RESEARCH"),
            spread_rate=Decimal(payload.get("spread_rate", "0")),
            adverse_funding_rate=Decimal(payload.get("adverse_funding_rate", "0")),
            leverage=Decimal(payload.get("leverage", "1")),
            trial_count=payload.get("trial_count", 1),
            trial_index=payload.get("trial_index", 0),
        )


@dataclass(frozen=True, slots=True)
class BacktestReplayJob:
    """Thông tin hiển thị đã giới hạn theo workspace hiện hành."""

    id: UUID
    status: str
    stage: str
    progress: int
    bundle_hash: str
    strategy_release_id: str
    execution_interval: str
    learning_intent: str
    created_at: datetime
    updated_at: datetime
    error_code: str | None
    result_run_ids: tuple[str, ...]
    cancel_requested: bool
    cancel_requested_at: datetime | None
    cancel_requested_by: UUID | None


@dataclass(frozen=True, slots=True)
class BacktestReplayBundleChoice:
    """Thông tin gọn của bundle có thể chọn trong trang admin."""

    bundle_hash: str
    symbol: str
    evaluation_start: datetime
    range_end: datetime
    intervals: tuple[str, ...]


class BacktestReplayJobService:
    """Cho phép owner xếp hàng, xem và yêu cầu hủy replay PAPER."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def submit(
        self,
        principal: Principal,
        request: BacktestReplayJobSubmission,
    ) -> BacktestReplayJob:
        """Xác minh bundle trong tenant rồi thêm job đang chờ vào hàng đợi."""
        self._require_owner(principal)
        if type(request) is not BacktestReplayJobSubmission:
            raise ValueError("Yêu cầu job replay không hợp lệ.")
        async with UnitOfWork(self._sessions, workspace_id=principal.workspace_id) as uow:
            bundle = await BacktestDatasetReplayBundleRepository(uow.session).get_by_hash(
                workspace_id=principal.workspace_id,
                bundle_hash=request.bundle_hash,
            )
            if bundle is None:
                raise LookupError("Không tìm thấy bundle trong workspace.")
            if request.execution_interval not in {item.interval for item in bundle.components}:
                raise ValueError("Khung thời gian khớp PAPER không có trong bundle.")
            row = await BacktestReplayJobRepository(uow.session).enqueue(
                workspace_id=principal.workspace_id,
                requested_by=principal.membership_id,
                request=request,
            )
            return _to_job(row)

    async def list(self, principal: Principal) -> tuple[BacktestReplayJob, ...]:
        """Trả các job gần đây trong workspace của owner hiện hành."""
        self._require_owner(principal)
        async with UnitOfWork(self._sessions, workspace_id=principal.workspace_id) as uow:
            rows = await BacktestReplayJobRepository(uow.session).list_recent(
                workspace_id=principal.workspace_id
            )
            return tuple(_to_job(row) for row in rows)

    async def list_bundles(self, principal: Principal) -> tuple[BacktestReplayBundleChoice, ...]:
        """Trả lựa chọn bundle và khung thời gian đã lưu trong workspace hiện hành."""
        self._require_owner(principal)
        async with UnitOfWork(self._sessions, workspace_id=principal.workspace_id) as uow:
            references = await BacktestDatasetReplayBundleRepository(uow.session).list_recent(
                workspace_id=principal.workspace_id
            )
            return tuple(
                BacktestReplayBundleChoice(
                    bundle_hash=item.bundle_hash,
                    symbol=item.symbol,
                    evaluation_start=item.evaluation_start,
                    range_end=item.range_end,
                    intervals=tuple(component.interval for component in item.components),
                )
                for item in references
            )

    async def cancel(self, principal: Principal, job_id: UUID) -> BacktestReplayJob:
        """Ghi yêu cầu hủy; worker sẽ dừng tại điểm an toàn tiếp theo."""
        self._require_owner(principal)
        if type(job_id) is not UUID:
            raise ValueError("job_id không hợp lệ.")
        async with UnitOfWork(self._sessions, workspace_id=principal.workspace_id) as uow:
            row = await BacktestReplayJobRepository(uow.session).request_cancel(
                workspace_id=principal.workspace_id,
                job_id=job_id,
                requested_by=principal.membership_id,
            )
            if row is None:
                raise LookupError("Không tìm thấy job trong workspace.")
            return _to_job(row)

    @staticmethod
    def _require_owner(principal: Principal) -> None:
        if type(principal) is not Principal or Role.OWNER not in principal.roles:
            raise PermissionError("Chỉ owner workspace được quản lý replay job.")


def _to_job(row: BacktestReplayJobRecord) -> BacktestReplayJob:
    learning_intent = str(row.request_payload.get("learning_intent", "RESEARCH"))
    return BacktestReplayJob(
        id=row.id,
        status=row.status,
        stage=row.stage,
        progress=row.progress,
        bundle_hash=row.bundle_hash,
        strategy_release_id=row.strategy_release_id,
        execution_interval=row.execution_interval,
        learning_intent=learning_intent,
        created_at=row.created_at,
        updated_at=row.updated_at,
        error_code=row.error_code,
        result_run_ids=(
            ()
            if learning_intent != "RESEARCH"
            else tuple(str(value) for value in row.result_run_ids)
        ),
        cancel_requested=row.cancel_requested,
        cancel_requested_at=row.cancel_requested_at,
        cancel_requested_by=row.cancel_requested_by,
    )


__all__ = [
    "BacktestReplayBundleChoice",
    "BacktestReplayJob",
    "BacktestReplayJobService",
    "BacktestReplayJobSubmission",
]
