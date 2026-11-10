from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission, _to_job


def test_training_intent_job_hides_result_run_ids_from_general_job_reader() -> None:
    now = datetime(2026, 11, 8, tzinfo=UTC)
    request = BacktestReplayJobSubmission(
        bundle_hash="a" * 64,
        strategy_release_id="h01-v1",
        execution_interval="1h",
        starting_equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0"),
        seed=7,
        learning_intent="TRAINING_BASE",
    )
    payload = request.to_payload()
    row = BacktestReplayJobRecord(
        id=UUID("51627cc6-38e7-44e3-8f6f-6e3c3f5a81c1"),
        workspace_id=UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510"),
        requested_by=UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc"),
        bundle_hash=request.bundle_hash,
        strategy_release_id=request.strategy_release_id,
        execution_interval=request.execution_interval,
        request_payload=payload,
        canonical_request=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        status="SUCCEEDED",
        stage="completed",
        progress=100,
        cancel_requested=False,
        attempt_count=1,
        lease_generation=1,
        result_run_ids=["f4a4a6f6-2a43-46db-962e-7d0f39df55ef"],
        created_at=now,
        updated_at=now,
    )

    job = _to_job(row)

    assert job.learning_intent == "TRAINING_BASE"
    assert job.result_run_ids == ()
