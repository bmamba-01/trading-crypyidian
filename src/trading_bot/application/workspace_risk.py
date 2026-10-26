"""Application service điều phối kiểm tra và giữ ngân sách rủi ro Multi-Bot (MB04.5).

Tuân thủ Ruling 10, Ruling 7, Ruling 11 và Ruling 12:
- Khóa 2 cấp advisory lock: workspace lock trước, account lock sau (sắp xếp UUID tăng dần).
- Sau khi có lock, re-read trạng thái root cap, account cap, bot cap, equity snapshot và exposure.
- Đánh giá ngân sách thuần qua WorkspaceBudgetEngine.
- Persist quyết định và reservation trong cùng transaction; idempotent replay và conflict detection.
- Các transition vòng đời (transfer, release, mark_unknown, expire_overdue) chống double-count.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    RiskDecisionRecord,
    RiskReservationRecord,
    TradeRecord,
)
from trading_bot.adapters.database.repositories.account_equity import (
    AccountEquityRepository,
)
from trading_bot.adapters.database.repositories.bot_instances import BotInstanceRepository
from trading_bot.adapters.database.repositories.risk_conversion import (
    RiskConversionRepository,
)
from trading_bot.adapters.database.repositories.risk_reservations import (
    RiskReservationRepository,
)
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
)
from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    load_effective_bot_config,
)
from trading_bot.application.runtime_risk_profile import (
    RiskProfileRuntimeError,
    load_risk_profile,
)
from trading_bot.domain.risk.equity import (
    AccountEquitySnapshot,
    RiskConversionObservation,
)
from trading_bot.domain.risk.reservations import (
    ReservationConflict,
    ReservationReleaseReason,
    RiskReservation,
)
from trading_bot.domain.risk.reservations import (
    ReservationStatus as SingleBotReservationStatus,
)
from trading_bot.domain.risk.workspace_budget import (
    AccountScopeInput,
    BudgetConflictError,
    BudgetOperation,
    BudgetReasonCode,
    BudgetReservation,
    PositionRisk,
    WorkspaceBudgetEngine,
    WorkspacePolicy,
    WorkspaceRiskDecision,
)
from trading_bot.domain.risk.workspace_budget import (
    ReservationStatus as BudgetReservationStatus,
)

ReservationStatus = BudgetReservationStatus
ZERO = Decimal("0")


def _canonical_value(value: object) -> object:
    """Chuẩn hóa dữ liệu để hashing và lưu payload nhất quán."""
    if isinstance(value, Decimal):
        text = format(value, "f")
        return "0" if value == ZERO else text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, (datetime, timedelta)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canonical_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): _canonical_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    """Yêu cầu giữ ngân sách rủi ro cho một lệnh intent mới."""

    workspace_id: UUID
    bot_id: UUID
    account_id: UUID
    idempotency_key: str
    requested_risk_usdt: Decimal
    as_of: datetime
    expires_at: datetime
    symbol: str = "BTCUSDT"
    requested_notional: Decimal = Decimal("0")
    requested_initial_margin: Decimal = Decimal("0")
    plan_hash: str = ""
    candidate_id: str = ""
    operation: BudgetOperation = BudgetOperation.NEW_ENTRY

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key không được để trống.")
        if (
            not isinstance(self.requested_risk_usdt, Decimal)
            or not self.requested_risk_usdt.is_finite()
        ):
            raise ValueError("requested_risk_usdt phải là Decimal hữu hạn.")
        if self.requested_risk_usdt <= ZERO:
            raise ValueError("requested_risk_usdt phải lớn hơn 0.")
        for name, value in (
            ("requested_notional", self.requested_notional),
            ("requested_initial_margin", self.requested_initial_margin),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
                raise ValueError(f"{name} phải là Decimal hữu hạn không âm.")
            if value == ZERO:
                raise ValueError(f"{name} phải lớn hơn 0.")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() != timedelta(0):
            raise ValueError("as_of phải là UTC aware datetime.")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() != timedelta(0):
            raise ValueError("expires_at phải là UTC aware datetime.")
        if self.expires_at <= self.as_of:
            raise ValueError("expires_at phải sau as_of.")


@dataclass(frozen=True, slots=True)
class ReservationResult:
    """Kết quả phản hồi của yêu cầu cấp giữ ngân sách."""

    approved: bool
    reason_code: BudgetReasonCode
    detail_vi: str
    decision_id: UUID | None = None
    reservation_id: UUID | None = None
    decision: WorkspaceRiskDecision | None = None
    reservation: RiskReservation | None = None
    is_replay: bool = False


def _request_payload_hash(request: ReservationRequest) -> str:
    """Tạo dấu vân tay ổn định cho toàn bộ payload yêu cầu reservation."""
    payload = {
        "workspace_id": request.workspace_id,
        "bot_id": request.bot_id,
        "account_id": request.account_id,
        "idempotency_key": request.idempotency_key,
        "requested_risk_usdt": request.requested_risk_usdt,
        "as_of": request.as_of,
        "expires_at": request.expires_at,
        "symbol": request.symbol,
        "requested_notional": request.requested_notional,
        "requested_initial_margin": request.requested_initial_margin,
        "plan_hash": request.plan_hash,
        "candidate_id": request.candidate_id,
        "operation": request.operation,
    }
    canonical = json.dumps(_canonical_value(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkspaceRiskCoordinator:
    """Dịch vụ ứng dụng điều phối kiểm tra ngân sách 3 cấp và khóa nguyên tử 2 cấp."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] | None = None,
        engine: WorkspaceBudgetEngine | None = None,
        settings_repo: Any = None,
        equity_repo: Any = None,
        conversion_repo: Any = None,
        reservations_repo: Any = None,
        target_status_loader: Any = None,
        test_scope_caps_loader: Callable[
            [ReservationRequest], tuple[Decimal, Decimal | None, str, str]
        ]
        | None = None,
    ) -> None:
        self._session = session
        self._clock = clock or (lambda: datetime.now(UTC))
        self._engine = engine or WorkspaceBudgetEngine()
        self._settings_repo = settings_repo or WorkspaceSettingsRepository(session)
        self._equity_repo = equity_repo or AccountEquityRepository(session)
        self._conversion_repo = conversion_repo or RiskConversionRepository(session)
        self._reservations_repo = reservations_repo or RiskReservationRepository(
            session, clock=self._clock
        )
        self._target_status_loader = target_status_loader
        test_scope_caps_loader = test_scope_caps_loader or getattr(
            session, "test_scope_caps_loader", None
        )
        if isinstance(session, AsyncSession) and (
            test_scope_caps_loader is not None or target_status_loader is not None
        ):
            raise ValueError("Không được thay nguồn scope/status trên AsyncSession production.")
        self._test_scope_caps_loader = test_scope_caps_loader

    async def acquire_locks(self, workspace_id: UUID, account_ids: Sequence[UUID]) -> None:
        """Thực hiện khóa advisory 2 cấp theo Ruling 10: workspace trước -> accounts tăng dần."""
        # Cấp 1: Workspace lock
        await self._reservations_repo.lock_workspace_risk(workspace_id)
        # Cấp 2: Account locks sắp xếp UUID tăng dần
        await self._reservations_repo.lock_accounts_risk(workspace_id, account_ids)

    async def reserve(
        self,
        request: ReservationRequest,
    ) -> ReservationResult:
        """Đánh giá và lưu reservation nguyên tử theo 4 bước Ruling 10."""
        # ---------------------------------------------------------------------
        # Bước 1: Khóa 2 cấp Advisory Lock
        # ---------------------------------------------------------------------
        await self.acquire_locks(request.workspace_id, [request.account_id])

        # ---------------------------------------------------------------------
        # Bước 2: Idempotency check & Re-read trạng thái sau khóa
        # ---------------------------------------------------------------------
        # 2a. Kiểm tra nếu reservation đã tồn tại với cùng idempotency key
        existing_res = await self._reservations_repo.get_by_idempotency_key(
            workspace_id=request.workspace_id,
            bot_id=request.bot_id,
            account_id=request.account_id,
            idempotency_key=request.idempotency_key,
        )
        existing_dec = await self._reservations_repo.get_decision_by_idempotency_key(
            workspace_id=request.workspace_id,
            bot_id=request.bot_id,
            account_id=request.account_id,
            idempotency_key=request.idempotency_key,
        )
        if existing_res is not None:
            # Chỉ replay nếu fingerprint toàn payload khớp; thiếu lineage cũ thì fail closed.
            if (
                existing_res.plan_hash == request.plan_hash
                and existing_dec is not None
                and isinstance(existing_dec.payload, dict)
                and existing_dec.payload.get("request_payload_hash")
                == _request_payload_hash(request)
            ):
                return ReservationResult(
                    approved=True,
                    reason_code=BudgetReasonCode.APPROVED,
                    detail_vi="Phát lại (replay) kết quả reservation đã được duyệt trước đó.",
                    decision_id=existing_res.risk_decision_id,
                    reservation_id=existing_res.id,
                    reservation=self._to_reservation_domain(existing_res),
                    is_replay=True,
                )
            raise BudgetConflictError(
                f"Idempotency key '{request.idempotency_key}' đã được dùng cho payload khác."
            )

        # 2b. Kiểm tra nếu quyết định từ chối đã tồn tại với cùng idempotency key
        if existing_dec is not None:
            if (
                existing_dec.plan_hash == request.plan_hash
                and isinstance(existing_dec.payload, dict)
                and existing_dec.payload.get("request_payload_hash")
                == _request_payload_hash(request)
            ):
                return ReservationResult(
                    approved=False,
                    reason_code=BudgetReasonCode(existing_dec.reason_code),
                    detail_vi=existing_dec.detail_vi,
                    decision_id=existing_dec.id,
                    is_replay=True,
                )
            raise BudgetConflictError(
                f"Idempotency key '{request.idempotency_key}' đã dùng cho decision từ chối khác."
            )

        # 2c. Re-read root settings mới nhất
        policy = await self._load_policy(request.workspace_id, as_of=request.as_of)
        target_enabled = await self._target_is_enabled(request)
        bot_config_version_id = ""
        bot_config_hash = ""
        if isinstance(self._session, AsyncSession):
            (
                account_max_open_risk_fraction,
                bot_max_open_risk_fraction,
                bot_config_version_id,
                bot_config_hash,
            ) = await self._load_authoritative_caps(request)
        elif self._test_scope_caps_loader is not None:
            (
                account_max_open_risk_fraction,
                bot_max_open_risk_fraction,
                bot_config_version_id,
                bot_config_hash,
            ) = self._test_scope_caps_loader(request)
        else:
            raise ValueError("Coordinator cần AsyncSession hoặc test scope caps loader.")

        # 2d. Re-read accounts & bot scopes
        accounts = await self._load_account_scopes(
            request.workspace_id,
            request.bot_id,
            request.account_id,
            account_max_open_risk_fraction,
            bot_max_open_risk_fraction,
            request.as_of,
            policy=policy,
            enabled=target_enabled,
            bot_config_version_id=bot_config_version_id,
            bot_config_hash=bot_config_hash,
        )

        # 2e. Re-read positions và active reservations
        open_positions = await self._load_open_positions(request.workspace_id)
        db_reservations = await self._reservations_repo.list_active_reservations(
            workspace_id=request.workspace_id
        )
        active_reservations = [self.to_budget_reservation(r) for r in db_reservations]

        workspace_aggregate = await self._reservations_repo.get_workspace_risk_aggregate(
            workspace_id=request.workspace_id,
            as_of=request.as_of,
            max_mark_age=policy.max_mark_age,
        )

        # ---------------------------------------------------------------------
        # Bước 3: Đánh giá qua WorkspaceBudgetEngine.evaluate
        # ---------------------------------------------------------------------
        decision = self._engine.evaluate(
            policy=policy,
            accounts=accounts,
            positions=open_positions,
            reservations=active_reservations,
            workspace_aggregate=workspace_aggregate,
            target_bot_id=str(request.bot_id),
            requested_risk_usdt=request.requested_risk_usdt,
            as_of=request.as_of,
            operation=request.operation,
            candidate_id=request.candidate_id or str(uuid4()),
            idempotency_key=request.idempotency_key,
            plan_hash=request.plan_hash,
        )

        # ---------------------------------------------------------------------
        # Bước 4: Persist kết quả & Idempotency
        # ---------------------------------------------------------------------
        decision_id = uuid4()
        decision_payload = _canonical_value(decision)
        if isinstance(decision_payload, dict):
            decision_payload["idempotency_key"] = request.idempotency_key
            decision_payload["request_payload_hash"] = _request_payload_hash(request)
            decision_payload["candidate_expires_at"] = request.expires_at.isoformat()

        decision_key = hashlib.sha256(
            json.dumps(decision_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        decision_record = RiskDecisionRecord(
            id=decision_id,
            bot_id=request.bot_id,
            workspace_id=request.workspace_id,
            account_id=request.account_id,
            decision_key=decision_key,
            plan_hash=request.plan_hash,
            snapshot_id=decision.snapshot_ids[0] if decision.snapshot_ids else "",
            approved=decision.approved,
            reason_code=decision.reason_code.value,
            detail_vi=decision.detail_vi,
            payload=decision_payload if isinstance(decision_payload, dict) else {},
        )
        self._session.add(decision_record)

        if not decision.approved:
            await self._session.flush()
            return ReservationResult(
                approved=False,
                reason_code=decision.reason_code,
                detail_vi=decision.detail_vi,
                decision_id=decision_id,
                decision=decision,
            )

        # Nếu được duyệt: Ghi bản ghi risk_reservation
        reservation_id = uuid4()
        reservation_record = RiskReservationRecord(
            id=reservation_id,
            bot_id=request.bot_id,
            workspace_id=request.workspace_id,
            account_id=request.account_id,
            risk_decision_id=decision_id,
            idempotency_key=request.idempotency_key,
            plan_hash=request.plan_hash,
            symbol=request.symbol,
            reserved_risk=request.requested_risk_usdt,
            reserved_notional=request.requested_notional,
            reserved_initial_margin=request.requested_initial_margin,
            expires_at=request.expires_at,
            status=ReservationStatus.RESERVED.value,
            resolution_reason=None,
        )
        self._session.add(reservation_record)
        await self._session.flush()

        return ReservationResult(
            approved=True,
            reason_code=decision.reason_code,
            detail_vi=decision.detail_vi,
            decision_id=decision_id,
            reservation_id=reservation_id,
            decision=decision,
            reservation=self._to_reservation_domain(reservation_record),
        )

    async def transfer_to_open(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        reservation_id: UUID,
        trade_id: UUID | None = None,
    ) -> RiskReservation:
        """Chuyển reservation sang TRANSFERRED và ghi nhận OPEN_RISK_RECORDED."""
        await self.acquire_locks(workspace_id, [account_id])
        record = await self._get_reservation_locked(workspace_id, account_id, reservation_id)

        if record.status == ReservationStatus.TRANSFERRED.value:
            if record.resolution_reason == ReservationReleaseReason.OPEN_RISK_RECORDED.value:
                return self._to_reservation_domain(record)
            raise ReservationConflict("Reservation TRANSFERRED có resolution reason không khớp.")

        if record.status not in (ReservationStatus.RESERVED.value, ReservationStatus.UNKNOWN.value):
            raise ReservationConflict(
                f"Chỉ reservation RESERVED/UNKNOWN mới transfer sang open risk; "
                f"hiện tại là {record.status}."
            )

        record.status = ReservationStatus.TRANSFERRED.value
        record.resolution_reason = ReservationReleaseReason.OPEN_RISK_RECORDED.value
        await self._session.flush()
        return self._to_reservation_domain(record)

    async def release_reservation(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        reservation_id: UUID,
        reason: ReservationReleaseReason | str = ReservationReleaseReason.CANCELLED_BEFORE_SUBMIT,
    ) -> RiskReservation:
        """Giải phóng reservation sang RELEASED để trả lại ngân sách rủi ro."""
        await self.acquire_locks(workspace_id, [account_id])
        record = await self._get_reservation_locked(workspace_id, account_id, reservation_id)

        target_reason = (
            reason.value if isinstance(reason, ReservationReleaseReason) else str(reason)
        )

        if record.status == ReservationStatus.RELEASED.value:
            if record.resolution_reason == target_reason:
                return self._to_reservation_domain(record)
            raise ReservationConflict("Reservation đã RELEASED với lý do khác.")

        if record.status != ReservationStatus.RESERVED.value:
            raise ReservationConflict(
                f"Chỉ reservation RESERVED mới được giải phóng; hiện tại là {record.status}."
            )

        record.status = ReservationStatus.RELEASED.value
        record.resolution_reason = target_reason
        await self._session.flush()
        return self._to_reservation_domain(record)

    async def mark_unknown(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        reservation_id: UUID,
    ) -> RiskReservation:
        """Đánh dấu reservation thành UNKNOWN; ngân sách tiếp tục được giữ."""
        await self.acquire_locks(workspace_id, [account_id])
        record = await self._get_reservation_locked(workspace_id, account_id, reservation_id)

        if record.status == ReservationStatus.UNKNOWN.value:
            return self._to_reservation_domain(record)

        if record.status != ReservationStatus.RESERVED.value:
            raise ReservationConflict(
                f"Chỉ reservation RESERVED mới chuyển sang UNKNOWN; hiện tại là {record.status}."
            )

        record.status = ReservationStatus.UNKNOWN.value
        await self._session.flush()
        return self._to_reservation_domain(record)

    async def expire_overdue(
        self,
        *,
        workspace_id: UUID,
        as_of: datetime,
        account_ids: Sequence[UUID] | None = None,
    ) -> int:
        """Giải phóng các reservation RESERVED đã quá hạn as_of; bỏ qua UNKNOWN."""
        await self._reservations_repo.lock_workspace_risk(workspace_id)
        if account_ids:
            await self._reservations_repo.lock_accounts_risk(workspace_id, account_ids)
        return await self._reservations_repo.expire_overdue(
            workspace_id=workspace_id, as_of=as_of, account_ids=account_ids
        )

    def build_account_scope(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        account_cap: Decimal,
        bot_cap: Decimal | None = None,
        snapshot: AccountEquitySnapshot | None = None,
        conversion: RiskConversionObservation | None = None,
        enabled: bool = True,
    ) -> AccountScopeInput:
        """Dựng AccountScopeInput cho các bot/account khác trong cùng workspace."""
        return AccountScopeInput(
            workspace_id=str(workspace_id),
            bot_id=str(bot_id),
            account_id=str(account_id),
            enabled=enabled,
            account_max_open_risk_fraction=account_cap,
            bot_max_open_risk_fraction=bot_cap,
            snapshot=snapshot,
            conversion=conversion,
            risk_profile_version_id="",
        )

    # -------------------------------------------------------------------------
    # Helper nạp dữ liệu Re-read sau khóa
    # -------------------------------------------------------------------------

    async def _load_policy(
        self, workspace_id: UUID, *, as_of: datetime | None = None
    ) -> WorkspacePolicy:
        published = await self._settings_repo.load_effective_config(
            workspace_id=workspace_id, as_of=as_of
        )
        if published is None:
            raise ValueError(f"Không tìm thấy cấu hình root hợp lệ cho workspace {workspace_id}.")
        risk = published.settings.risk
        return WorkspacePolicy(
            workspace_id=str(workspace_id),
            root_workspace_max_open_risk_fraction=risk.workspace_max_open_risk_fraction,
            root_settings_version_id=str(published.settings_version_id),
            root_settings_hash=published.settings_hash,
            max_equity_age=timedelta(seconds=risk.max_equity_age_seconds),
            max_conversion_age=timedelta(seconds=risk.max_conversion_age_seconds),
            risk_profile_version_id=str(risk.profile_version_id),
            max_mark_age=timedelta(seconds=risk.max_mark_age_seconds),
        )

    async def _load_account_scopes(
        self,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        account_cap: Decimal,
        bot_cap: Decimal | None,
        as_of: datetime,
        policy: WorkspacePolicy | None = None,
        enabled: bool = True,
        bot_config_version_id: str = "",
        bot_config_hash: str = "",
    ) -> list[AccountScopeInput]:
        # Nạp snapshot và conversion cho target account
        effective_policy = policy or await self._load_policy(workspace_id, as_of=as_of)
        snap = await self._equity_repo.load_paper_equity_snapshot(
            workspace_id=workspace_id,
            bot_id=bot_id,
            account_id=account_id,
            as_of=as_of,
            max_equity_age=effective_policy.max_equity_age,
            max_mark_age=effective_policy.max_mark_age,
        )
        conv = await self._conversion_repo.get_conversion_observation(
            workspace_id=workspace_id,
            base_asset=snap.asset if snap is not None else "USDT",
            quote_asset="USDT",
            as_of=as_of,
            max_age=effective_policy.max_conversion_age,
        )

        acc_fraction = account_cap
        bot_fraction = bot_cap

        target_scope = AccountScopeInput(
            workspace_id=str(workspace_id),
            bot_id=str(bot_id),
            account_id=str(account_id),
            enabled=enabled,
            account_max_open_risk_fraction=acc_fraction,
            bot_max_open_risk_fraction=bot_fraction,
            snapshot=snap,
            conversion=conv,
            risk_profile_version_id=effective_policy.risk_profile_version_id,
            bot_config_version_id=bot_config_version_id,
            bot_config_hash=bot_config_hash,
        )

        return [target_scope]

    async def _target_is_enabled(self, request: ReservationRequest) -> bool:
        """Kiểm tra lại identity/status của bot và PAPER account trong transaction hiện tại."""
        if self._target_status_loader is not None:
            return bool(
                await self._target_status_loader(
                    workspace_id=request.workspace_id,
                    bot_id=request.bot_id,
                    account_id=request.account_id,
                )
            )
        # Session giả không đại diện cho PostgreSQL identity trong unit tests.
        if not isinstance(self._session, AsyncSession):
            return True
        linked = await BotInstanceRepository(self._session).get_with_account(
            workspace_id=request.workspace_id, bot_id=request.bot_id
        )
        if linked is None:
            return False
        bot, account = linked
        return (
            account.id == request.account_id
            and account.mode == "PAPER"
            and account.status == "ENABLED"
            and bot.status == "ENABLED"
        )

    async def _load_authoritative_caps(
        self, request: ReservationRequest
    ) -> tuple[Decimal, Decimal | None, str, str]:
        """Nạp lại root profile và bot overlay sau workspace/account risk locks."""
        try:
            published = await self._settings_repo.load_effective_config(
                workspace_id=request.workspace_id, as_of=request.as_of
            )
            if published is None or published.workspace_id != request.workspace_id:
                raise ValueError("Root workspace settings không hiệu lực tại as_of.")
            profile = await load_risk_profile(
                self._session,
                workspace_id=request.workspace_id,
                profile_id=published.settings.risk.profile_version_id,
            )
            effective = await load_effective_bot_config(
                self._session,
                workspace_id=request.workspace_id,
                account_id=request.account_id,
                as_of=request.as_of,
                published=published,
            )
        except (RiskProfileRuntimeError, BotConfigResolutionError) as error:
            raise ValueError("Root risk profile hoặc bot overlay không hợp lệ.") from error
        if effective is not None and (
            effective.workspace_id != request.workspace_id
            or effective.bot_id != request.bot_id
            or effective.account_id != request.account_id
            or effective.settings_version_id != published.settings_version_id
            or not effective.verify_hash()
        ):
            raise ValueError("Effective bot configuration lệch scope/version.")
        return (
            profile.limits.account_max_open_risk_fraction,
            effective.risk_limits.account_max_open_risk_fraction if effective else None,
            str(effective.bot_config_version_id) if effective else "",
            effective.bot_config_hash if effective else "",
        )

    async def _load_open_positions(self, workspace_id: UUID) -> list[PositionRisk]:
        """Nạp danh sách các vị thế OPEN từ database."""
        result = await self._session.scalars(
            select(TradeRecord).where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.status == "OPEN",
            )
        )
        positions: list[PositionRisk] = []
        for trade in result.all():
            worst_case = ZERO
            if trade.lineage and isinstance(trade.lineage, dict):
                raw_loss = trade.lineage.get(
                    "worst_case_loss_usdt", trade.lineage.get("requested_risk", ZERO)
                )
                worst_case = Decimal(str(raw_loss))
            positions.append(
                PositionRisk(
                    workspace_id=str(trade.workspace_id),
                    bot_id=str(trade.bot_id),
                    account_id=str(trade.account_id),
                    position_id=str(trade.id),
                    symbol=trade.symbol,
                    worst_case_loss_usdt=worst_case,
                )
            )
        return positions

    async def _get_reservation_locked(
        self, workspace_id: UUID, account_id: UUID, reservation_id: UUID
    ) -> RiskReservationRecord:
        record = await self._session.scalar(
            select(RiskReservationRecord)
            .where(
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.account_id == account_id,
                RiskReservationRecord.id == reservation_id,
            )
            .with_for_update()
        )
        if record is None:
            raise ReservationConflict("Không tìm thấy reservation trong workspace/account.")
        return record

    def to_budget_reservation(self, record: RiskReservationRecord) -> BudgetReservation:
        """Chuyển bản ghi RiskReservationRecord sang domain BudgetReservation."""
        return BudgetReservation(
            reservation_id=str(record.id),
            workspace_id=str(record.workspace_id),
            bot_id=str(record.bot_id),
            account_id=str(record.account_id),
            idempotency_key=record.idempotency_key,
            reserved_risk_usdt=Decimal(str(record.reserved_risk)),
            status=BudgetReservationStatus(record.status),
            expires_at=record.expires_at,
            plan_hash=record.plan_hash,
        )

    def _to_reservation_domain(self, record: RiskReservationRecord) -> RiskReservation:
        """Chuyển RiskReservationRecord sang RiskReservation domain DTO."""
        reason = (
            ReservationReleaseReason(record.resolution_reason)
            if record.resolution_reason is not None
            else None
        )
        return RiskReservation(
            reservation_id=record.id,
            workspace_id=str(record.workspace_id),
            account_id=str(record.account_id),
            idempotency_key=record.idempotency_key,
            plan_hash=record.plan_hash,
            reserved_risk=Decimal(str(record.reserved_risk)),
            status=SingleBotReservationStatus(record.status),
            expires_at=record.expires_at,
            symbol=record.symbol,
            reserved_notional=Decimal(str(record.reserved_notional)),
            resolution_reason=reason,
            reserved_initial_margin=Decimal(str(record.reserved_initial_margin)),
        )
