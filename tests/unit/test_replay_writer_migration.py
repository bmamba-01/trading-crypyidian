"""Kiểm quyền hẹp của principal ghi kết quả replay WP06."""

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "f5a7c9e1b3d6_wp06_replay_writer_role.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wp06_replay_writer_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_gives_replay_writer_only_workspace_scoped_persistence() -> None:
    module = _load()
    statements: list[str] = []
    module.op = SimpleNamespace(execute=lambda statement: statements.append(str(statement)))

    module.upgrade()

    rendered = "\n".join(statements)
    assert module.down_revision == "e6c8a0b2d4f7"
    assert "crypidian_replay_writer" in rendered
    assert "WP06_REPLAY_WRITER_ROLE_UNSAFE" in rendered
    assert (
        "GRANT SELECT, INSERT ON TABLE public.backtest_run TO crypidian_replay_writer" in rendered
    )
    assert (
        "GRANT SELECT, INSERT ON TABLE public.backtest_run_dataset_rights "
        "TO crypidian_replay_writer"
    ) in rendered
    assert (
        "REVOKE INSERT ON TABLE public.backtest_trade_outcome FROM crypidian_trainer_worker"
        in rendered
    )
    assert "REVOKE EXECUTE ON FUNCTION public.begin_backtest_job_persist" in rendered
    assert "GRANT EXECUTE ON FUNCTION public.begin_backtest_job_persist" in rendered
    assert "TO crypidian_replay_writer" in rendered
    assert "GRANT EXECUTE ON FUNCTION public.wp06_entry_features_valid(jsonb)" in rendered
    assert (
        "GRANT SELECT ON TABLE public.source_policy_revision TO crypidian_replay_writer" in rendered
    )
    assert (
        "GRANT SELECT ON TABLE public.source_policy_observation TO crypidian_replay_writer"
        in rendered
    )
    assert "FOR SELECT TO crypidian_replay_writer USING (workspace_id = NULLIF" in rendered
    assert "GRANT SELECT ON TABLE public.market_bar TO crypidian_replay_writer" not in rendered


def test_downgrade_restores_trainer_persist_capability_and_removes_writer_grants() -> None:
    module = _load()
    statements: list[str] = []
    module.op = SimpleNamespace(execute=lambda statement: statements.append(str(statement)))

    module.downgrade()

    rendered = "\n".join(statements)
    assert "GRANT INSERT ON TABLE public.backtest_run TO crypidian_trainer_worker" in rendered
    assert "GRANT EXECUTE ON FUNCTION public.begin_backtest_job_persist" in rendered
    assert "TO crypidian_trainer_worker" in rendered
    assert (
        "REVOKE ALL PRIVILEGES ON TABLE public.backtest_run FROM crypidian_replay_writer"
        in rendered
    )
    assert (
        "REVOKE SELECT ON TABLE public.source_policy_revision FROM crypidian_replay_writer"
        in rendered
    )
    assert "REVOKE ALL PRIVILEGES ON FUNCTION public.begin_backtest_job_persist" in rendered
    assert "REVOKE ALL PRIVILEGES ON FUNCTION public.wp06_entry_features_valid(jsonb)" in rendered


def test_candidate_source_metadata_migration_has_only_identity_columns():
    path = MIGRATION.parent / "c4e6a8b0d2f3_wp06_candidate_source_metadata.py"
    spec = importlib.util.spec_from_file_location("candidate_source_metadata", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = []
    module.op = SimpleNamespace(execute=lambda statement: statements.append(str(statement)))
    module.upgrade()
    grants = [statement for statement in statements if statement.startswith("GRANT")]
    assert len(grants) == 1
    assert "SELECT (" in grants[0]
    assert "TO crypidian_candidate_builder" in grants[0]
    columns = grants[0].split("SELECT (", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
    assert set(columns) == {
        "id",
        "workspace_id",
        "symbol",
        "timeframe",
        "source",
        "logical_key",
        "revision",
        "supersedes_id",
        "checksum",
        "raw_reference_id",
        "open_time",
        "close_time",
        "available_at",
        "is_closed",
    }
    assert module.down_revision == "b2d4f6a8c0e1"
    assert not any("POLICY" in value for value in statements)
    statements.clear()
    module.downgrade()
    assert any(value.startswith("REVOKE SELECT (") for value in statements)
