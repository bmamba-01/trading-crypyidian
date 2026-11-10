import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_training_admin_migration_grants_only_workspace_scoped_dataset_metadata() -> None:
    source_path = ROOT / "migrations" / "versions" / "d0e2f4a6c8b1_wp06_training_admin_metadata.py"
    source = source_path.read_text(encoding="utf-8")
    module = ast.parse(source)
    metadata_columns = next(
        ast.literal_eval(statement.value)
        for statement in module.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "METADATA_COLUMNS"
            for target in statement.targets
        )
    )

    assert 'down_revision: str | None = "c2d4e6f8a0b3"' in source
    assert "FOR SELECT TO " in source
    assert "{ADMIN} USING ({WORKSPACE})" in source
    assert set(metadata_columns) == {
        "workspace_id",
        "id",
        "manifest_hash",
        "split_policy_version",
        "fold_plan_hash",
        "approval_hash",
        "content_hash",
        "sample_count",
        "assignment_count",
        "created_at",
    }
    assert "features" not in metadata_columns
    assert "label_value" not in metadata_columns
    assert "holdout" not in metadata_columns
