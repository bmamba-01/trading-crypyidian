"""Kiểm tra RLS, grants và chain migration cho ledger flatten-all."""

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations" / "versions"
REVISION = "d1f3a5b7c9e2"


def _load() -> ModuleType:
    path = next(VERSIONS.glob(f"{REVISION}_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        self.statements.append(" ".join(str(sql).split()))

    def __getattr__(self, name: str) -> object:
        def record(*args: object, **kwargs: object) -> None:
            self.statements.append(f"{name} {args!r} {kwargs!r}")

        return record


def _run(function: str) -> tuple[ModuleType, list[str]]:
    module = _load()
    recorder = RecordingOp()
    original = module.op
    module.op = recorder
    try:
        getattr(module, function)()
    finally:
        module.op = original
    return module, recorder.statements


def test_migration_follows_enable_and_creates_three_rls_ledgers() -> None:
    module, statements = _run("upgrade")
    joined = "\n".join(statements)

    assert module.down_revision == "c8e4a2f6d190"
    for table in ("flatten_preview", "flatten_operation", "flatten_operation_item"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in joined
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in joined
    assert "app.membership_id" in joined
    assert "CREATE POLICY flatten_operation_runtime_update" in joined
    assert "GRANT UPDATE (status, attempt_count, result, last_error_code, updated_at)" in joined
    assert "FROM PUBLIC, crypidian_app" in joined


def test_downgrade_drops_only_flatten_tables_and_revokes_process_grants() -> None:
    _, statements = _run("downgrade")
    joined = "\n".join(statements)

    assert (
        "REVOKE ALL ON TABLE flatten_preview, flatten_operation, flatten_operation_item" in joined
    )
    assert "flatten_operation_item" in joined
    assert "flatten_operation" in joined
    assert "flatten_preview" in joined
