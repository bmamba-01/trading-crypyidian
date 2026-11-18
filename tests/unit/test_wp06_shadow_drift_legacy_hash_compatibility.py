from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations/versions/f8e0a2c4d6bb_wp06_shadow_drift_source_scope.py"
)


def _function_body(name: str) -> str:
    source = MIGRATION.read_text(encoding="utf-8")
    start = source.index(f"CREATE OR REPLACE FUNCTION public.{name}(")
    body_start = source.index("AS $$", start) + len("AS $$")
    body_end = source.index("END $$", body_start)
    return source[body_start:body_end]


def test_legacy_shadow_hashes_keep_json_field_order_when_scope_is_missing() -> None:
    for name in (
        "wp06_shadow_observation_idempotency_key",
        "wp06_shadow_observation_canonical_document",
    ):
        body = _function_body(name)
        legacy_branch = body[body.index("IF row_value.source_scope_fingerprint IS NULL THEN") :]
        legacy_branch = legacy_branch[: legacy_branch.index("END IF;")]

        assert "pg_catalog.json_build_object" in legacy_branch
        assert "pg_catalog.jsonb_build_object" not in legacy_branch
        assert "source_scope_fingerprint IS NULL" in body


def test_legacy_assessment_hashes_keep_json_field_order_without_rights_digest() -> None:
    for name in (
        "wp06_drift_assessment_idempotency_key",
        "wp06_drift_assessment_content_hash",
    ):
        body = _function_body(name)
        legacy_branch = body[body.index("IF row_value.source_rights_digest IS NULL THEN") :]
        legacy_branch = legacy_branch[: legacy_branch.index("END IF;")]

        assert "pg_catalog.json_build_object" in legacy_branch
        assert "pg_catalog.jsonb_build_object" not in legacy_branch
        assert "source_rights_digest IS NULL" in body
