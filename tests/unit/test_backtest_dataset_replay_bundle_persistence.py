from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundleReference,
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.domain.market.models import OHLCVBar


def test_reference_hash_matches_bundle_hash_without_volatile_rights_timestamps() -> None:
    now = datetime(2026, 11, 3, 12, tzinfo=UTC)
    workspace_id = uuid4()
    request = RestBackfillRequest(
        symbol="BTCUSDT",
        interval="1h",
        start=now - timedelta(hours=2),
        end=now,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
    )
    bars = tuple(
        OHLCVBar(
            symbol="BTCUSDT",
            timeframe="1h",
            open_time=now - timedelta(hours=2 - index),
            close_time=now - timedelta(hours=1 - index) - timedelta(milliseconds=1),
            event_at=now - timedelta(hours=1 - index) - timedelta(milliseconds=1),
            received_at=now,
            available_at=now,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("1"),
            is_closed=True,
            source="BINANCE_USDM_REST_BACKFILL",
            source_cursor=f"1h:{index}",
            checksum=hashlib.sha256(f"1h:{index}".encode()).hexdigest(),
        )
        for index in range(2)
    )

    def make_input(checked_at: datetime) -> BacktestDatasetReplayInput:
        rights = BackfillRightsEvidence(
            workspace_id=workspace_id,
            provider="BINANCE_USDM_REST",
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
            terms_url="https://example.invalid/terms",
            policy_fingerprint=hashlib.sha256(b"policy").hexdigest(),
            verified_at=now - timedelta(days=1),
            checked_at=checked_at,
            valid_until=checked_at + timedelta(minutes=5),
        )
        manifest = build_backtest_dataset_manifest(
            request,
            evaluation_start=now - timedelta(hours=1),
            warmup_bars=1,
            bars=bars,
            rights_evidence=rights,
            prepared_at=now,
        )
        return BacktestDatasetReplayInput(manifest, bars, rights)

    original = make_input(now)
    first = build_backtest_dataset_replay_bundle((original,))
    refreshed_rights = replace(
        original.current_rights,
        checked_at=now + timedelta(minutes=1),
        valid_until=now + timedelta(minutes=6),
    )
    refreshed = replace(original, current_rights=refreshed_rights)
    later = build_backtest_dataset_replay_bundle((refreshed,))

    first_reference = BacktestDatasetReplayBundleReference.from_bundle(first)
    later_reference = BacktestDatasetReplayBundleReference.from_bundle(later)
    assert first_reference.bundle_hash == first.dataset_hash
    assert later_reference.bundle_hash == later.dataset_hash
    assert first_reference.canonical_json == later_reference.canonical_json


def _records():
    from trading_bot.adapters.database import models

    assert hasattr(models, "BacktestDatasetReplayBundleRecord"), "Thiếu bản ghi bundle"
    assert hasattr(models, "BacktestRunDatasetRightsRecord"), "Thiếu bản ghi quyền theo run"
    return models.BacktestDatasetReplayBundleRecord, models.BacktestRunDatasetRightsRecord


def _bundle():
    from test_backtest_dataset_replay_bundle import replay_input

    return build_backtest_dataset_replay_bundle((replay_input("1h"), replay_input("15m")))


def test_bundle_and_receipt_metadata_bind_workspace_unique_keys_and_run_fk() -> None:
    from sqlalchemy import UniqueConstraint

    bundle_record, rights_record = _records()
    bundle_table = bundle_record.__table__
    receipt_table = rights_record.__table__
    assert bundle_table.name == "backtest_dataset_replay_bundle"
    assert receipt_table.name == "backtest_run_dataset_rights"
    assert any(
        tuple(constraint.columns.keys()) == ("workspace_id", "bundle_hash")
        for constraint in bundle_table.constraints
        if isinstance(constraint, UniqueConstraint)
    )
    assert any(
        tuple(constraint.columns.keys()) == ("workspace_id", "run_id", "child_dataset_hash")
        for constraint in receipt_table.constraints
        if isinstance(constraint, UniqueConstraint)
    )
    assert any(
        tuple(constraint.columns.keys()) == ("workspace_id", "run_id")
        and tuple(element.target_fullname for element in constraint.elements)
        == ("backtest_run.workspace_id", "backtest_run.id")
        for constraint in receipt_table.foreign_key_constraints
    )
    assert {"bundle_json", "canonical_bundle", "bundle_hash", "schema_version"} <= set(
        bundle_table.c.keys()
    )
    assert {"evidence_json", "canonical_evidence", "evidence_hash", "checked_at"} <= set(
        receipt_table.c.keys()
    )
    assert not receipt_table.c.checked_at.nullable


def test_bundle_record_round_trip_checks_canonical_json_and_indexed_scope() -> None:
    import json

    bundle_record, _ = _records()
    bundle = _bundle()
    reference = BacktestDatasetReplayBundleReference.from_bundle(bundle)
    record = bundle_record.from_reference(workspace_id=bundle.workspace_id, reference=reference)
    assert record.to_reference() == reference
    assert record.bundle_json == json.loads(reference.canonical_json)
    assert record.canonical_bundle == reference.canonical_json


def test_bundle_record_rejects_cross_workspace_factory() -> None:
    bundle_record, _ = _records()
    reference = BacktestDatasetReplayBundleReference.from_bundle(_bundle())
    with pytest.raises(ValueError, match="workspace|Workspace"):
        bundle_record.from_reference(workspace_id=uuid4(), reference=reference)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("bundle_hash", "0" * 64),
        ("canonical_bundle", "{}"),
        ("bundle_json", {"schema_version": 1}),
        ("workspace_id", uuid4()),
        ("schema_version", 2),
        ("symbol", "ETHUSDT"),
        ("evaluation_start", datetime(2026, 1, 1, tzinfo=UTC)),
        ("range_end", datetime(2027, 1, 1, tzinfo=UTC)),
        ("rights_provider", "OTHER_PROVIDER"),
        ("rights_purpose", "other_purpose"),
        ("policy_fingerprint", "0" * 64),
    ],
)
def test_bundle_record_fails_closed_on_tampering(field: str, replacement: object) -> None:
    bundle_record, _ = _records()
    reference = BacktestDatasetReplayBundleReference.from_bundle(_bundle())
    record = bundle_record.from_reference(workspace_id=reference.workspace_id, reference=reference)
    setattr(record, field, replacement)
    with pytest.raises(ValueError, match="khớp|hợp lệ"):
        record.to_reference()


def test_receipt_factory_binds_exact_current_rights_and_all_scope_fields() -> None:
    import json

    _, rights_record = _records()
    bundle = _bundle()
    component = bundle.components[0]
    run_id = uuid4()
    record = rights_record.from_bundle_component(
        workspace_id=bundle.workspace_id,
        run_id=run_id,
        bundle_hash=bundle.dataset_hash,
        component=component,
    )
    rights = component.current_rights
    expected = {
        "schema_version": 1,
        "workspace_id": str(bundle.workspace_id),
        "run_id": str(run_id),
        "bundle_hash": bundle.dataset_hash,
        "child_dataset_hash": component.manifest.dataset_hash,
        "interval": "15m",
        "provider": "BINANCE_USDM_REST",
        "purpose": "BACKTEST_RESEARCH",
        "terms_url": "https://example.invalid/terms",
        "policy_fingerprint": rights.policy_fingerprint,
        "verified_at": "2026-11-02T12:00:00+00:00",
        "checked_at": "2026-11-03T12:00:00+00:00",
        "valid_until": "2026-11-03T12:05:00+00:00",
    }
    canonical = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert record.evidence_json == expected
    assert record.canonical_evidence == canonical
    assert record.evidence_hash == hashlib.sha256(canonical.encode()).hexdigest()
    for field in (
        "workspace_id",
        "run_id",
        "bundle_hash",
        "child_dataset_hash",
        "interval",
        "provider",
        "purpose",
        "terms_url",
        "policy_fingerprint",
    ):
        assert str(getattr(record, field)) == expected[field]
    assert record.verified_at == rights.verified_at
    assert record.checked_at == rights.checked_at
    assert record.valid_until == rights.valid_until
    duplicate = rights_record.from_bundle_component(
        workspace_id=bundle.workspace_id,
        run_id=run_id,
        bundle_hash=bundle.dataset_hash,
        component=component,
    )
    assert duplicate.canonical_evidence == record.canonical_evidence
    assert duplicate.evidence_hash == record.evidence_hash
    refreshed = replace(
        component,
        current_rights=replace(rights, checked_at=rights.checked_at + timedelta(seconds=1)),
    )
    changed = rights_record.from_bundle_component(
        workspace_id=bundle.workspace_id,
        run_id=run_id,
        bundle_hash=bundle.dataset_hash,
        component=refreshed,
    )
    assert changed.evidence_hash != record.evidence_hash


@pytest.mark.parametrize("invalid", ["workspace", "policy", "unchecked", "hash", "run"])
def test_receipt_factory_rejects_invalid_rights_or_scope(invalid: str) -> None:
    _, rights_record = _records()
    bundle = _bundle()
    component = bundle.components[0]
    workspace_id = bundle.workspace_id
    run_id = uuid4()
    bundle_hash = bundle.dataset_hash
    if invalid == "workspace":
        workspace_id = uuid4()
    elif invalid == "policy":
        component = replace(
            component, current_rights=replace(component.current_rights, policy_fingerprint="0" * 64)
        )
    elif invalid == "unchecked":
        component = replace(
            component, current_rights=replace(component.current_rights, checked_at=None)
        )
    elif invalid == "hash":
        bundle_hash = "invalid"
    else:
        run_id = "invalid"
    with pytest.raises(ValueError, match="quyền|workspace|hash|run_id|checked_at"):
        rights_record.from_bundle_component(
            workspace_id=workspace_id,
            run_id=run_id,
            bundle_hash=bundle_hash,
            component=component,
        )


def _migration_sql(direction: str) -> str:
    import importlib.util
    from io import StringIO
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (
        Path(__file__).parents[2]
        / "migrations/versions/e7c9a1b3d5f2_wp07c_persisted_replay_bundle.py"
    )
    assert path.is_file(), "Thiếu migration bundle và biên nhận quyền"
    spec = importlib.util.spec_from_file_location("wp07c_bundle_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "e7c9a1b3d5f2"
    assert module.down_revision == "c9d7e5f3a1b2"
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        getattr(module, direction)()
    return output.getvalue()


def test_migration_enforces_workspace_rls_append_only_and_minimum_role_grants() -> None:
    sql = _migration_sql("upgrade")
    tables = ("backtest_dataset_replay_bundle", "backtest_run_dataset_rights")
    for table in tables:
        assert f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY' in sql
        assert f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY' in sql
        assert f'BEFORE UPDATE OR DELETE ON public."{table}"' in sql
    assert sql.count("current_setting('app.workspace_id', true)") == 4
    grants = [line for line in sql.splitlines() if line.startswith("GRANT")]
    assert set(grants) == {
        "GRANT SELECT, INSERT ON TABLE public.backtest_dataset_replay_bundle "
        "TO crypidian_admin_web;",
        "GRANT SELECT, INSERT ON TABLE public.backtest_run_dataset_rights "
        "TO crypidian_trainer_worker;",
        "GRANT SELECT ON TABLE public.backtest_run_dataset_rights TO crypidian_admin_web;",
    }
    assert "FOREIGN KEY(workspace_id, run_id) REFERENCES backtest_run (workspace_id, id)" in sql
    assert "UNIQUE (workspace_id, bundle_hash)" in sql
    assert "UNIQUE (workspace_id, run_id, child_dataset_hash)" in sql


def test_downgrade_guards_both_tables_before_any_drop() -> None:
    sql = _migration_sql("downgrade")
    guard_end = sql.index("END $$")
    assert "SELECT 1 FROM public.backtest_dataset_replay_bundle" in sql[:guard_end]
    assert "SELECT 1 FROM public.backtest_run_dataset_rights" in sql[:guard_end]
    assert "RAISE EXCEPTION" in sql[:guard_end]
    assert guard_end < sql.index("DROP")


def test_bundle_record_rejects_jsonb_boolean_instead_of_schema_integer() -> None:
    bundle_record, _ = _records()
    reference = BacktestDatasetReplayBundleReference.from_bundle(_bundle())
    record = bundle_record.from_reference(workspace_id=reference.workspace_id, reference=reference)
    record.bundle_json["schema_version"] = True
    with pytest.raises(ValueError, match="JSONB"):
        record.to_reference()


def _repository_type():
    import importlib.util

    module_name = "trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles"
    assert importlib.util.find_spec(module_name) is not None, "Thiếu bundle repository"
    from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
        BacktestDatasetReplayBundleConflict,
        BacktestDatasetReplayBundleRepository,
    )

    return BacktestDatasetReplayBundleRepository, BacktestDatasetReplayBundleConflict


def _loader_type():
    import importlib.util

    module_name = "trading_bot.application.backtest_dataset_replay_bundle_loader"
    assert importlib.util.find_spec(module_name) is not None, "Thiếu bundle loader"
    from trading_bot.application.backtest_dataset_replay_bundle_loader import (
        BacktestDatasetReplayBundleLoader,
    )

    return BacktestDatasetReplayBundleLoader


@pytest.mark.asyncio
async def test_bundle_repository_append_readback_and_identical_retry():
    from test_backtest_dataset_manifest_persistence import FakeSession

    repository_type, _ = _repository_type()
    bundle = _bundle()
    session = FakeSession()
    result = await repository_type(session).append(workspace_id=bundle.workspace_id, bundle=bundle)
    assert not result.is_duplicate
    assert session.added == [result.record]
    assert result.record.to_reference() == BacktestDatasetReplayBundleReference.from_bundle(bundle)
    assert "app.workspace_id" in session.statements[0]
    assert "pg_advisory_xact_lock" in session.statements[1]
    assert "backtest_dataset_replay_bundle.workspace_id" in session.statements[2]
    retry_session = FakeSession([result.record])
    duplicate = await repository_type(retry_session).append(
        workspace_id=bundle.workspace_id, bundle=bundle
    )
    assert duplicate.is_duplicate
    assert duplicate.record is result.record
    assert not retry_session.added
    read_session = FakeSession([result.record])
    restored = await repository_type(read_session).get_by_hash(
        workspace_id=bundle.workspace_id, bundle_hash=bundle.dataset_hash
    )
    assert restored == result.record.to_reference()
    assert "app.workspace_id" in read_session.statements[0]
    assert "backtest_dataset_replay_bundle.workspace_id" in read_session.statements[1]
    assert "backtest_dataset_replay_bundle.bundle_hash" in read_session.statements[1]
    assert (
        await repository_type(FakeSession()).get_by_hash(
            workspace_id=bundle.workspace_id, bundle_hash=bundle.dataset_hash
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("canonical_bundle", "{}"),
        ("bundle_json", {}),
        ("symbol", "ETHUSDT"),
        ("workspace_id", uuid4()),
        ("bundle_hash", "0" * 64),
    ],
)
async def test_bundle_repository_rejects_corrupt_read_and_same_hash_retry(field, replacement):
    from test_backtest_dataset_manifest_persistence import FakeSession

    repository_type, conflict_type = _repository_type()
    bundle = _bundle()
    record_type, _ = _records()
    record = record_type.from_reference(
        workspace_id=bundle.workspace_id,
        reference=BacktestDatasetReplayBundleReference.from_bundle(bundle),
    )
    setattr(record, field, replacement)
    with pytest.raises((ValueError, conflict_type), match="khớp|canonical|hợp lệ"):
        await repository_type(FakeSession([record])).get_by_hash(
            workspace_id=bundle.workspace_id, bundle_hash=bundle.dataset_hash
        )
    with pytest.raises(conflict_type, match="canonical|khớp"):
        await repository_type(FakeSession([record])).append(
            workspace_id=bundle.workspace_id, bundle=bundle
        )


@pytest.mark.asyncio
async def test_bundle_repository_rejects_workspace_and_invalid_hash_before_query():
    from test_backtest_dataset_manifest_persistence import FakeSession

    repository_type, conflict_type = _repository_type()
    bundle = _bundle()
    session = FakeSession()
    with pytest.raises(conflict_type, match="workspace"):
        await repository_type(session).append(workspace_id=uuid4(), bundle=bundle)
    for invalid in ("invalid", "A" * 64, "", None):
        with pytest.raises(ValueError, match="SHA-256"):
            await repository_type(session).get_by_hash(
                workspace_id=bundle.workspace_id, bundle_hash=invalid
            )
    with pytest.raises(ValueError, match="UUID"):
        await repository_type(session).get_by_hash(
            workspace_id="invalid", bundle_hash=bundle.dataset_hash
        )
    assert session.statements == []


def _replay_loader_fixture(*, failure=None, reference_change=None):
    from test_backtest_dataset_replay_bundle import NOW, replay_input

    from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayLoader

    bundle = _bundle()
    reference = BacktestDatasetReplayBundleReference.from_bundle(bundle)
    if reference_change is not None:
        reference = reference_change(reference)
    manifests = {item.manifest.dataset_hash: item.manifest for item in bundle.components}
    first = bundle.components[-1]
    if failure == "missing_child":
        del manifests[first.manifest.dataset_hash]
    elif failure == "not_ready":
        manifests[first.manifest.dataset_hash] = replay_input("1h", incomplete=True).manifest

    class BundleLookup:
        async def get_by_hash(self, *, workspace_id, bundle_hash):
            return None if failure == "missing_bundle" else reference

    class ManifestLookup:
        async def get_by_hash(self, *, workspace_id, dataset_hash):
            return manifests.get(dataset_hash)

    class RightsGate:
        calls = 0

        async def authorize(self, *, request, now):
            self.calls += 1
            if request.interval == "1h" and failure in ("revoked", "expired"):
                raise PermissionError("Quyền hiện hành bị thu hồi hoặc hết hạn.")
            if request.interval == "1h" and failure == "revalidate_revoked" and self.calls > 2:
                raise PermissionError("Quyền hiện hành bị thu hồi trong lúc replay.")
            rights = bundle.components[0].current_rights
            if request.interval == "1h" and failure == "policy":
                rights = replace(rights, policy_fingerprint="0" * 64)
            return replace(rights, checked_at=now, valid_until=now + timedelta(minutes=5))

    class MarketData:
        async def list_bars(self, **query):
            item = next(
                item for item in bundle.components if item.manifest.interval == query["timeframe"]
            )
            bars = item.bars
            if query["timeframe"] == "1h" and failure == "missing_bars":
                bars = bars[:-1]
            if query["timeframe"] == "1h" and failure == "changed_bars":
                bars = (replace(bars[0], close=Decimal("99")), *bars[1:])
            return list(bars)

    loader = _loader_type()(
        repository=BundleLookup(),
        replay_loader=BacktestDatasetReplayLoader(
            repository=ManifestLookup(), rights_gate=RightsGate(), market_data=MarketData()
        ),
    )
    return loader, bundle, reference, NOW + timedelta(minutes=1)


@pytest.mark.asyncio
async def test_bundle_loader_rechecks_every_child_and_returns_fresh_rights():
    loader, bundle, reference, now = _replay_loader_fixture()
    loaded = await loader.load_for_replay(
        workspace_id=bundle.workspace_id, bundle_hash=reference.bundle_hash, now=now
    )
    assert loaded.dataset_hash == bundle.dataset_hash
    assert tuple(item.manifest for item in loaded.components) == tuple(
        item.manifest for item in bundle.components
    )
    assert tuple(item.bars for item in loaded.components) == tuple(
        item.bars for item in bundle.components
    )
    assert all(item.current_rights.checked_at == now for item in loaded.components)


@pytest.mark.asyncio
async def test_bundle_loader_revalidates_rights_without_changing_bundle_identity():
    loader, bundle, reference, now = _replay_loader_fixture()
    loaded = await loader.load_for_replay(
        workspace_id=bundle.workspace_id, bundle_hash=reference.bundle_hash, now=now
    )
    rechecked_at = now + timedelta(minutes=1)
    refreshed = await loader.revalidate_current_rights(loaded, now=rechecked_at)
    assert refreshed.dataset_hash == loaded.dataset_hash == reference.bundle_hash
    assert tuple(item.manifest for item in refreshed.components) == tuple(
        item.manifest for item in loaded.components
    )
    assert tuple(item.bars for item in refreshed.components) == tuple(
        item.bars for item in loaded.components
    )
    assert all(item.current_rights.checked_at == rechecked_at for item in refreshed.components)


@pytest.mark.asyncio
async def test_bundle_loader_revalidation_fails_if_rights_are_revoked_during_replay():
    loader, bundle, reference, now = _replay_loader_fixture(failure="revalidate_revoked")
    loaded = await loader.load_for_replay(
        workspace_id=bundle.workspace_id, bundle_hash=reference.bundle_hash, now=now
    )
    with pytest.raises(PermissionError, match="hiện hành"):
        await loader.revalidate_current_rights(loaded, now=now + timedelta(minutes=1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing_bundle",
        "missing_child",
        "not_ready",
        "revoked",
        "expired",
        "policy",
        "missing_bars",
        "changed_bars",
    ],
)
async def test_bundle_loader_returns_no_partial_bundle_on_child_failure(failure):
    from trading_bot.application.backtest_dataset_manifest import BacktestDatasetNotReady
    from trading_bot.application.backtest_dataset_replay import BacktestDatasetDataChanged

    loader, bundle, reference, now = _replay_loader_fixture(failure=failure)
    expected = {
        "missing_bundle": LookupError,
        "missing_child": LookupError,
        "not_ready": BacktestDatasetNotReady,
        "revoked": PermissionError,
        "expired": PermissionError,
        "policy": PermissionError,
        "missing_bars": BacktestDatasetDataChanged,
        "changed_bars": BacktestDatasetDataChanged,
    }[failure]
    with pytest.raises(expected):
        await loader.load_for_replay(
            workspace_id=bundle.workspace_id, bundle_hash=reference.bundle_hash, now=now
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("workspace_id", uuid4()),
        ("symbol", "ETHUSDT"),
        ("evaluation_start", datetime(2026, 11, 3, 10, tzinfo=UTC)),
        ("range_end", datetime(2026, 11, 3, 13, tzinfo=UTC)),
        ("rights_provider", "OTHER"),
        ("rights_purpose", "OTHER"),
        ("policy_fingerprint", "0" * 64),
    ],
)
async def test_bundle_loader_compares_stored_scope_with_rebuilt_bundle(field, replacement):
    loader, bundle, reference, now = _replay_loader_fixture(
        reference_change=lambda ref: replace(ref, **{field: replacement})
    )
    with pytest.raises((ValueError, PermissionError), match="khớp|workspace"):
        await loader.load_for_replay(
            workspace_id=bundle.workspace_id, bundle_hash=reference.bundle_hash, now=now
        )


@pytest.mark.asyncio
async def test_bundle_loader_rejects_reference_hash_and_component_tampering():
    loader, bundle, reference, now = _replay_loader_fixture(
        reference_change=lambda ref: replace(
            ref,
            components=(
                replace(ref.components[0], bars_content_hash="0" * 64),
                *ref.components[1:],
            ),
        )
    )
    for requested in (reference.bundle_hash, bundle.dataset_hash):
        with pytest.raises(ValueError, match="khớp"):
            await loader.load_for_replay(
                workspace_id=bundle.workspace_id, bundle_hash=requested, now=now
            )
