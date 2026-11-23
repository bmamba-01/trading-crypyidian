"""Unit test cho metric WP-07E và cảnh báo vận hành tiếng Việt (CRY-376)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.observability.workbench_metrics import (
    AlertCategory,
    AlertSeverity,
    BackfillProgressMetric,
    DatasetRightsInput,
    StrategyPublishInput,
    TelegramBindingHealthInput,
    TelegramSubscriptionHealthInput,
    WorkbenchAlert,
    compute_percentile_seconds,
    evaluate_bar_freshness,
    evaluate_workbench_metrics_and_alerts,
)


def test_evaluate_bar_freshness_and_percentiles() -> None:
    now = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    fresh = evaluate_bar_freshness(
        symbol="BTCUSDT",
        timeframe="1h",
        latest_bar_close_at=now - timedelta(minutes=10),
        expected_bars=24,
        actual_bars=24,
        gap_count=0,
        max_age_seconds=7200,
        now=now,
        trace_run_id="run-1",
        evidence_id="ev-1",
    )
    assert fresh.is_stale is False
    assert fresh.coverage_ratio == 1.0
    assert fresh.gap_count == 0
    assert fresh.age_seconds == 600.0

    stale = evaluate_bar_freshness(
        symbol="ETHUSDT",
        timeframe="15m",
        latest_bar_close_at=now - timedelta(hours=3),
        expected_bars=96,
        actual_bars=48,
        gap_count=48,
        max_age_seconds=1800,
        now=now,
        trace_run_id="run-2",
        evidence_id="ev-2",
    )
    assert stale.is_stale is True
    assert stale.coverage_ratio == 0.5
    assert stale.gap_count == 48

    assert compute_percentile_seconds([1.0, 2.0, 10.0, 4.0], 0.50) == 2.0
    assert compute_percentile_seconds([1.0, 2.0, 10.0, 4.0], 0.95) == 10.0
    assert compute_percentile_seconds([], 0.95) is None


def test_evaluate_workbench_metrics_covers_all_wp07e_alerts_and_traces() -> None:
    now = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    stale_bar = evaluate_bar_freshness(
        symbol="SOLUSDT",
        timeframe="1h",
        latest_bar_close_at=now - timedelta(hours=5),
        expected_bars=24,
        actual_bars=20,
        gap_count=4,
        max_age_seconds=3600,
        now=now,
        trace_run_id="run-bars-99",
        evidence_id="ev-bars-99",
    )
    report = evaluate_workbench_metrics_and_alerts(
        now=now,
        bar_freshness=[stale_bar],
        last_scan_at=now - timedelta(minutes=12),
        scanner_max_lag_seconds=180,
        scanner_trace_run_id="scan-run-77",
        delivery_latencies_seconds=[0.4, 1.1, 3.8],
        delivery_retry_count=2,
        delivery_dead_letter_count=1,
        delivery_trace_outbox_id="outbox-dead-1",
        backfill_progress=[
            BackfillProgressMetric(
                job_id="bf-1",
                manifest_id="mf-1",
                processed_bars=500,
                total_bars=1000,
                progress_ratio=0.5,
                status="RUNNING",
                archive_checksum="sha256:abc",
            )
        ],
        strategy_statuses=[
            StrategyPublishInput(
                bot_name="Bot-Alpha",
                release_id="h01-v1",
                status="DRAFT",
                trace_ref="rel-draft-1",
            )
        ],
        telegram_bindings=[
            TelegramBindingHealthInput(
                binding_id="bind-revoked-1",
                revoked_at=now - timedelta(hours=1),
            )
        ],
        telegram_subscriptions=[
            TelegramSubscriptionHealthInput(
                subscription_id="sub-bad-1",
                bot_name="Bot-Alpha",
                enabled=True,
                binding_active=False,
            )
        ],
        dataset_rights=[
            DatasetRightsInput(
                grant_id="grant-exp-1",
                source_code="BINANCE_USDM_REST",
                status="verified",
                expires_at=now - timedelta(minutes=5),
                manifest_id="mf-rights-1",
            )
        ],
    )

    payload = report.to_dict()
    assert payload["summary"]["alert_count"] == 7
    assert payload["summary"]["stale_bar_streams"] == 1
    assert payload["summary"]["total_bar_gaps"] == 4
    assert payload["summary"]["scanner_lagging"] is True
    assert payload["summary"]["delivery_dead_letters"] == 1
    assert payload["summary"]["active_backfills"] == 1

    categories = {item["category"] for item in payload["alerts"]}
    assert categories == {
        AlertCategory.DATA_STALE.value,
        AlertCategory.SCANNER_LAG.value,
        AlertCategory.STRATEGY_UNPUBLISHED.value,
        AlertCategory.BINDING_REVOKED.value,
        AlertCategory.SUBSCRIPTION_INVALID.value,
        AlertCategory.DATASET_RIGHTS_EXPIRED.value,
        AlertCategory.DELIVERY_DEAD_LETTER.value,
    }
    trace_refs = {item["code"]: item["trace_ref"] for item in payload["alerts"]}
    assert trace_refs["MARKET_DATA_STALE"] == "ev-bars-99"
    assert trace_refs["SCANNER_LAG_HIGH"] == "scan-run-77"
    assert trace_refs["STRATEGY_NOT_PUBLISHED"] == "rel-draft-1"
    assert trace_refs["TELEGRAM_BINDING_REVOKED"] == "bind-revoked-1"
    assert trace_refs["TELEGRAM_SUBSCRIPTION_INVALID"] == "sub-bad-1"
    assert trace_refs["DATASET_RIGHTS_EXPIRED"] == "mf-rights-1"
    assert trace_refs["TELEGRAM_DELIVERY_DEAD_LETTER"] == "outbox-dead-1"


def test_workbench_alerts_redact_tokens_and_pii() -> None:
    alert = WorkbenchAlert(
        code="TELEGRAM_BINDING_REVOKED",
        severity=AlertSeverity.WARNING,
        category=AlertCategory.BINDING_REVOKED,
        title_vi="Lỗi token=123456:CANARY_BOT_TOKEN_SECRET",
        detail_vi="Bearer CANARY_BEARER_SECRET trong header",
        trace_ref="bind-1",
        metadata={
            "chat_id": "999888777",
            "telegram_user_id": "111222333",
            "bot_token": "999:SECRET_TOKEN",
            "email": "trader@example.com",
            "binding_id": "bind-1",
        },
    )
    serialized = json.dumps(alert.to_dict(), ensure_ascii=False)
    assert "CANARY_BOT_TOKEN_SECRET" not in serialized
    assert "CANARY_BEARER_SECRET" not in serialized
    assert "999888777" not in serialized
    assert "111222333" not in serialized
    assert "999:SECRET_TOKEN" not in serialized
    assert "trader@example.com" not in serialized
    assert "bind-1" in serialized


def test_admin_api_workbench_metrics_alerts_endpoint() -> None:
    app = FastAPI()
    app.include_router(admin_web_router)
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=Principal(
            user_id=uuid4(),
            membership_id=uuid4(),
            workspace_id=uuid4(),
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
        ),
        email="admin@crypidian.test",
    )
    client = TestClient(app)
    resp = client.get("/admin/api/workbench/metrics-alerts")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    body = resp.json()
    assert "summary" in body
    assert "bar_freshness" in body
    assert "scanner_lag" in body
    assert "delivery_latency" in body
    assert "alerts" in body
