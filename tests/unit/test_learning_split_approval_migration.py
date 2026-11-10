from pathlib import Path

from trading_bot.adapters.database.models import LearningSplitApprovalRecord


def test_split_approval_migration_is_tenant_scoped_immutable_and_preseal() -> None:
    migration = Path("migrations/versions/f9b1d3e5a7c9_wp06_split_approval.py").read_text(
        encoding="utf-8"
    )

    assert LearningSplitApprovalRecord.__tablename__ == "learning_split_approval"
    assert 'down_revision: str | None = "f5a7c9e1b3d6"' in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "FOR ALL TO {ADMIN} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})" in migration
    assert "GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {ADMIN}" in migration
    assert "FROM PUBLIC, {APP}, {ADMIN}, {TRAINER}" in migration
    assert "WP06_LEARNING_SPLIT_APPROVAL_IMMUTABLE" in migration
    assert "WP06_SPLIT_APPROVAL_DOWNGRADE_REQUIRES_EMPTY_TABLE" in migration
    assert "manifest_hash" in migration
    assert "fold_plan_hash" in migration
