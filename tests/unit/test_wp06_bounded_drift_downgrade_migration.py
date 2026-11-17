"""Kiểm tĩnh guard downgrade bounded drift của c3f5a7b9d1e3.

Từ `b7d9f1a3c5e8` receipt replay TRAINING đã ghi chặn downgrade trước, nên E2E task9 có dữ
liệu không còn chạm tới guard này; cùng tiền lệ downgrade-che-guard của WP06/WP07.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2] / "migrations/versions/c3f5a7b9d1e3_wp06_bounded_drift_window.py"
)


def _downgrade_statements() -> list[str]:
    spec = importlib.util.spec_from_file_location("wp06_bounded_drift_window", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []

    class _RecordingOp:
        """Ghi mọi lời gọi alembic `op.*`; SQL thô ghi nguyên văn, thao tác khác ghi tên."""

        def execute(self, sql: object) -> None:
            statements.append(str(sql))

        def f(self, name: str) -> str:
            return name

        def __getattr__(self, name: str):
            return lambda *args, **kwargs: statements.append(f"OP {name} {args!r}")

    module.op = _RecordingOp()
    module.downgrade()
    return statements


def test_downgrade_refuses_populated_coverage_v2_assessments_before_any_rewrite() -> None:
    statements = _downgrade_statements()
    guard = statements[0]
    assert "FROM public.learning_drift_assessment" in guard
    assert "policy_version = 'coverage-v2-30d'" in guard
    assert "RAISE EXCEPTION 'WP06_BOUNDED_DRIFT_DOWNGRADE_POPULATED'" in guard
    # Guard phải chạy trước khi khôi phục trigger cũ để không sửa nửa chừng.
    assert all("WP06_BOUNDED_DRIFT_DOWNGRADE_POPULATED" not in item for item in statements[1:])
    assert any("wp06_lock_learning_drift_assessment_cohort" in item for item in statements[1:])


def test_downgrade_does_not_delete_drift_history() -> None:
    joined = "\n".join(_downgrade_statements())
    assert "DELETE FROM" not in joined
    assert "TRUNCATE" not in joined
