from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from trading_bot.adapters.database import models


def test_performance_report_table_is_workspace_scoped_aggregate_only_and_immutable() -> None:
    assert hasattr(models, "LearningPaperPerformanceReportRecord")
    table = models.LearningPaperPerformanceReportRecord.__table__
    assert table.name == "learning_paper_performance_report"
    assert not {
        "probabilities",
        "labels",
        "trade_id",
        "decision_id",
        "trade_payload",
        "feature_payload",
    } & set(table.c.keys())

    constraints = tuple(table.constraints)
    assert any(
        isinstance(item, UniqueConstraint)
        and {column.name for column in item.columns} == {"workspace_id", "idempotency_key"}
        for item in constraints
    )
    references = [
        {element.target_fullname for element in item.elements}
        for item in constraints
        if isinstance(item, ForeignKeyConstraint)
    ]
    assert {
        "learning_scorer_test_evaluation_report.workspace_id",
        "learning_scorer_test_evaluation_report.id",
    } in references
    checks = " ".join(
        str(item.sqltext) for item in constraints if isinstance(item, CheckConstraint)
    )
    assert "target_fingerprint" in checks
    assert "report_hash" in checks
    assert "jsonb_typeof(report) = 'object'" in checks
