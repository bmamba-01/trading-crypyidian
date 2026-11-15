from pathlib import Path


def test_builder_manifest_insert_requires_exact_staged_candidate_approval() -> None:
    migration = Path("migrations/versions/d2b4c6e8f0a1_wp06_builder_manifest_seal.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision: str | None = "d1e3f5a7c9b2"' in migration
    assert "dataset_manifest_builder_insert" in migration
    assert "FOR INSERT TO {BUILDER} WITH CHECK ({WORKSPACE})" in migration
    assert "GRANT INSERT ON TABLE public.dataset_manifest TO {BUILDER}" in migration
    assert "current_user = '{BUILDER}'" in migration
    assert "JOIN public.learning_split_approval AS approval" in migration
    assert "approval.fold_plan_hash = candidate.fold_plan_hash" in migration
    assert "candidate.canonical_manifest = NEW.canonical_manifest" in migration
    assert "WP06_BUILDER_MANIFEST_REQUIRES_APPROVED_CANDIDATE" in migration


def test_split_candidate_populated_downgrade_guard_runs_before_drops() -> None:
    """Guard split candidate không còn chạm tới được trong E2E.

    Từ migration b7d9f1a3c5e8 (14/11/2026) receipt replay TRAINING đã ghi chặn downgrade trước.
    """
    source = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "d1e3f5a7c9b2_wp06_split_candidates.py"
    ).read_text(encoding="utf-8")
    downgrade = source.split("def downgrade", 1)[1]

    guard = downgrade.index("WP06_SPLIT_CANDIDATE_DOWNGRADE_REQUIRES_EMPTY_TABLE")
    assert guard < downgrade.index("DROP TRIGGER")
    assert guard < downgrade.index("DROP FUNCTION")
    assert guard < downgrade.index("DROP POLICY")
