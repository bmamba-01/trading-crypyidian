from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from trading_bot.adapters.database.repositories import learning_scorer_runtime_committer as module
from trading_bot.adapters.database.repositories.learning_scorer_runtime_committer import (
    PostgresScorerRuntimeCommitter,
)
from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    ScorerDeploymentRightsUnavailable,
)
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentHealth,
    ScorerDeploymentMode,
)

WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
EVENT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ARTIFACT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
REPORT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
REGISTRY_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
PROOF_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
ARTIFACT_HASH = "a" * 64
REPORT_HASH = "b" * 64
PROOF_HASH = "c" * 64
SCOPE_HASH = "d" * 64
POLICY_HASH = "e" * 64
RIGHTS_HASH = "f" * 64


def _snapshot(mode: ScorerDeploymentMode) -> ScorerDeploymentAdminSnapshot:
    return ScorerDeploymentAdminSnapshot(
        workspace_id=WORKSPACE,
        mode=mode,
        deployment_event_id=None if mode is ScorerDeploymentMode.DISABLED else EVENT_ID,
        deployment_revision=0 if mode is ScorerDeploymentMode.DISABLED else 2,
        active_pointer_version=(0 if mode is ScorerDeploymentMode.DISABLED else 4),
        health=(
            ScorerDeploymentHealth.DISABLED
            if mode is ScorerDeploymentMode.DISABLED
            else ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED
        ),
    )


def _deployment_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "id": EVENT_ID,
        "mode": "SHADOW",
        "revision": 2,
        "registry_id": REGISTRY_ID,
        "registry_revision": 3,
        "active_pointer_version": 4,
        "scorer_artifact_id": ARTIFACT_ID,
        "scorer_artifact_hash": ARTIFACT_HASH,
        "evaluation_report_id": REPORT_ID,
        "evaluation_report_hash": REPORT_HASH,
        "evaluation_status": "ACCEPTED_SHADOW_ONLY",
        "proof_id": PROOF_ID,
        "proof_hash": PROOF_HASH,
        "candidate_completeness_verified": False,
        "report_scope_hash": SCOPE_HASH,
        "proof_scope_hash": SCOPE_HASH,
        "policy_id": "approved-policy-v1",
        "policy_hash": POLICY_HASH,
        "current_rights_receipt_hash": RIGHTS_HASH,
    }
    event.update(overrides)
    return event


def _eligible_rights(**overrides: object) -> SimpleNamespace:
    rights: dict[str, object] = {
        "shadow_eligible": True,
        "paper_gate_eligible": False,
        "artifact_hash": ARTIFACT_HASH,
        "report_hash": REPORT_HASH,
        "proof_hash": PROOF_HASH,
        "policy_hash": POLICY_HASH,
        "evaluation_status": "ACCEPTED_SHADOW_ONLY",
        "candidate_completeness_verified": False,
        "fingerprint": RIGHTS_HASH,
    }
    rights.update(overrides)
    return SimpleNamespace(**rights)


async def test_disabled_deployment_is_inert_and_does_not_call_rights_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeploymentRepository:
        def __init__(self, _session) -> None:
            pass

        async def load_admin_scoring_deployment(self, _workspace_id):
            return _snapshot(ScorerDeploymentMode.DISABLED)

    class ForbiddenRightsGateway:
        def __init__(self, _session) -> None:
            raise AssertionError("DISABLED must not resolve report or rights")

    monkeypatch.setattr(module, "LearningScoringDeploymentRepository", DeploymentRepository)
    monkeypatch.setattr(module, "LearningScorerCurrentRightsRepository", ForbiddenRightsGateway)

    result = await PostgresScorerRuntimeCommitter()(
        SimpleNamespace(),
        SimpleNamespace(workspace_id=WORKSPACE),
        SimpleNamespace(snapshot=None, vector=None),
        SimpleNamespace(),
    )

    assert result is None


async def test_active_deployment_rechecks_current_rights_then_fails_closed_without_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeploymentRepository:
        def __init__(self, _session) -> None:
            pass

        async def load_admin_scoring_deployment(self, _workspace_id):
            return _snapshot(ScorerDeploymentMode.SHADOW)

    class Session:
        async def execute(self, _statement):
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(one_or_none=lambda: _deployment_event())
            )

    rights_calls: list[tuple[UUID, UUID, UUID]] = []

    class RightsRepository:
        def __init__(self, _session) -> None:
            pass

        async def verify_deployment_rights(
            self, *, workspace_id, scorer_artifact_id, evaluation_report_id
        ):
            rights_calls.append((workspace_id, scorer_artifact_id, evaluation_report_id))
            return _eligible_rights()

    monkeypatch.setattr(module, "LearningScoringDeploymentRepository", DeploymentRepository)
    monkeypatch.setattr(module, "LearningScorerCurrentRightsRepository", RightsRepository)

    with pytest.raises(ScorerDeploymentRightsUnavailable, match="artifact/policy resolver"):
        await PostgresScorerRuntimeCommitter()(
            Session(),
            SimpleNamespace(workspace_id=WORKSPACE),
            SimpleNamespace(snapshot=object(), vector=object()),
            SimpleNamespace(),
        )

    assert rights_calls == [(WORKSPACE, ARTIFACT_ID, REPORT_ID)]


async def test_active_deployment_with_inconclusive_rights_never_reaches_scorer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeploymentRepository:
        def __init__(self, _session) -> None:
            pass

        async def load_admin_scoring_deployment(self, _workspace_id):
            return _snapshot(ScorerDeploymentMode.PAPER_GATE)

    class Session:
        async def execute(self, _statement):
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    one_or_none=lambda: _deployment_event(
                        mode="PAPER_GATE", active_pointer_version=4
                    )
                )
            )

    class RightsRepository:
        def __init__(self, _session) -> None:
            pass

        async def verify_deployment_rights(self, **_kwargs):
            return SimpleNamespace(shadow_eligible=False, paper_gate_eligible=False)

    monkeypatch.setattr(module, "LearningScoringDeploymentRepository", DeploymentRepository)
    monkeypatch.setattr(module, "LearningScorerCurrentRightsRepository", RightsRepository)

    with pytest.raises(ScorerDeploymentRightsUnavailable, match="không cho phép mode"):
        await PostgresScorerRuntimeCommitter()(
            Session(),
            SimpleNamespace(workspace_id=WORKSPACE),
            SimpleNamespace(snapshot=object(), vector=object()),
            SimpleNamespace(),
        )


async def test_active_deployment_event_query_selects_only_runtime_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeploymentRepository:
        def __init__(self, _session) -> None:
            pass

        async def load_admin_scoring_deployment(self, _workspace_id):
            return _snapshot(ScorerDeploymentMode.SHADOW)

    class MappingResult:
        def mappings(self):
            return self

        def one_or_none(self):
            return {**_deployment_event()}

    class Session:
        async def execute(self, statement):
            assert {item["name"] for item in statement.column_descriptions} == {
                "workspace_id",
                "id",
                "mode",
                "revision",
                "active_pointer_version",
                "scorer_artifact_id",
                "scorer_artifact_hash",
                "evaluation_report_id",
                "evaluation_report_hash",
                "evaluation_status",
                "proof_hash",
                "candidate_completeness_verified",
                "report_scope_hash",
                "proof_scope_hash",
                "policy_hash",
            }
            return MappingResult()

    class RightsRepository:
        def __init__(self, _session) -> None:
            pass

        async def verify_deployment_rights(self, **_kwargs):
            return _eligible_rights()

    monkeypatch.setattr(module, "LearningScoringDeploymentRepository", DeploymentRepository)
    monkeypatch.setattr(module, "LearningScorerCurrentRightsRepository", RightsRepository)

    with pytest.raises(ScorerDeploymentRightsUnavailable, match="artifact/policy resolver"):
        await PostgresScorerRuntimeCommitter()(
            Session(),
            SimpleNamespace(workspace_id=WORKSPACE),
            SimpleNamespace(snapshot=object(), vector=object()),
            SimpleNamespace(),
        )


async def test_runtime_rejects_deployment_after_registry_pointer_version_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeploymentRepository:
        def __init__(self, _session) -> None:
            pass

        async def load_admin_scoring_deployment(self, _workspace_id):
            return _snapshot(ScorerDeploymentMode.SHADOW)

    class Session:
        async def execute(self, _statement):
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    one_or_none=lambda: _deployment_event(active_pointer_version=3)
                )
            )

    class ForbiddenRightsRepository:
        def __init__(self, _session) -> None:
            raise AssertionError("Stale registry pointer must be rejected before rights lookup")

    monkeypatch.setattr(module, "LearningScoringDeploymentRepository", DeploymentRepository)
    monkeypatch.setattr(module, "LearningScorerCurrentRightsRepository", ForbiddenRightsRepository)

    with pytest.raises(ScorerDeploymentRightsUnavailable, match="pointer"):
        await PostgresScorerRuntimeCommitter()(
            Session(),
            SimpleNamespace(workspace_id=WORKSPACE),
            SimpleNamespace(snapshot=object(), vector=object()),
            SimpleNamespace(),
        )


async def test_runtime_rejects_current_rights_hashes_that_differ_from_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeploymentRepository:
        def __init__(self, _session) -> None:
            pass

        async def load_admin_scoring_deployment(self, _workspace_id):
            return _snapshot(ScorerDeploymentMode.SHADOW)

    class Session:
        async def execute(self, _statement):
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(one_or_none=lambda: _deployment_event())
            )

    class RightsRepository:
        def __init__(self, _session) -> None:
            pass

        async def verify_deployment_rights(self, **_kwargs):
            return _eligible_rights(artifact_hash="9" * 64)

    monkeypatch.setattr(module, "LearningScoringDeploymentRepository", DeploymentRepository)
    monkeypatch.setattr(module, "LearningScorerCurrentRightsRepository", RightsRepository)

    with pytest.raises(ScorerDeploymentRightsUnavailable, match="Hash"):
        await PostgresScorerRuntimeCommitter()(
            Session(),
            SimpleNamespace(workspace_id=WORKSPACE),
            SimpleNamespace(snapshot=object(), vector=object()),
            SimpleNamespace(),
        )
