from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.application.learning_scoring_deployment import ScorerDeploymentService
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.domain.learning.scoring_deployment import ScorerDeploymentMode


def _principal(
    *,
    role: Role = Role.OWNER,
    mfa_verified_at: datetime | None = None,
    mfa_method: MfaMethod | None = MfaMethod.TOTP,
) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({role}),
        mfa_verified_at=mfa_verified_at or datetime.now(UTC),
        mfa_method=mfa_method,
    )


class _UnusedSessionFactory:
    def __call__(self):
        raise AssertionError("La service ne doit pas ouvrir de DB pour un refus fail-closed.")


def test_scorer_activation_fails_closed_until_authoritative_rights_gateway_exists() -> None:
    facade = ScorerDeploymentService(_UnusedSessionFactory()).for_principal(_principal())

    with pytest.raises(RuntimeError, match="rights gateway"):
        asyncio.run(
            facade.set_mode(
                mode=ScorerDeploymentMode.SHADOW,
                expected_pointer_version=3,
                expected_deployment_revision=2,
                idempotency_key=uuid4(),
                reason="Chờ cổng quyền TRAINING và nguồn WP05.",
            )
        )


@pytest.mark.parametrize(
    ("mfa_method", "verified_at_offset"),
    [
        (None, timedelta(0)),
        (MfaMethod.TOTP, -timedelta(minutes=16)),
        (MfaMethod.TOTP, timedelta(days=1)),
    ],
)
def test_deployment_mutation_rejects_missing_stale_or_future_totp(
    mfa_method: MfaMethod | None,
    verified_at_offset: timedelta,
) -> None:
    principal = _principal(
        mfa_method=mfa_method,
        mfa_verified_at=datetime.now(UTC) + verified_at_offset,
    )
    facade = ScorerDeploymentService(_UnusedSessionFactory()).for_principal(principal)

    with pytest.raises(PermissionError, match="TOTP gần đây"):
        asyncio.run(
            facade.set_mode(
                mode=ScorerDeploymentMode.DISABLED,
                expected_pointer_version=0,
                expected_deployment_revision=0,
                idempotency_key=uuid4(),
                reason="Tắt scorer sau khi rà soát trạng thái.",
            )
        )


def test_deployment_mutation_requires_reviewer_role() -> None:
    facade = ScorerDeploymentService(_UnusedSessionFactory()).for_principal(
        _principal(role=Role.OPERATOR)
    )

    with pytest.raises(PermissionError, match="không có quyền"):
        asyncio.run(
            facade.set_mode(
                mode=ScorerDeploymentMode.DISABLED,
                expected_pointer_version=0,
                expected_deployment_revision=0,
                idempotency_key=uuid4(),
                reason="Tắt scorer sau khi rà soát trạng thái.",
            )
        )
