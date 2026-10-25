"""Lớp strategy release và cấu hình bot đã publish cho bot QA A/B (tích hợp MB-02/MB-03).

Ruling tích hợp: fan-out evidence (MB-03) chỉ dành cho bot có cấu hình bot PUBLISHED, nên
bot A và B của workspace QA nhận overlay với strategy release khác nhau để dữ liệu QA đi
được qua fan-out tới cả hai bot. Bot C (DISABLED) và bot OTHER giữ root-fallback của MB-02.
Release được publish trong cả hai workspace với hash của registry built-in để resolver chấp
nhận (cùng quy tắc `_evaluate_strategy` của MB-02).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from tests.fixtures.qa_dataset.identity import (
    QA_OTHER_WORKSPACE_KEY,
    QA_WORKSPACE_KEY,
    SEEDED_AT,
    qa_id,
)

from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
from trading_bot.domain.strategy.registry import StrategyRegistry

QA_STRATEGY_RELEASES = ("h01-v1", "h03-v1")


@dataclass(frozen=True, slots=True)
class QaStrategyRelease:
    """Hàng `strategy_release` PUBLISHED khớp hash registry built-in."""

    id: UUID
    workspace_key: str
    name: str
    version: int
    status: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class QaBotConfig:
    """Overlay baseline của một bot QA; version do repository MB-02 cấp khi publish."""

    workspace_key: str
    account_key: str
    release_id: str
    overlay_json: dict[str, Any]
    overlay_hash: str
    created_by_member_key: str
    approved_by_member_key: str
    effective_at: datetime


def build_strategy_releases() -> tuple[QaStrategyRelease, ...]:
    registry = StrategyRegistry()
    return tuple(
        QaStrategyRelease(
            id=qa_id("strategy_release", f"{workspace_key}:{name}:1"),
            workspace_key=workspace_key,
            name=name,
            version=1,
            status="PUBLISHED",
            content_hash=registry.get_release(name).content_hash,
        )
        for workspace_key in (QA_WORKSPACE_KEY, QA_OTHER_WORKSPACE_KEY)
        for name in QA_STRATEGY_RELEASES
    )


# A: release mặc định của root (H03) nhưng siết risk mỗi lệnh; B: release H01 khác root.
_BASELINE_OVERLAYS: dict[str, dict[str, Any]] = {
    "A": {
        "schema_version": 1,
        "strategy": {"release_id": "h03-v1", "parameters": {}},
        "risk": {"per_trade_risk_fraction": "0.005"},
    },
    "B": {
        "schema_version": 1,
        "strategy": {"release_id": "h01-v1", "parameters": {}},
        "risk": {"max_positions": 1},
    },
}


def build_bot_configs() -> tuple[QaBotConfig, ...]:
    configs = []
    for account_key, raw in _BASELINE_OVERLAYS.items():
        overlay = BotConfigOverlay.model_validate(raw)
        payload = canonical_overlay(overlay)
        if overlay.strategy is None:
            raise ValueError(f"Overlay QA của bot {account_key} phải chọn strategy release.")
        configs.append(
            QaBotConfig(
                workspace_key=QA_WORKSPACE_KEY,
                account_key=account_key,
                release_id=overlay.strategy.release_id,
                overlay_json=payload,
                overlay_hash=overlay_hash(payload),
                created_by_member_key=f"{QA_WORKSPACE_KEY}.strategy_admin",
                approved_by_member_key=f"{QA_WORKSPACE_KEY}.owner",
                effective_at=SEEDED_AT,
            )
        )
    return tuple(configs)
