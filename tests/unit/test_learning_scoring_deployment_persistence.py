from __future__ import annotations

from pathlib import Path

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from trading_bot.adapters.database.models.base import Base


def test_scorer_deployment_is_tenant_scoped_immutable_metadata_only() -> None:
    events = Base.metadata.tables["learning_scoring_deployment_event"]
    pointer = Base.metadata.tables["learning_scoring_deployment_pointer"]
    forbidden = {
        "feature_vector",
        "feature_values",
        "labels",
        "sample_ids",
        "test_payload",
        "raw_source",
    }

    assert not forbidden & set(events.columns.keys())
    assert {
        "event_hash",
        "intent_hash",
        "canonical_event",
        "current_rights_receipt_hash",
    } <= set(events.columns.keys())
    assert "event_id" in pointer.columns
    assert any(
        isinstance(item, UniqueConstraint)
        and tuple(column.name for column in item.columns) == ("workspace_id", "idempotency_key")
        for item in events.constraints
    )
    assert any(
        isinstance(item, ForeignKeyConstraint)
        and tuple(column.name for column in item.columns) == ("workspace_id", "actor_membership_id")
        and item.referred_table.name == "user_membership"
        for item in events.foreign_key_constraints
    )


def test_session_schema_keeps_unverified_legacy_mfa_method_unknown() -> None:
    session = Base.metadata.tables["session"]
    assert session.c.mfa_method.nullable is True


def test_deployment_migration_scopes_event_insert_and_drops_jsonb_function_signature() -> None:
    root = Path(__file__).parents[2]
    migration = (
        root / "migrations" / "versions" / "c5e7a9b1d3f5_wp06_scorer_deployment.py"
    ).read_text(encoding="utf-8")

    assert "FOR INSERT TO {ADMIN} WITH CHECK ({WORKSPACE})" in migration
    assert "DROP FUNCTION public.wp06_scoring_deployment_payload_has_forbidden(jsonb)" in migration
    assert "length(btrim(reason)) BETWEEN 12 AND 2000" in migration
