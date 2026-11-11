from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories.learning_scorer_runtime_committer import (
    PostgresScorerRuntimeCommitter,
)
from trading_bot.application import market_signal_composition
from trading_bot.application.learning_scorer_evaluation import ScorerTestEvaluationPolicy
from trading_bot.application.market_signal_composition import _latest_bar_rows
from trading_bot.application.scorer_runtime_policy import ConfiguredScorerRuntimePolicyProvider

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class _ScalarRows:
    def __init__(self, values: tuple[object, ...]) -> None:
        self.values = values

    def all(self) -> tuple[object, ...]:
        return self.values


class _Session:
    def __init__(self, values_by_query: tuple[tuple[object, ...], ...]) -> None:
        self.values_by_query = values_by_query
        self.statements = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return _ScalarRows(self.values_by_query[len(self.statements) - 1])


@pytest.mark.asyncio
async def test_latest_bar_rows_selects_bounded_times_then_preferred_revisions() -> None:
    base = datetime(2026, 11, 8, tzinfo=UTC)
    values_by_query = (
        (base,),
        (object(),),
        (base, base.replace(hour=1)),
        (object(), object()),
        (base,),
        (object(),),
    )
    session = _Session(values_by_query)

    result = await _latest_bar_rows(
        session,  # type: ignore[arg-type]
        workspace_id=WORKSPACE_ID,
        as_of=base,
        symbols=("BTCUSDT", "ETHUSDT", "BNBUSDT"),
        timeframes=("1h",),
        max_bars_per_stream=256,
    )

    expected_rows = tuple(values_by_query[index] for index in (1, 3, 5))
    assert result == tuple(row for stream_rows in expected_rows for row in stream_rows)
    assert len(session.statements) == 6
    for index, symbol in enumerate(("BNBUSDT", "BTCUSDT", "ETHUSDT")):
        times_statement = session.statements[index * 2]
        revision_statement = session.statements[index * 2 + 1]
        times_sql = str(times_statement.compile(dialect=postgresql.dialect()))
        revision_sql = str(revision_statement.compile(dialect=postgresql.dialect()))
        assert symbol in times_statement.compile().params.values()
        assert symbol in revision_statement.compile().params.values()
        assert "SELECT DISTINCT market_bar.open_time" in times_sql
        assert "LIMIT" in times_sql
        assert "DISTINCT ON (market_bar.open_time)" in revision_sql
        assert revision_sql.lstrip().upper().startswith("SELECT ")
        for sql in (times_sql.lower(), revision_sql.lower()):
            assert "market_bar.workspace_id =" in sql
            assert "market_bar.symbol =" in sql
            assert "market_bar.timeframe =" in sql
            assert "market_bar.source in" in sql
            assert "market_bar.is_closed is true" in sql
            assert "market_bar.close_time <=" in sql
            assert "market_bar.available_at <=" in sql
        assert "order by market_bar.open_time desc" in times_sql.lower()
        revision_order = revision_sql.lower().split("order by", maxsplit=1)[1]
        assert revision_order.index("market_bar.open_time") < revision_order.index("case when")
        assert revision_order.index("case when") < revision_order.index(
            "market_bar.available_at desc"
        )
        assert revision_order.index("market_bar.available_at desc") < revision_order.index(
            "market_bar.revision desc"
        )
        assert revision_order.index("market_bar.revision desc") < revision_order.index(
            "market_bar.id desc"
        )


def test_market_signal_composition_wires_configured_scorer_committer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    class Store:
        def __init__(self, _sessions, *, score_candidate):
            captured.append(score_candidate)

    monkeypatch.setattr(market_signal_composition, "PostgresSignalDecisionStore", Store)
    policy = ScorerTestEvaluationPolicy(
        policy_version="scorer-runtime-test-v1",
        minimum_sample_count=1000,
        minimum_class_support=100,
        maximum_calibration_error=0.05,
        maximum_log_loss_regression=0.01,
        maximum_brier_regression=0.01,
        minimum_coverage=0.8,
        minimum_candidate_retention=0.2,
        maximum_false_veto_rate=0.1,
        approve_probability_threshold=0.6,
        minimum_confidence=0.55,
        reliability_bin_count=10,
        maximum_standardized_feature_deviation=6.0,
        runtime_inference_timeout_ms=100,
    )
    provider = ConfiguredScorerRuntimePolicyProvider(policy)

    market_signal_composition.build_market_signal_scanner(
        object(),
        max_observation_age=timedelta(seconds=30),
        signal_validity=timedelta(seconds=30),
        scorer_policy_provider=provider,
    )

    assert len(captured) == 1
    assert isinstance(captured[0], PostgresScorerRuntimeCommitter)
    assert captured[0]._policy_provider is provider
