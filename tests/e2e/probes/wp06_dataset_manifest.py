"""Probe PostgreSQL WP-06: lưu v1/v2 bất biến, RLS và quyền chỉ đọc."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    DatasetManifestRecord,
    DatasetManifestSourceRefRecord,
    Workspace,
)
from trading_bot.adapters.database.repositories.dataset_manifest import DatasetManifestRepository
from trading_bot.application.learning_fold_plan import build_manifest_fold_plan
from trading_bot.domain.learning.dataset import (
    DatasetManifest,
    DatasetSample,
    DatasetSourceReference,
    DatasetSplit,
)
from trading_bot.domain.learning.evaluation import TemporalWindow, WalkForwardSplit
from trading_bot.domain.research.source_policy import SourceUsePurpose

STATE = json.loads(os.environ.get("WP06_STATE", "{}"))
PHASE = os.environ.get("WP06_PHASE", "")
ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


def _engine(role: str | None = None):
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    url = URL.create(
        "postgresql+psycopg",
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )
    options = {"options": f"-c role={role}"} if role else {}
    return create_async_engine(url, connect_args=options)


def _manifest(workspace_id: UUID, *, variant: str = "a") -> DatasetManifest:
    return DatasetManifest(
        workspace_id=workspace_id,
        query_hash="1" * 64,
        symbols=("BTCUSDT",),
        data_start=NOW - timedelta(days=1),
        data_end=NOW + timedelta(days=1),
        as_of=NOW + timedelta(minutes=1),
        feature_schema_version="feature-schema-v1",
        feature_version="feature-v1",
        label_version="label-v1",
        sample_schema_version="sample-schema-v1",
        split_policy_version="split-v1",
        samples=(
            DatasetSample(
                sample_id=f"sample-{variant}",
                decision_at=NOW,
                feature_available_at=NOW - timedelta(minutes=1),
                outcome_start=NOW,
                outcome_end=NOW + timedelta(seconds=1),
                feature_snapshot_hash="2" * 64,
                outcome_id=f"trade-{variant}",
                outcome_hash="3" * 64,
                label_hash="4" * 64,
                label_value=1,
                label_version="label-v1",
                split=DatasetSplit.TRAIN,
                source_dependent=False,
                source_rights_hashes=(),
                source_revision_refs=(),
            ),
        ),
        exclusions=(),
        holdout_sealed_at=None,
        retention_policy_ref="retention-v1",
    )


def _manifest_v2(workspace_id: UUID, *, variant: str = "a") -> DatasetManifest:
    original = _manifest(workspace_id, variant=variant)
    prototype = original.samples[0]
    return replace(
        original,
        manifest_version="dataset-manifest-v2",
        data_start=NOW,
        data_end=NOW + timedelta(hours=16),
        as_of=NOW + timedelta(hours=16),
        samples=tuple(
            replace(
                prototype,
                sample_id=f"{name}-{variant}",
                decision_at=NOW + timedelta(hours=decision),
                feature_available_at=NOW,
                outcome_start=NOW + timedelta(hours=decision),
                outcome_end=NOW + timedelta(hours=end),
                split=DatasetSplit.HOLDOUT if name == "sealed" else DatasetSplit.DEVELOPMENT,
            )
            for name, decision, end in (
                ("train", 1, 2),
                ("train-overlap", 2, 5),
                ("validation", 4, 5),
                ("test", 6, 7),
                ("validation-next", 9, 10),
                ("test-next", 11, 12),
                ("sealed", 14, 15),
            )
        ),
        holdout_sealed_at=NOW + timedelta(hours=16),
    )


def _manifest_v3(workspace_id: UUID) -> DatasetManifest:
    original = _manifest(workspace_id, variant="typed")
    prototype = original.samples[0]
    return replace(
        original,
        manifest_version="dataset-manifest-v3",
        samples=(
            replace(
                prototype,
                source_dependent=True,
                source_rights_hashes=(),
                source_revision_refs=(),
                source_references=(
                    DatasetSourceReference(
                        source_id="official-calendar",
                        item_id="release-2026-11",
                        revision_id="revision-1",
                        policy_id="official-calendar-policy-v2",
                        policy_sequence=2,
                        purpose=SourceUsePurpose.TRAINING,
                        rights_hash="5" * 64,
                    ),
                ),
            ),
        ),
    )


def _fold_plan(manifest: DatasetManifest):
    def window(start: int, end: int) -> TemporalWindow:
        return TemporalWindow(NOW + timedelta(hours=start), NOW + timedelta(hours=end))

    return build_manifest_fold_plan(
        manifest,
        folds=(
            WalkForwardSplit(window(0, 3), window(3, 5), window(5, 8)),
            WalkForwardSplit(window(0, 8), window(8, 10), window(10, 13)),
        ),
        expected_split_policy_version="split-v1",
        evaluation_cutoff=NOW + timedelta(hours=13),
    )


def _assert_holdout_interval_protected(manifest: DatasetManifest) -> None:
    prototype = manifest.samples[0]
    candidate = replace(
        manifest,
        samples=tuple(
            replace(sample, outcome_end=NOW + timedelta(hours=9))
            if sample.sample_id == "test-a"
            else sample
            for sample in manifest.samples
        )
        + (
            replace(
                prototype,
                sample_id="holdout-edge-a",
                decision_at=NOW + timedelta(hours=8),
                feature_available_at=NOW,
                outcome_start=NOW + timedelta(hours=8),
                outcome_end=NOW + timedelta(hours=10),
                split=DatasetSplit.HOLDOUT,
            ),
        ),
    )
    first_fold = _fold_plan(manifest).folds[0].split
    try:
        build_manifest_fold_plan(
            candidate,
            folds=(first_fold,),
            expected_split_policy_version="split-v1",
            evaluation_cutoff=NOW + timedelta(hours=13),
        )
    except ValueError as error:
        message = str(error).lower()
        if "holdout" not in message or "interval" not in message:
            raise AssertionError("Kế hoạch bị từ chối vì lý do khác holdout interval.") from error
        return
    raise AssertionError("Fold đã nhận sample có outcome interval giao holdout.")


async def _seed() -> dict[str, object]:
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=UUID(STATE["workspace_id"]),
                    name="WP06 manifest workspace",
                    slug=f"wp06-{STATE['workspace_id'][:8]}",
                ),
                Workspace(
                    id=UUID(STATE["other_workspace_id"]),
                    name="WP06 other workspace",
                    slug=f"wp06-{STATE['other_workspace_id'][:8]}",
                ),
            ]
        )
    await engine.dispose()
    return {"seeded": True}


async def _seed_downgrade_guard() -> dict[str, object]:
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    manifest = _manifest(UUID(STATE["other_workspace_id"]))
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(manifest.workspace_id)},
        )
        session.add(DatasetManifestRecord.from_domain(manifest))
        await session.flush()
    await engine.dispose()
    return {"inserted": True}


async def _persist() -> dict[str, object]:
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    other_workspace_id = UUID(STATE["other_workspace_id"])
    manifests = (_manifest(workspace_id), _manifest_v2(workspace_id))
    async with sessions() as session, session.begin():
        repository = DatasetManifestRepository(session)
        for manifest in manifests:
            first = await repository.append(workspace_id=workspace_id, manifest=manifest)
            duplicate = await repository.append(workspace_id=workspace_id, manifest=manifest)
            assert not first.is_duplicate
            assert duplicate.is_duplicate
    await engine.dispose()

    engine = _engine("crypidian_candidate_builder")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        repository = DatasetManifestRepository(session)
        for manifest in manifests:
            loaded = await repository.get_by_hash(
                workspace_id=workspace_id,
                manifest_hash=manifest.content_hash,
            )
            hidden = await repository.get_by_hash(
                workspace_id=other_workspace_id,
                manifest_hash=manifest.content_hash,
            )
            assert loaded == manifest
            assert loaded.canonical_json == manifest.canonical_json
            assert loaded.content_hash == manifest.content_hash
            assert hidden is None
            if loaded.manifest_version == "dataset-manifest-v2":
                plan = _fold_plan(loaded)
                _assert_holdout_interval_protected(loaded)
                assert plan.content_hash == _fold_plan(manifest).content_hash
                assert plan.folds[0].train_sample_ids == ("train-a",)
                assert plan.folds[0].validation_sample_ids == ("validation-a",)
                assert plan.folds[0].test_sample_ids == ("test-a",)
                assert plan.folds[0].exclusions[0].reasons == (
                    "label_incomplete_at_cutoff",
                    "protected_label_overlap",
                )
                assert plan.folds[1].train_sample_ids == (
                    "test-a",
                    "train-a",
                    "train-overlap-a",
                    "validation-a",
                )
                assert plan.folds[1].validation_sample_ids == ("validation-next-a",)
                assert plan.folds[1].test_sample_ids == ("test-next-a",)
                assert plan.folds[1].exclusions == ()
    await engine.dispose()
    return {
        "versions": [manifest.manifest_version for manifest in manifests],
        "folds": 2,
        "holdout_interval_overlap_rejected": True,
    }


async def _persist_typed() -> dict[str, object]:
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    other_workspace_id = UUID(STATE["other_workspace_id"])
    manifest = _manifest_v3(workspace_id)
    reference = manifest.samples[0].source_references[0]
    rollback_manifest = replace(manifest, query_hash="d" * 64)
    async with sessions() as session:
        try:
            async with session.begin():
                await DatasetManifestRepository(session).append(
                    workspace_id=workspace_id,
                    manifest=rollback_manifest,
                )
                raise RuntimeError("WP06_ROLLBACK_FIXTURE")
        except RuntimeError as error:
            if str(error) != "WP06_ROLLBACK_FIXTURE":
                raise
        async with session.begin():
            assert (
                await session.scalar(
                    select(DatasetManifestRecord.id).where(
                        DatasetManifestRecord.workspace_id == workspace_id,
                        DatasetManifestRecord.manifest_hash == rollback_manifest.content_hash,
                    )
                )
                is None
            )
            ref_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_manifest_source_ref "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": workspace_id},
            )
            assert ref_count == 0
    async with sessions() as session, session.begin():
        repository = DatasetManifestRepository(session)
        first = await repository.append(workspace_id=workspace_id, manifest=manifest)
        duplicate = await repository.append(workspace_id=workspace_id, manifest=manifest)
        assert not first.is_duplicate
        assert duplicate.is_duplicate
        matches = await repository.find_source_reference_uses(
            workspace_id=workspace_id,
            reference=reference,
        )
        wrong_workspace = await repository.find_source_reference_uses(
            workspace_id=other_workspace_id,
            reference=reference,
        )
        rights_changed = await repository.find_source_reference_uses(
            workspace_id=workspace_id,
            reference=replace(reference, rights_hash="6" * 64),
        )
        assert len(matches) == 1
        assert matches[0].manifest_id == first.record.id
        assert matches[0].sample_id == "sample-typed"
        assert matches[0].rights_hash == "5" * 64
        assert wrong_workspace == ()
        assert rights_changed == matches
    await engine.dispose()

    engine = _engine("crypidian_candidate_builder")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        loaded = await DatasetManifestRepository(session).get_by_hash(
            workspace_id=workspace_id,
            manifest_hash=manifest.content_hash,
        )
        assert loaded == manifest
    await engine.dispose()

    return {
        "typed_ref_count": len(matches),
        "workspace_scope": True,
        "rights_hash_observed": True,
        "transaction_atomic": True,
    }


async def _assert_roles_and_immutability() -> dict[str, object]:
    workspace_id = UUID(STATE["workspace_id"])
    manifest = _manifest(workspace_id)
    errors: list[str] = []

    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        try:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                    {"workspace_id": str(workspace_id)},
                )
                await session.execute(
                    update(DatasetManifestRecord)
                    .where(DatasetManifestRecord.manifest_hash == manifest.content_hash)
                    .values(manifest_hash="b" * 64)
                )
        except DBAPIError as error:
            errors.append(str(error.orig))
    await engine.dispose()
    assert any("WP06_DATASET_MANIFEST_IMMUTABLE" in error for error in errors), repr(errors)

    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        try:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                    {"workspace_id": str(workspace_id)},
                )
                await session.execute(
                    update(DatasetManifestSourceRefRecord)
                    .where(DatasetManifestSourceRefRecord.workspace_id == workspace_id)
                    .values(rights_hash="7" * 64)
                )
        except DBAPIError as error:
            errors.append(str(error.orig))
    await engine.dispose()
    assert any("WP06_DATASET_MANIFEST_IMMUTABLE" in error for error in errors), repr(errors)

    typed_manifest = _manifest_v3(workspace_id)
    reference = typed_manifest.samples[0].source_references[0]
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        try:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                    {"workspace_id": str(workspace_id)},
                )
                manifest_record_id = await session.scalar(
                    select(DatasetManifestRecord.id).where(
                        DatasetManifestRecord.workspace_id == workspace_id,
                        DatasetManifestRecord.manifest_hash == typed_manifest.content_hash,
                    )
                )
                assert manifest_record_id is not None
                session.add(
                    DatasetManifestSourceRefRecord.from_reference(
                        workspace_id=workspace_id,
                        manifest_id=manifest_record_id,
                        sample_id=typed_manifest.samples[0].sample_id,
                        reference=replace(reference, rights_hash="8" * 64),
                    )
                )
                await session.flush()
        except DBAPIError as error:
            errors.append(str(error.orig))
    await engine.dispose()
    assert any("WP06_SOURCE_REF_NOT_IN_MANIFEST" in error for error in errors), repr(errors)

    engine = _engine("crypidian_candidate_builder")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        repository = DatasetManifestRepository(session)
        for expected in (manifest, _manifest_v2(workspace_id)):
            loaded = await repository.get_by_hash(
                workspace_id=workspace_id,
                manifest_hash=expected.content_hash,
            )
            assert loaded == expected
    await engine.dispose()

    engine = _engine(TRAINER)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    trainer_payload_denied = False
    try:
        async with sessions() as session, session.begin():
            await DatasetManifestRepository(session).get_by_hash(
                workspace_id=workspace_id,
                manifest_hash=manifest.content_hash,
            )
    except DBAPIError as error:
        trainer_payload_denied = "permission denied" in str(error.orig).lower()
    await engine.dispose()
    assert trainer_payload_denied

    engine = _engine(TRAINER)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        repository = DatasetManifestRepository(session)
        typed = _manifest_v3(workspace_id)
        uses = await repository.find_source_reference_uses(
            workspace_id=workspace_id,
            reference=typed.samples[0].source_references[0],
        )
        assert len(uses) == 1
        assert (
            await repository.find_source_reference_uses(
                workspace_id=UUID(STATE["other_workspace_id"]),
                reference=typed.samples[0].source_references[0],
            )
            == ()
        )
    await engine.dispose()

    engine = _engine(TRAINER)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        try:
            async with session.begin():
                await DatasetManifestRepository(session).append(
                    workspace_id=workspace_id,
                    manifest=_manifest_v2(workspace_id, variant="b"),
                )
        except DBAPIError as error:
            errors.append(str(error.orig))
    await engine.dispose()
    assert any("permission denied" in error.lower() for error in errors)
    return {
        "builder_payload_read": True,
        "trainer_payload_denied": True,
        "trainer_insert_denied": True,
        "append_only": True,
    }


async def _count_without_workspace_scope() -> dict[str, object]:
    engine = _engine(ADMIN)
    async with engine.connect() as connection:
        count = await connection.scalar(
            select(text("count(*)")).select_from(text("dataset_manifest"))
        )
        source_ref_count = await connection.scalar(
            select(text("count(*)")).select_from(text("dataset_manifest_source_ref"))
        )
    await engine.dispose()
    assert count == 0
    assert source_ref_count == 0
    return {"unscoped_rows": count, "unscoped_source_refs": source_ref_count}


async def _main() -> None:
    handlers = {
        "seed": _seed,
        "seed_downgrade_guard": _seed_downgrade_guard,
        "persist": _persist,
        "persist_typed": _persist_typed,
        "roles": _assert_roles_and_immutability,
        "unscoped": _count_without_workspace_scope,
    }
    if PHASE not in handlers:
        raise ValueError("WP06_PHASE không hợp lệ.")
    print(json.dumps(await handlers[PHASE](), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
