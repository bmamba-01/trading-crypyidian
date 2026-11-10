from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.adapters.database.models.learning_entities import LearningSplitApprovalRecord
from trading_bot.application.learning_split_approval import (
    LearningSplitApprovalService,
    SplitApprovalRequest,
)
from trading_bot.auth.policy import Permission, Principal, Role, authorize
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample, DatasetSplit
from trading_bot.domain.learning.evaluation import TemporalWindow, WalkForwardSplit

BASE = datetime(2026, 1, 1, tzinfo=UTC)
WORKSPACE = uuid4()


def at(hour: int) -> datetime:
    return BASE + timedelta(hours=hour)


def manifest() -> DatasetManifest:
    samples = tuple(
        DatasetSample(
            sample_id=name,
            decision_at=at(decision),
            feature_available_at=at(decision - 1),
            outcome_start=at(decision),
            outcome_end=at(decision + 1),
            feature_snapshot_hash=char * 64,
            outcome_id=f"outcome-{name}",
            outcome_hash=hash_char * 64,
            label_hash=label_char * 64,
            label_value=1,
            label_version="labels-v1",
            split=split,
            source_dependent=False,
            source_rights_hashes=(),
            source_revision_refs=(),
        )
        for name, decision, split, char, hash_char, label_char in (
            ("train", 1, DatasetSplit.TRAIN, "1", "a", "b"),
            ("validation", 4, DatasetSplit.VALIDATION, "2", "c", "d"),
            ("test", 6, DatasetSplit.TEST, "3", "e", "f"),
        )
    )
    return DatasetManifest(
        workspace_id=WORKSPACE,
        query_hash="9" * 64,
        symbols=("BTCUSDT",),
        data_start=at(0),
        data_end=at(8),
        as_of=at(8),
        feature_schema_version="features-v1",
        feature_version="features-v1",
        label_version="labels-v1",
        sample_schema_version="samples-v1",
        split_policy_version="split-v1",
        samples=samples,
        exclusions=(),
        holdout_sealed_at=None,
    )


def folds() -> tuple[WalkForwardSplit, ...]:
    return (
        WalkForwardSplit(
            train=TemporalWindow(at(0), at(3)),
            validation=TemporalWindow(at(3), at(5)),
            test=TemporalWindow(at(5), at(8)),
        ),
    )


def principal(
    role: Role = Role.ANALYST_REVIEWER, *, verified_at: datetime | None = None
) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=WORKSPACE,
        roles=frozenset({role}),
        mfa_verified_at=verified_at or datetime.now(UTC),
    )


@dataclass
class ApprovalRepository:
    approval: object | None = None
    calls: int = 0

    async def append_preseal(self, *, approval: object) -> object:
        self.calls += 1
        self.approval = approval
        return approval


@pytest.mark.asyncio
async def test_split_approval_is_mfa_gated_and_binds_manifest_policy_and_fold_hash() -> None:
    repository = ApprovalRepository()
    service = LearningSplitApprovalService(repository)
    identity = principal()

    result = await service.for_principal(identity).approve(
        SplitApprovalRequest(
            manifest=manifest(),
            folds=folds(),
            evaluation_cutoff=at(8),
            reason="Chấp thuận kiểm tra cuốn chiếu cho manifest đã xác minh.",
            idempotency_key=uuid4(),
        )
    )

    assert authorize(identity, Permission.LEARNING_SPLIT_APPROVE)
    assert repository.calls == 1
    assert result.workspace_id == identity.workspace_id
    assert result.manifest_hash == manifest().content_hash
    assert result.split_policy_version == "split-v1"
    assert len(result.fold_plan_hash) == 64
    assert result.actor_membership_id == identity.membership_id
    assert result.reason == "Chấp thuận kiểm tra cuốn chiếu cho manifest đã xác minh."
    stored = LearningSplitApprovalRecord.from_domain(result)
    assert stored.to_domain() == result
    stored.fold_plan_hash = "0" * 64
    with pytest.raises(ValueError, match="không khớp"):
        stored.to_domain()


@pytest.mark.asyncio
async def test_split_approval_rejects_stale_mfa_and_roles_without_approval_permission() -> None:
    repository = ApprovalRepository()
    service = LearningSplitApprovalService(repository)
    request = SplitApprovalRequest(
        manifest=manifest(),
        folds=folds(),
        evaluation_cutoff=at(8),
        reason="Chấp thuận kiểm tra cuốn chiếu cho manifest đã xác minh.",
        idempotency_key=uuid4(),
    )

    with pytest.raises(PermissionError, match="MFA gần đây"):
        await service.for_principal(
            principal(verified_at=datetime.now(UTC) - timedelta(hours=1))
        ).approve(request)
    with pytest.raises(PermissionError, match="quyền phê duyệt"):
        await service.for_principal(principal(Role.OPERATOR)).approve(request)
    assert repository.calls == 0


@pytest.mark.asyncio
async def test_split_approval_rejects_invalid_or_unsafe_fold_plan_before_persist() -> None:
    repository = ApprovalRepository()
    service = LearningSplitApprovalService(repository)
    invalid_fold = WalkForwardSplit(
        train=TemporalWindow(at(3), at(4)),
        validation=TemporalWindow(at(2), at(5)),
        test=TemporalWindow(at(5), at(8)),
    )
    request = SplitApprovalRequest(
        manifest=manifest(),
        folds=(invalid_fold,),
        evaluation_cutoff=at(8),
        reason="Chấp thuận kiểm tra cuốn chiếu cho manifest đã xác minh.",
        idempotency_key=uuid4(),
    )

    with pytest.raises(ValueError, match="thứ tự"):
        await service.for_principal(principal()).approve(request)
    assert repository.calls == 0
