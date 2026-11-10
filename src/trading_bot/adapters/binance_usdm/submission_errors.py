"""Phân loại lỗi gửi lệnh theo semantics USDⓈ-M, không thực hiện retry."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from trading_bot.domain.execution.venue_orders import SubmissionOutcome


class BinanceFailureKind(StrEnum):
    """Phân loại an toàn cho kết quả gửi request Binance."""

    NOT_SENT = "NOT_SENT"
    DEFINITIVE_REJECTION = "DEFINITIVE_REJECTION"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    RATE_LIMITED = "RATE_LIMITED"
    BANNED = "BANNED"
    ROUTING_MISMATCH = "ROUTING_MISMATCH"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BinanceResponse:
    """Phần response lỗi tối thiểu; message thô không được đưa vào kết quả."""

    status_code: int
    error_code: int | None
    message: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("status_code phải là mã HTTP hợp lệ.")
        if self.error_code is not None and type(self.error_code) is not int:
            raise ValueError("error_code phải là số nguyên hoặc None.")
        if not isinstance(self.message, str):
            raise ValueError("message phải là chuỗi.")


@dataclass(frozen=True, slots=True)
class SubmissionFailure:
    """Kết quả thuần để application giữ UNKNOWN hoặc lên lịch xử lý phù hợp."""

    kind: BinanceFailureKind
    outcome: SubmissionOutcome
    retry_allowed: bool
    backoff_required: bool = False
    operator_clearance_required: bool = False


def classify_submission_failure(
    response: BinanceResponse | None,
    *,
    request_was_sent: bool = True,
) -> SubmissionFailure:
    """Phân loại lỗi. Hàm không retry và không giữ lại response message thô."""

    if type(request_was_sent) is not bool:
        raise ValueError("request_was_sent phải là boolean.")
    if not request_was_sent:
        if response is not None:
            raise ValueError("Không thể vừa có response vừa khẳng định request chưa gửi.")
        return SubmissionFailure(
            BinanceFailureKind.NOT_SENT,
            SubmissionOutcome.NOT_SENT,
            True,
        )
    if response is None:
        return SubmissionFailure(
            BinanceFailureKind.UNKNOWN,
            SubmissionOutcome.UNKNOWN,
            False,
        )

    if response.error_code == -4120:
        # Sai họ endpoint không chứng minh order vắng mặt hoặc reservation được release.
        return SubmissionFailure(
            BinanceFailureKind.ROUTING_MISMATCH,
            SubmissionOutcome.UNKNOWN,
            False,
            operator_clearance_required=True,
        )
    message = response.message.strip().casefold()
    if response.status_code == 418:
        return SubmissionFailure(
            BinanceFailureKind.BANNED,
            SubmissionOutcome.UNKNOWN,
            False,
            operator_clearance_required=True,
        )
    if response.status_code == 429:
        return SubmissionFailure(
            BinanceFailureKind.RATE_LIMITED,
            SubmissionOutcome.RETRYABLE_FAILURE,
            True,
            backoff_required=True,
        )
    if response.status_code == 408:
        return SubmissionFailure(
            BinanceFailureKind.UNKNOWN,
            SubmissionOutcome.UNKNOWN,
            False,
        )
    if response.status_code == 503:
        if response.error_code == -1008 or message in {
            "service unavailable.",
            "internal error; unable to process your request. please try again.",
        }:
            return SubmissionFailure(
                BinanceFailureKind.RETRYABLE_FAILURE,
                SubmissionOutcome.RETRYABLE_FAILURE,
                True,
                backoff_required=True,
            )
        return SubmissionFailure(
            BinanceFailureKind.UNKNOWN,
            SubmissionOutcome.UNKNOWN,
            False,
        )
    if 500 <= response.status_code <= 599:
        return SubmissionFailure(
            BinanceFailureKind.UNKNOWN,
            SubmissionOutcome.UNKNOWN,
            False,
        )
    if 100 <= response.status_code <= 399:
        return SubmissionFailure(
            BinanceFailureKind.UNKNOWN,
            SubmissionOutcome.UNKNOWN,
            False,
        )
    if 400 <= response.status_code <= 499:
        return SubmissionFailure(
            BinanceFailureKind.DEFINITIVE_REJECTION,
            SubmissionOutcome.DEFINITIVE_REJECTION,
            False,
        )
    raise ValueError("Chỉ phân loại response lỗi có HTTP status từ 400 trở lên.")
