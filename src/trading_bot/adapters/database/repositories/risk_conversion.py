"""Risk conversion repository quản lý tỷ giá quy đổi rủi ro về USDT (MB04.4)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from trading_bot.domain.risk.equity import (
    EquityReasonCode,
    RiskConversionObservation,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class RiskConversionRepository:
    """Nạp observation tỷ giá quy đổi rủi ro về USDT; từ chối ghép multi-hop và tự fallback."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        observations: Sequence[RiskConversionObservation] | None = None,
    ) -> None:
        self._session = session
        self._observations: list[RiskConversionObservation] = list(observations or [])

    def register_observation(self, observation: RiskConversionObservation) -> None:
        """Đăng ký observation có provenance vào repository."""
        self._observations.append(observation)

    async def get_conversion_observation(
        self,
        *,
        workspace_id: UUID,
        base_asset: str,
        quote_asset: str = "USDT",
        as_of: datetime,
        max_age: timedelta,
    ) -> RiskConversionObservation:
        """Nạp observation quy đổi rủi ro trực tiếp; fail-closed nếu không đạt chuẩn."""
        _validate_aware(as_of, "as_of")
        base = base_asset.upper().strip()
        quote = quote_asset.upper().strip()

        # 1. Identity conversion cho USDT/USDT
        if base == "USDT":
            if quote != "USDT":
                return _missing_observation(
                    workspace_id=workspace_id,
                    base_asset=base,
                    quote_asset=quote,
                    as_of=as_of,
                    reason=EquityReasonCode.ROUTE_UNSUPPORTED,
                )
            return RiskConversionObservation(
                workspace_id=str(workspace_id),
                base_asset="USDT",
                quote_asset="USDT",
                rate=Decimal("1.0"),
                as_of=as_of,
                available_at=as_of,
                source_ref="identity",
                source_version="v1",
                source_hash="0" * 64,
                policy_version_id="system_identity",
                route="DIRECT",
                approved=True,
                quality="ACCEPTED",
                known=True,
                unknown_reason=None,
            )

        # 2. Non-USDT: chỉ hỗ trợ quote USDT
        if quote != "USDT":
            return _missing_observation(
                workspace_id=workspace_id,
                base_asset=base,
                quote_asset=quote,
                as_of=as_of,
                reason=EquityReasonCode.ROUTE_UNSUPPORTED,
            )

        # 3. Tìm kiếm trong registered observations
        matches = [
            obs
            for obs in self._observations
            if obs.workspace_id == str(workspace_id)
            and obs.base_asset == base
            and obs.quote_asset == quote
        ]

        if not matches:
            return _missing_observation(
                workspace_id=workspace_id,
                base_asset=base,
                quote_asset=quote,
                as_of=as_of,
                reason=EquityReasonCode.CONVERSION_MISSING,
            )

        # Lọc observation theo thời điểm quan sát
        available_matches = [obs for obs in matches if obs.available_at <= as_of]
        if not available_matches:
            # Tất cả observation đều ở tương lai
            future_obs = sorted(matches, key=lambda o: o.available_at)[0]
            return replace(
                future_obs,
                known=False,
                unknown_reason=EquityReasonCode.OBSERVATION_FUTURE.value,
            )

        # Lấy observation mới nhất còn hiệu lực trước hoặc bằng as_of
        latest = sorted(available_matches, key=lambda o: (o.as_of, o.available_at), reverse=True)[0]

        # Kiểm tra tính chất DIRECT
        if latest.route != "DIRECT":
            return replace(
                latest,
                known=False,
                unknown_reason=EquityReasonCode.ROUTE_UNSUPPORTED.value,
            )

        # Kiểm tra phê duyệt và chất lượng
        if not latest.approved or latest.quality != "ACCEPTED":
            return replace(
                latest,
                known=False,
                unknown_reason=EquityReasonCode.CONVERSION_REJECTED.value,
            )

        # Kiểm tra độ tươi (freshness)
        if as_of - latest.as_of > max_age or as_of - latest.available_at > max_age:
            return replace(
                latest,
                known=False,
                unknown_reason=EquityReasonCode.CONVERSION_STALE.value,
            )

        # Kiểm tra tính hợp lệ của timestamp
        if latest.as_of > latest.available_at:
            return replace(
                latest,
                known=False,
                unknown_reason=EquityReasonCode.TIMESTAMP_INVALID.value,
            )

        # Kiểm tra rate dương
        if latest.rate <= Decimal("0"):
            return replace(
                latest,
                known=False,
                unknown_reason=EquityReasonCode.AMOUNT_INVALID.value,
            )

        return latest


def _validate_aware(dt: datetime, name: str) -> None:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError(f"{name} phải là datetime nhận biết múi giờ UTC.")


def _missing_observation(
    *,
    workspace_id: UUID,
    base_asset: str,
    quote_asset: str,
    as_of: datetime,
    reason: EquityReasonCode,
) -> RiskConversionObservation:
    return RiskConversionObservation(
        workspace_id=str(workspace_id),
        base_asset=base_asset,
        quote_asset=quote_asset,
        rate=Decimal("0"),
        as_of=as_of,
        available_at=as_of,
        source_ref="missing",
        source_version="v0",
        source_hash="",
        policy_version_id="missing",
        route="DIRECT",
        approved=False,
        quality="MISSING",
        known=False,
        unknown_reason=reason.value,
    )
