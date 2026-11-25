"""Adapter PostgreSQL cho server-side release gate G0–G5 (OPS07F / Task 10 / CRY-468).

Ghi nhận mọi quyết định đánh giá hồ sơ G0–G5 kèm `dossier_hash` bất biến vào `risk_decision`
và khóa `bot_runtime_state.readiness = 'RECOVERY_REQUIRED'` khi bị từ chối.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.bot_runtime_entities import BotRuntimeStateRecord
from trading_bot.adapters.database.models.risk_entities import RiskDecisionRecord, TradePlanRecord
from trading_bot.domain.execution.live_readiness_dossier import (
    LiveReadinessDossier,
    LiveReadinessGateRequest,
    LiveReadinessGateVerdict,
    evaluate_live_readiness_gate,
)


class SqlAlchemyLiveReadinessGateRepository:
    """Thực thi server-side gate G0–G5 và ghi bằng chứng kiểm toán bất biến lên PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def evaluate_and_record(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        dossier: LiveReadinessDossier,
        request: LiveReadinessGateRequest,
        signing_key: str,
        as_of: datetime,
        evaluation_key: str,
    ) -> tuple[LiveReadinessGateVerdict, RiskDecisionRecord]:
        verdict = evaluate_live_readiness_gate(
            dossier=dossier,
            request=request,
            signing_key=signing_key,
            as_of=as_of,
        )

        # Đảm bảo tồn tại trade_plan đại diện cho dossier_hash (bất biến theo khóa hash)
        plan = await self._session.scalar(
            select(TradePlanRecord).where(
                TradePlanRecord.workspace_id == workspace_id,
                TradePlanRecord.account_id == account_id,
                TradePlanRecord.plan_hash == verdict.dossier_hash,
            )
        )
        if plan is None:
            self._session.add(
                TradePlanRecord(
                    workspace_id=workspace_id,
                    bot_id=bot_id,
                    account_id=account_id,
                    plan_hash=verdict.dossier_hash,
                    symbol=request.symbols[0],
                    notional=Decimal("1"),
                    payload={
                        "kind": "live_readiness_dossier_g0_g5",
                        "schema_version": dossier.schema_version,
                        "build_hash": dossier.build_hash,
                    },
                )
            )
            await self._session.flush()

        # Lưu quyết định đánh giá bất biến (approved luôn là False cho dispatch vì G6 tách biệt)
        decision = RiskDecisionRecord(
            id=uuid4(),
            workspace_id=workspace_id,
            bot_id=bot_id,
            account_id=account_id,
            decision_key=f"dossier-g0g5-{evaluation_key}",
            plan_hash=verdict.dossier_hash,
            snapshot_id=f"dossier-{as_of.isoformat()}",
            approved=verdict.all_g0_g5_passed,
            reason_code=verdict.reason_code.value,
            detail_vi=verdict.detail_vi,
            payload={
                "dossier_hash": verdict.dossier_hash,
                "live_dispatch_allowed": verdict.live_dispatch_allowed,
                "all_g0_g5_passed": verdict.all_g0_g5_passed,
                "gates": {
                    g.value: {"passed": r.passed, "reason_code": r.reason_code.value}
                    for g, r in verdict.gate_results.items()
                },
            },
        )
        self._session.add(decision)

        # Khi bất kỳ cổng nào thất bại -> đóng entry gate tại bot_runtime_state
        if not verdict.all_g0_g5_passed:
            state = await self._session.scalar(
                select(BotRuntimeStateRecord).where(
                    BotRuntimeStateRecord.workspace_id == workspace_id,
                    BotRuntimeStateRecord.bot_id == bot_id,
                    BotRuntimeStateRecord.account_id == account_id,
                )
            )
            if state is not None:
                state.readiness = "RECOVERY_REQUIRED"
                state.reason_code = verdict.reason_code.value

        await self._session.flush()
        return verdict, decision
