"""Probe ranh giới approval split bằng login PostgreSQL của từng service."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    LearningSplitApprovalRecord,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.adapters.database.repositories.dataset_manifest import (
    DatasetManifestConflict,
    DatasetManifestRepository,
)
from trading_bot.adapters.database.repositories.learning_split_approvals import (
    LearningSplitApprovalRepository,
)
from trading_bot.application.learning_split_approval import (
    LearningSplitApprovalService,
    SplitApprovalRequest,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample, DatasetSplit
from trading_bot.domain.learning.evaluation import TemporalWindow, WalkForwardSplit

PHASE = os.environ.get("WP06_APPROVAL_PHASE", "admin")
NOW = datetime.now(UTC).replace(microsecond=0)


def _engine():
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    url = URL.create(
        "postgresql+psycopg",
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )
    return create_async_engine(url)


def _at(hour: int) -> datetime:
    return NOW + timedelta(hours=hour)


def _manifest(workspace_id, *, variant: str = "a") -> DatasetManifest:
    samples = tuple(
        DatasetSample(
            sample_id=f"{name}-{variant}",
            decision_at=_at(decision),
            feature_available_at=_at(decision - 1),
            outcome_start=_at(decision),
            outcome_end=_at(decision) + timedelta(minutes=1),
            feature_snapshot_hash=feature_hash * 64,
            outcome_id=f"outcome-{name}-{variant}",
            outcome_hash=outcome_hash * 64,
            label_hash=label_hash * 64,
            label_value=1,
            label_version="label-v1",
            split=split,
            source_dependent=False,
            source_rights_hashes=(),
            source_revision_refs=(),
        )
        for name, decision, split, feature_hash, outcome_hash, label_hash in (
            ("train", 1, DatasetSplit.TRAIN, "1", "a", "b"),
            ("validation", 4, DatasetSplit.VALIDATION, "2", "c", "d"),
            ("test", 6, DatasetSplit.TEST, "3", "e", "f"),
        )
    )
    return DatasetManifest(
        workspace_id=workspace_id,
        query_hash="9" * 64 if variant == "a" else "8" * 64,
        symbols=("BTCUSDT",),
        data_start=_at(0),
        data_end=_at(8),
        as_of=_at(8),
        feature_schema_version="feature-schema-v1",
        feature_version="feature-v1",
        label_version="label-v1",
        sample_schema_version="sample-schema-v1",
        split_policy_version="split-v1",
        samples=samples,
        exclusions=(),
        holdout_sealed_at=None,
    )


def _folds():
    return (
        WalkForwardSplit(
            train=TemporalWindow(_at(0), _at(3)),
            validation=TemporalWindow(_at(3), _at(5)),
            test=TemporalWindow(_at(5), _at(8)),
        ),
    )


async def _admin() -> dict[str, object]:
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id, other_workspace_id, user_id, membership_id = (uuid4() for _ in range(4))
    async with sessions() as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=workspace_id,
                    name="WP06 split approval",
                    slug=f"wp06-approval-{workspace_id.hex[:12]}",
                ),
                Workspace(
                    id=other_workspace_id,
                    name="WP06 split approval other",
                    slug=f"wp06-other-{other_workspace_id.hex[:12]}",
                ),
                UserAccount(
                    id=user_id,
                    email_normalized=f"wp06-{user_id.hex}@example.invalid",
                    password_hash="fixture-only-not-a-credential",
                ),
            ]
        )
        await session.flush()
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        session.add(UserMembership(id=membership_id, workspace_id=workspace_id, user_id=user_id))

    identity = Principal(
        user_id=user_id,
        membership_id=membership_id,
        workspace_id=workspace_id,
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=NOW,
    )
    candidate = _manifest(workspace_id)
    request = SplitApprovalRequest(
        manifest=candidate,
        folds=_folds(),
        evaluation_cutoff=_at(8),
        reason="Duyệt kế hoạch chia thời gian đã được xem xét.",
        idempotency_key=uuid4(),
    )
    async with sessions() as session, session.begin():
        repository = LearningSplitApprovalRepository(session)
        service = LearningSplitApprovalService(repository).for_principal(identity)
        try:
            await service.approve(request)
        except DBAPIError as error:
            if "WP06_SPLIT_APPROVAL_CANDIDATE_MISMATCH" not in str(error):
                raise AssertionError("Approval thiếu candidate bị từ chối sai lý do.") from error
        else:
            raise AssertionError("Approval manifest do caller tự dựng đã được ghi.")

    async with sessions() as session, session.begin():
        try:
            await DatasetManifestRepository(session).append_for_training(
                workspace_id=workspace_id,
                manifest=candidate,
                folds=_folds(),
                evaluation_cutoff=_at(8),
            )
        except DatasetManifestConflict as error:
            if "thiếu approval" not in str(error):
                raise AssertionError("Seal không candidate bị từ chối sai lý do.") from error
        else:
            raise AssertionError("Training manifest thiếu candidate/approval vẫn được seal.")

    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(other_workspace_id)},
        )
        hidden = await session.scalar(
            select(LearningSplitApprovalRecord.id).where(
                LearningSplitApprovalRecord.manifest_hash == candidate.content_hash
            )
        )
        assert hidden is None
    async with sessions() as session, session.begin():
        count = await session.scalar(
            select(text("count(*)")).select_from(text("learning_split_approval"))
        )
        assert count == 0
    await engine.dispose()
    return {
        "approval_requires_staged_candidate": True,
        "training_seal_without_candidate_denied": True,
        "workspace_rls": True,
        "unscoped_read_denied": True,
    }


async def _trainer_denied() -> dict[str, object]:
    engine = _engine()
    denied = False
    async with engine.connect() as connection:
        try:
            await connection.execute(text("SELECT id FROM public.learning_split_approval LIMIT 1"))
        except DBAPIError as error:
            denied = "permission denied" in str(error.orig).lower()
    await engine.dispose()
    assert denied, "Trainer login đọc được bảng split approval."
    return {"trainer_select_denied": True}


async def main() -> None:
    if PHASE == "admin":
        result = await _admin()
    elif PHASE == "trainer":
        result = await _trainer_denied()
    else:
        raise ValueError("WP06_APPROVAL_PHASE không hợp lệ.")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
