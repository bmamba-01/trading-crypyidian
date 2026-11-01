from trading_bot.adapters.binance_usdm.submission_errors import (
    BinanceFailureKind,
    BinanceResponse,
    classify_submission_failure,
)
from trading_bot.domain.execution.venue_orders import SubmissionOutcome


def test_unknown_503_must_reconcile_before_retry() -> None:
    result = classify_submission_failure(
        BinanceResponse(
            status_code=503,
            error_code=None,
            message="Unknown error, please check your request or try again later.",
        )
    )

    assert result.kind is BinanceFailureKind.UNKNOWN
    assert result.outcome is SubmissionOutcome.UNKNOWN
    assert result.retry_allowed is False


def test_documented_service_unavailable_is_safe_retry_failure() -> None:
    result = classify_submission_failure(BinanceResponse(503, None, "Service Unavailable."))

    assert result.kind is BinanceFailureKind.RETRYABLE_FAILURE
    assert result.outcome is SubmissionOutcome.RETRYABLE_FAILURE
    assert result.retry_allowed is True
    assert result.backoff_required is True


def test_documented_internal_503_is_safe_retry_failure() -> None:
    result = classify_submission_failure(
        BinanceResponse(
            503,
            None,
            "Internal error; unable to process your request. Please try again.",
        )
    )

    assert result.kind is BinanceFailureKind.RETRYABLE_FAILURE
    assert result.retry_allowed is True
    assert result.backoff_required is True


def test_system_throttle_is_retryable_failure() -> None:
    result = classify_submission_failure(
        BinanceResponse(
            503,
            -1008,
            "Request throttled by system-level protection. Please try again.",
        )
    )

    assert result.kind is BinanceFailureKind.RETRYABLE_FAILURE
    assert result.retry_allowed is True
    assert result.backoff_required is True


def test_429_requires_backoff_but_does_not_imply_order_unknown() -> None:
    result = classify_submission_failure(BinanceResponse(429, -1003, "Too many requests."))

    assert result.kind is BinanceFailureKind.RATE_LIMITED
    assert result.outcome is SubmissionOutcome.RETRYABLE_FAILURE
    assert result.retry_allowed is True
    assert result.backoff_required is True


def test_418_disables_retry_until_operator_or_policy_clears_ban() -> None:
    result = classify_submission_failure(BinanceResponse(418, -1003, "IP banned."))

    assert result.kind is BinanceFailureKind.BANNED
    assert result.outcome is SubmissionOutcome.UNKNOWN
    assert result.retry_allowed is False
    assert result.operator_clearance_required is True
    assert result.backoff_required is False


def test_408_after_submit_is_unknown() -> None:
    result = classify_submission_failure(BinanceResponse(408, None, "Request timeout."))

    assert result.kind is BinanceFailureKind.UNKNOWN
    assert result.outcome is SubmissionOutcome.UNKNOWN


def test_unknown_5xx_is_fail_closed() -> None:
    result = classify_submission_failure(BinanceResponse(502, None, "gateway error"))

    assert result.kind is BinanceFailureKind.UNKNOWN
    assert result.outcome is SubmissionOutcome.UNKNOWN
    assert result.retry_allowed is False


def test_unexpected_redirect_is_unknown_and_never_retryable() -> None:
    result = classify_submission_failure(BinanceResponse(302, None, "redirect"))

    assert result.kind is BinanceFailureKind.UNKNOWN
    assert result.outcome is SubmissionOutcome.UNKNOWN
    assert result.retry_allowed is False


def test_definitive_4xx_rejection_is_not_retryable() -> None:
    result = classify_submission_failure(BinanceResponse(400, -1121, "Invalid symbol."))

    assert result.kind is BinanceFailureKind.DEFINITIVE_REJECTION
    assert result.outcome is SubmissionOutcome.DEFINITIVE_REJECTION
    assert result.retry_allowed is False


def test_failure_before_network_send_can_be_retried() -> None:
    result = classify_submission_failure(None, request_was_sent=False)

    assert result.kind is BinanceFailureKind.NOT_SENT
    assert result.outcome is SubmissionOutcome.NOT_SENT
    assert result.retry_allowed is True


def test_response_text_is_redacted_from_result() -> None:
    result = classify_submission_failure(
        BinanceResponse(400, -1121, "Invalid symbol. apiKey=secret-value")
    )

    assert "secret-value" not in repr(result)
