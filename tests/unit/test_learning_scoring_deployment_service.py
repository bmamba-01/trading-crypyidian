from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tests.unit.test_learning_scorer_runtime import _policy

from trading_bot.application import learning_scoring_deployment as service_module
from trading_bot.application.learning_scoring_deployment import ScorerDeploymentService
from trading_bot.application.scorer_runtime_policy import ConfiguredScorerRuntimePolicyProvider
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
                evaluation_report_id=uuid4(),
                expected_pointer_version=3,
                expected_deployment_revision=2,
                idempotency_key=uuid4(),
                reason="Chờ cổng quyền TRAINING và nguồn WP05.",
            )
        )


def test_active_service_requires_selected_persisted_report_before_database_access() -> None:
    facade = ScorerDeploymentService(_UnusedSessionFactory()).for_principal(_principal())

    with pytest.raises(ValueError, match="evaluation_report_id đã lưu"):
        asyncio.run(
            facade.set_mode(
                mode=ScorerDeploymentMode.SHADOW,
                evaluation_report_id=None,
                expected_pointer_version=3,
                expected_deployment_revision=2,
                idempotency_key=uuid4(),
                reason="Chỉ dùng report đã lưu, không nhận ngưỡng từ caller.",
            )
        )


def test_active_service_forwards_exact_report_id_and_cas_to_authoritative_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_id = uuid4()
    expected_pointer_version = 7
    expected_deployment_revision = 3
    idempotency_key = uuid4()
    event = object()
    captured: dict[str, object] = {}

    class FakeUnitOfWork:
        def __init__(self, *_args, **_kwargs) -> None:
            self.session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

    class FakeRepository:
        def __init__(self, _session) -> None:
            pass

        async def append_active_event(self, **kwargs):
            captured.update(kwargs)
            return event

    monkeypatch.setattr(service_module, "UnitOfWork", FakeUnitOfWork)
    monkeypatch.setattr(service_module, "LearningScoringDeploymentRepository", FakeRepository)
    policy_provider = ConfiguredScorerRuntimePolicyProvider(_policy())
    facade = ScorerDeploymentService(
        object(),
        policy_provider=policy_provider,  # type: ignore[arg-type]
    ).for_principal(_principal())

    result = asyncio.run(
        facade.set_mode(
            mode=ScorerDeploymentMode.SHADOW,
            evaluation_report_id=report_id,
            expected_pointer_version=expected_pointer_version,
            expected_deployment_revision=expected_deployment_revision,
            idempotency_key=idempotency_key,
            reason="Duyệt report đã lưu theo CAS và quyền hiện hành.",
        )
    )

    assert result is event
    assert captured == {
        "workspace_id": facade._principal.workspace_id,
        "actor_membership_id": facade._principal.membership_id,
        "mfa_verified_at": facade._principal.mfa_verified_at,
        "mode": ScorerDeploymentMode.SHADOW,
        "evaluation_report_id": report_id,
        "expected_pointer_version": expected_pointer_version,
        "expected_deployment_revision": expected_deployment_revision,
        "idempotency_key": idempotency_key,
        "reason": "Duyệt report đã lưu theo CAS và quyền hiện hành.",
        "policy_provider": policy_provider,
    }
    assert not {"report_hash", "proof_hash", "policy_hash", "rights"}.intersection(captured)


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
