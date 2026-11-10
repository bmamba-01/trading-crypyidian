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


def _snapshot(mode: ScorerDeploymentMode) -> ScorerDeploymentAdminSnapshot:
    return ScorerDeploymentAdminSnapshot(
        workspace_id=WORKSPACE,
        mode=mode,
        deployment_event_id=None if mode is ScorerDeploymentMode.DISABLED else EVENT_ID,
        deployment_revision=0 if mode is ScorerDeploymentMode.DISABLED else 2,
        active_pointer_version=0,
        health=(
            ScorerDeploymentHealth.DISABLED
            if mode is ScorerDeploymentMode.DISABLED
            else ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED
        ),
    )


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
                mappings=lambda: SimpleNamespace(
                    one_or_none=lambda: {
                        "workspace_id": WORKSPACE,
                        "mode": "SHADOW",
                        "revision": 2,
                        "scorer_artifact_id": ARTIFACT_ID,
                        "evaluation_report_id": REPORT_ID,
                    }
                )
            )

    rights_calls: list[tuple[UUID, UUID, UUID]] = []

    class RightsRepository:
        def __init__(self, _session) -> None:
            pass

        async def verify_deployment_rights(
            self, *, workspace_id, scorer_artifact_id, evaluation_report_id
        ):
            rights_calls.append((workspace_id, scorer_artifact_id, evaluation_report_id))
            return SimpleNamespace(shadow_eligible=True, paper_gate_eligible=False)

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
                    one_or_none=lambda: {
                        "workspace_id": WORKSPACE,
                        "mode": "PAPER_GATE",
                        "revision": 2,
                        "scorer_artifact_id": ARTIFACT_ID,
                        "evaluation_report_id": REPORT_ID,
                    }
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
            return {
                "workspace_id": WORKSPACE,
                "id": EVENT_ID,
                "mode": "SHADOW",
                "revision": 2,
                "scorer_artifact_id": ARTIFACT_ID,
                "evaluation_report_id": REPORT_ID,
            }

    class Session:
        async def execute(self, statement):
            assert {item["name"] for item in statement.column_descriptions} == {
                "workspace_id",
                "id",
                "mode",
                "revision",
                "scorer_artifact_id",
                "evaluation_report_id",
            }
            return MappingResult()

    class RightsRepository:
        def __init__(self, _session) -> None:
            pass

        async def verify_deployment_rights(self, **_kwargs):
            return SimpleNamespace(shadow_eligible=True, paper_gate_eligible=False)

    monkeypatch.setattr(module, "LearningScoringDeploymentRepository", DeploymentRepository)
    monkeypatch.setattr(module, "LearningScorerCurrentRightsRepository", RightsRepository)

    with pytest.raises(ScorerDeploymentRightsUnavailable, match="artifact/policy resolver"):
        await PostgresScorerRuntimeCommitter()(
            Session(),
            SimpleNamespace(workspace_id=WORKSPACE),
            SimpleNamespace(snapshot=object(), vector=object()),
            SimpleNamespace(),
        )
