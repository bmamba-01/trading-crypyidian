"""Chính sách runtime scorer bất biến lấy từ cấu hình đã được duyệt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from trading_bot.application.learning_scorer_evaluation import ScorerTestEvaluationPolicy


class ScorerRuntimePolicyProvider(Protocol):
    """Trả policy chỉ khi version/hash khớp report và deployment hiện hành."""

    def get_policy(
        self,
        *,
        workspace_id: UUID,
        policy_id: str,
        expected_policy_hash: str,
    ) -> ScorerTestEvaluationPolicy | None: ...


@dataclass(frozen=True, slots=True)
class ConfiguredScorerRuntimePolicyProvider:
    """Bọc policy cấu hình; không dựng lại ngưỡng từ một hash."""

    policy: ScorerTestEvaluationPolicy

    def __post_init__(self) -> None:
        if type(self.policy) is not ScorerTestEvaluationPolicy:
            raise TypeError("Policy runtime phải là ScorerTestEvaluationPolicy typed.")

    @classmethod
    def from_settings(cls, settings: object) -> ConfiguredScorerRuntimePolicyProvider:
        factory = getattr(settings, "scorer_test_evaluation_policy", None)
        if not callable(factory):
            raise TypeError("AppSettings chưa cung cấp policy scorer đã cấu hình.")
        return cls(factory())

    def get_policy(
        self,
        *,
        workspace_id: UUID,
        policy_id: str,
        expected_policy_hash: str,
    ) -> ScorerTestEvaluationPolicy | None:
        if type(workspace_id) is not UUID:
            return None
        if (
            self.policy.policy_version != policy_id
            or self.policy.policy_hash != expected_policy_hash
        ):
            return None
        return self.policy


__all__ = ["ConfiguredScorerRuntimePolicyProvider", "ScorerRuntimePolicyProvider"]
