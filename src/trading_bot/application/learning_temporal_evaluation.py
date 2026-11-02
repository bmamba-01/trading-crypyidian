"""Chọn mẫu cho một fold walk-forward theo lineage và cutoff point-in-time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from trading_bot.domain.learning.evaluation import (
    SampleLineage,
    TemporalSampleSelection,
    WalkForwardSplit,
    select_temporal_samples,
)

SampleRole = Literal["train", "validation", "test"]


@dataclass(frozen=True, slots=True)
class FoldSampleExclusion:
    """Lý do một mẫu trong cửa sổ fold bị loại khỏi vai trò tương ứng."""

    role: SampleRole
    sample_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WalkForwardSampleSelection:
    """Các ID đủ điều kiện và loại trừ có lý do của một fold."""

    train_sample_ids: tuple[str, ...]
    validation_sample_ids: tuple[str, ...]
    test_sample_ids: tuple[str, ...]
    exclusions: tuple[FoldSampleExclusion, ...]

    @property
    def excluded_sample_ids(self) -> tuple[str, ...]:
        """ID loại trừ theo thứ tự ổn định, không lặp."""
        return tuple(sorted({item.sample_id for item in self.exclusions}))


def _inside(sample: SampleLineage, *, start: datetime, end: datetime) -> bool:
    """Cửa sổ quyết định là nửa mở để sample thuộc tối đa một vai trò."""
    return start <= sample.decision_at < end


def _exclusions(
    role: SampleRole, result: TemporalSampleSelection
) -> tuple[FoldSampleExclusion, ...]:
    return tuple(
        FoldSampleExclusion(
            role=role,
            sample_id=item.sample_id,
            reasons=tuple(reason.value for reason in item.reasons),
        )
        for item in result.exclusions
    )


def select_walk_forward_samples(
    samples: tuple[SampleLineage, ...],
    *,
    split: WalkForwardSplit,
    evaluation_cutoff: datetime,
) -> WalkForwardSampleSelection:
    """Áp dụng cửa sổ, availability, cutoff nhãn và purge liên fold.

    Mẫu train phải có nhãn hoàn tất trước validation; validation phải hoàn tất
    trước test. Nhãn train bị purge nếu giao với bất kỳ nhãn validation/test
    nào; nhãn validation bị purge nếu giao với nhãn test. Feature lookback lịch
    sử dùng chung vẫn hợp lệ khi đã available tại decision của từng sample.
    """
    if not isinstance(split, WalkForwardSplit):
        raise ValueError("split phải là WalkForwardSplit.")
    if not (split.train.end <= split.validation.start and split.validation.end <= split.test.start):
        raise ValueError(
            "Các cửa sổ fold phải theo thứ tự train, validation, test và không giao nhau."
        )
    if len({sample.sample_id for sample in samples if isinstance(sample, SampleLineage)}) != len(
        samples
    ):
        raise ValueError("sample_id bị trùng trong dataset.")
    if any(not isinstance(sample, SampleLineage) for sample in samples):
        raise ValueError("Mọi mẫu phải có SampleLineage đầy đủ.")

    train = tuple(
        sample
        for sample in samples
        if _inside(sample, start=split.train.start, end=split.train.end)
    )
    validation = tuple(
        sample
        for sample in samples
        if _inside(sample, start=split.validation.start, end=split.validation.end)
    )
    test = tuple(
        sample for sample in samples if _inside(sample, start=split.test.start, end=split.test.end)
    )

    train_result = select_temporal_samples(
        train,
        evaluation_cutoff=split.validation.start,
        protected_samples=(*validation, *test),
    )
    validation_result = select_temporal_samples(
        validation,
        evaluation_cutoff=split.test.start,
        protected_samples=test,
    )
    test_result = select_temporal_samples(test, evaluation_cutoff=evaluation_cutoff)

    excluded = (
        *_exclusions("train", train_result),
        *_exclusions("validation", validation_result),
        *_exclusions("test", test_result),
    )
    return WalkForwardSampleSelection(
        train_sample_ids=train_result.eligible_sample_ids,
        validation_sample_ids=validation_result.eligible_sample_ids,
        test_sample_ids=test_result.eligible_sample_ids,
        exclusions=tuple(sorted(excluded, key=lambda item: (item.sample_id, item.role))),
    )
