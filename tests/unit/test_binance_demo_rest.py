from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from trading_bot.adapters.binance_usdm.demo_rest import (
    BinanceAdapterError,
    BinanceHttpRequest,
    BinanceHttpResponse,
    DemoApiCredentials,
    DemoUsdMRestAdapter,
    OrderAccepted,
    OrderFound,
    OrderMissing,
    SubmissionFailed,
    TransportNotSent,
)
from trading_bot.adapters.binance_usdm.demo_target import BinanceDemoTarget
from trading_bot.adapters.binance_usdm.submission_errors import BinanceFailureKind
from trading_bot.domain.execution.venue_orders import SubmissionOutcome, VenueOrderIntent

NOW_MS = 1_798_214_400_000


class FakeTransport:
    def __init__(self, response: BinanceHttpResponse | Exception) -> None:
        self.response = response
        self.requests: list[BinanceHttpRequest] = []

    def send(self, request: BinanceHttpRequest) -> BinanceHttpResponse:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def make_intent(*, order_type: str = "MARKET") -> VenueOrderIntent:
    kwargs: dict[str, object] = {
        "intent_id": uuid4(),
        "workspace_id": "workspace-1",
        "account_id": "demo-account",
        "idempotency_key": "plan-1-entry",
        "reservation_id": uuid4(),
        "plan_hash": "a" * 64,
        "settings_version": "settings-1",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "quantity": Decimal("0.01"),
        "client_order_id": "crypidian-1-entry",
        "created_at": datetime.fromtimestamp(NOW_MS / 1000, tz=UTC),
        "order_type": order_type,
    }
    if order_type == "LIMIT":
        kwargs["limit_price"] = Decimal("65000.25")
    return VenueOrderIntent(**kwargs)  # type: ignore[arg-type]


def make_started_intent(*, order_type: str = "MARKET", attempts: int = 1) -> VenueOrderIntent:
    order = make_intent(order_type=order_type)
    for attempt in range(attempts):
        submitted_at = order.created_at + timedelta(seconds=attempt + 1)
        order = order.begin_submission(at=submitted_at)
        if attempt + 1 < attempts:
            order = order.resolve_submission(
                outcome=SubmissionOutcome.NOT_SENT,
                observed_at=submitted_at + timedelta(milliseconds=1),
            )
    return order


def make_adapter(
    response: BinanceHttpResponse | Exception,
) -> tuple[DemoUsdMRestAdapter, FakeTransport]:
    target = BinanceDemoTarget(account_id="demo-account", credential_ref="secret-ref/demo")
    credentials = DemoApiCredentials(
        workspace_id="workspace-1",
        account_id="demo-account",
        credential_ref="secret-ref/demo",
        api_key="test-api-key",
        api_secret="test-api-secret",
    )
    transport = FakeTransport(response)
    adapter = DemoUsdMRestAdapter(
        target=target,
        workspace_id="workspace-1",
        credentials=credentials,
        transport=transport,
        clock_ms=lambda: NOW_MS,
    )
    return adapter, transport


def response(
    status: int, payload: object, *, headers: dict[str, str] | None = None
) -> BinanceHttpResponse:
    return BinanceHttpResponse(
        status_code=status,
        headers=headers or {},
        body=json.dumps(payload).encode(),
    )


def test_submit_signs_demo_market_order_and_never_uses_production_host() -> None:
    adapter, transport = make_adapter(
        response(
            200,
            {
                "symbol": "BTCUSDT",
                "orderId": 123,
                "clientOrderId": "crypidian-1-entry",
                "status": "NEW",
            },
        )
    )

    result = adapter.submit_order(make_started_intent())

    assert result.venue_order_id == "123"
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == "https://demo-fapi.binance.com/fapi/v1/order"
    assert dict(request.headers)["X-MBX-APIKEY"] == "test-api-key"
    assert urlsplit(request.url).netloc == "demo-fapi.binance.com"
    params = parse_qs(request.body.decode())
    assert params["symbol"] == ["BTCUSDT"]
    assert params["side"] == ["BUY"]
    assert params["type"] == ["MARKET"]
    assert params["quantity"] == ["0.01"]
    assert params["newClientOrderId"] == ["crypidian-1-entry"]
    assert params["timestamp"] == [str(NOW_MS)]
    unsigned = request.body.decode().split("&signature=", maxsplit=1)[0]
    expected = hmac.new(b"test-api-secret", unsigned.encode(), hashlib.sha256).hexdigest()
    assert params["signature"] == [expected]


def test_same_intent_attempt_is_claimed_once_before_second_transport_call() -> None:
    adapter, transport = make_adapter(
        response(
            200,
            {
                "symbol": "BTCUSDT",
                "orderId": 123,
                "clientOrderId": "crypidian-1-entry",
                "status": "NEW",
            },
        )
    )
    submitting = make_intent().begin_submission(at=datetime.fromtimestamp(NOW_MS / 1000, tz=UTC))

    assert isinstance(adapter.submit_order(submitting), OrderAccepted)
    with pytest.raises(BinanceAdapterError):
        adapter.submit_order(submitting)

    assert len(transport.requests) == 1


def test_limit_order_uses_gtc_limit_fields_and_no_conditionals() -> None:
    adapter, transport = make_adapter(
        response(
            200,
            {
                "symbol": "BTCUSDT",
                "orderId": 123,
                "clientOrderId": "crypidian-1-entry",
                "status": "NEW",
            },
        )
    )

    adapter.submit_order(make_started_intent(order_type="LIMIT"))

    params = parse_qs(transport.requests[0].body.decode())
    assert params["type"] == ["LIMIT"]
    assert params["price"] == ["65000.25"]
    assert params["timeInForce"] == ["GTC"]
    assert not {"stopPrice", "closePosition", "activationPrice"}.intersection(params)


def test_query_signs_lookup_by_client_order_id_and_returns_normalized_facts() -> None:
    adapter, transport = make_adapter(
        response(
            200,
            {
                "symbol": "BTCUSDT",
                "orderId": 123,
                "clientOrderId": "crypidian-1-entry",
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.004",
                "updateTime": NOW_MS,
            },
        )
    )

    result = adapter.query_order(make_intent())

    assert isinstance(result, OrderFound)
    assert result.venue_order_id == "123"
    assert result.client_order_id == "crypidian-1-entry"
    assert result.cumulative_filled == Decimal("0.004")
    request = transport.requests[0]
    assert request.method == "GET"
    assert urlsplit(request.url).scheme == "https"
    assert urlsplit(request.url).netloc == "demo-fapi.binance.com"
    params = parse_qs(urlsplit(request.url).query)
    assert params["symbol"] == ["BTCUSDT"]
    assert params["origClientOrderId"] == ["crypidian-1-entry"]


def test_order_query_not_found_is_not_authoritative_trade_history_absence() -> None:
    adapter, transport = make_adapter(
        response(400, {"code": -2013, "msg": "Order does not exist."})
    )

    result = adapter.query_order(make_intent())

    assert result == OrderMissing(client_order_id="crypidian-1-entry", authoritative_absence=False)
    assert len(transport.requests) == 1


def test_429_returns_mandatory_backoff_deadline_and_does_not_retry() -> None:
    adapter, transport = make_adapter(response(429, {"code": -1003, "msg": "Rate limited"}))

    result = adapter.submit_order(make_started_intent(attempts=2))

    assert isinstance(result, SubmissionFailed)
    assert result.failure.kind is BinanceFailureKind.RATE_LIMITED
    assert result.failure.backoff_required is True
    assert result.retry_not_before == datetime.fromtimestamp(NOW_MS / 1000 + 2, tz=UTC)
    assert len(transport.requests) == 1


def test_documented_503_failure_has_backoff_deadline_without_adapter_retry() -> None:
    adapter, transport = make_adapter(response(503, {"msg": "Service Unavailable."}))

    result = adapter.submit_order(make_started_intent(attempts=10))

    assert isinstance(result, SubmissionFailed)
    assert result.failure.kind is BinanceFailureKind.RETRYABLE_FAILURE
    assert result.failure.backoff_required is True
    assert result.retry_not_before == datetime.fromtimestamp(NOW_MS / 1000 + 30, tz=UTC)
    assert len(transport.requests) == 1


def test_transport_confirmed_not_sent_is_distinct_from_unknown() -> None:
    adapter, transport = make_adapter(TransportNotSent())

    result = adapter.submit_order(make_started_intent())

    assert isinstance(result, SubmissionFailed)
    assert result.failure.kind is BinanceFailureKind.NOT_SENT
    assert result.failure.outcome.value == "NOT_SENT"
    assert result.failure.retry_allowed is True
    assert result.retry_not_before is None
    assert len(transport.requests) == 1


def test_418_is_unknown_and_requires_operator_clearance_without_retry() -> None:
    adapter, transport = make_adapter(response(418, {"code": -1003, "msg": "IP banned: secret"}))

    result = adapter.submit_order(make_started_intent())

    assert isinstance(result, SubmissionFailed)
    assert result.failure.kind is BinanceFailureKind.BANNED
    assert result.failure.outcome.value == "UNKNOWN"
    assert result.failure.operator_clearance_required is True
    assert result.retry_not_before is None
    assert "IP banned" not in repr(result)
    assert len(transport.requests) == 1


def test_ambiguous_503_and_malformed_success_response_remain_unknown() -> None:
    adapter, _ = make_adapter(
        response(503, {"code": -1000, "msg": "Unknown error, check secret token"})
    )
    ambiguous = adapter.submit_order(make_started_intent())
    assert isinstance(ambiguous, SubmissionFailed)
    assert ambiguous.failure.kind is BinanceFailureKind.UNKNOWN
    assert ambiguous.failure.retry_allowed is False
    assert "secret token" not in repr(ambiguous)

    malformed_adapter, _ = make_adapter(response(200, {"status": "NEW"}))
    malformed = malformed_adapter.submit_order(make_started_intent())
    assert isinstance(malformed, SubmissionFailed)
    assert malformed.failure.kind is BinanceFailureKind.UNKNOWN


def test_credentials_are_bound_to_workspace_target_and_redacted() -> None:
    target = BinanceDemoTarget(account_id="demo-account", credential_ref="secret-ref/demo")
    with pytest.raises(ValueError, match="credential_ref"):
        DemoUsdMRestAdapter(
            target=target,
            workspace_id="workspace-1",
            credentials=DemoApiCredentials(
                workspace_id="workspace-1",
                account_id="demo-account",
                credential_ref="other-ref",
                api_key="sensitive-key",
                api_secret="sensitive-secret",
            ),
            transport=FakeTransport(response(200, {})),
            clock_ms=lambda: NOW_MS,
        )
    creds = DemoApiCredentials(
        workspace_id="workspace-1",
        account_id="demo-account",
        credential_ref="secret-ref/demo",
        api_key="sensitive-key",
        api_secret="sensitive-secret",
    )
    assert "sensitive-key" not in repr(creds)
    assert "sensitive-secret" not in repr(creds)


def test_adapter_rejects_wrong_workspace_or_account_before_transport() -> None:
    adapter, transport = make_adapter(response(200, {}))
    wrong_workspace = replace(make_intent(), workspace_id="workspace-other", request_fingerprint="")
    wrong_account = replace(make_intent(), account_id="account-other", request_fingerprint="")
    started_wrong_workspace = wrong_workspace.begin_submission(
        at=wrong_workspace.created_at + timedelta(seconds=1)
    )
    started_wrong_account = wrong_account.begin_submission(
        at=wrong_account.created_at + timedelta(seconds=1)
    )

    with pytest.raises(BinanceAdapterError):
        adapter.submit_order(started_wrong_workspace)
    with pytest.raises(BinanceAdapterError):
        adapter.submit_order(started_wrong_account)
    assert not transport.requests


def test_transport_exception_is_unknown_and_redacted() -> None:
    adapter, transport = make_adapter(RuntimeError("api_secret=test-api-secret"))

    result = adapter.submit_order(make_started_intent())

    assert isinstance(result, SubmissionFailed)
    assert result.failure.kind is BinanceFailureKind.UNKNOWN
    assert "test-api-secret" not in repr(result)
    assert len(transport.requests) == 1


def test_submit_rejects_non_submitting_intent_before_transport() -> None:
    adapter, transport = make_adapter(response(200, {}))

    with pytest.raises(BinanceAdapterError, match="SUBMITTING"):
        adapter.submit_order(make_intent())

    accepted = make_started_intent().resolve_submission(
        outcome=SubmissionOutcome.ACCEPTED,
        observed_at=make_intent().created_at + timedelta(seconds=2),
        venue_order_id="venue-1",
    )
    with pytest.raises(BinanceAdapterError, match="SUBMITTING"):
        adapter.submit_order(accepted)
    assert not transport.requests
