"""Kiểm tra migration bảo vệ chuyển account DISABLED → ENABLED của MB-07b."""

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations" / "versions"
REVISION = "c8e4a2f6d190"


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


def _run(name: str) -> tuple[ModuleType, list[str]]:
    module = _load()
    recorder = RecordingOp()
    original = module.op
    module.op = recorder
    try:
        getattr(module, name)()
    finally:
        module.op = original
    return module, recorder.statements


def test_enable_migration_is_child_of_current_head_and_keeps_flag_transaction_local() -> None:
    module, statements = _run("upgrade")
    joined = "\n".join(statements)

    assert module.down_revision == "b3d5f7a9c1e4"
    assert "app.bot_enable_authorized" in joined
    assert "COALESCE(current_setting('app.bot_enable_authorized', true), '') = 'on'" in joined
    assert "IS LOCAL" not in joined  # set_config(..., true) is transaction-local.
    assert "DISABLED" in joined
    assert "ENABLED" in joined


def test_downgrade_restores_disable_only_guard() -> None:
    _, statements = _run("downgrade")

    assert any("MB07A_ACCOUNT_STATUS_TRANSITION_DENIED" in statement for statement in statements)
    assert not any("app.bot_enable_authorized" in statement for statement in statements)
