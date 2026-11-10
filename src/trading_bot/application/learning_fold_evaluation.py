"""Đánh giá kết quả walk-forward theo manifest và fold plan đã ghim.

Thời điểm mở giao dịch thuộc khoảng nửa mở `[outcome_start, outcome_end)`;
thời điểm đóng được phép bằng đúng `outcome_end`, mốc hoàn tất nhãn.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from trading_bot.application.learning_fold_plan import (
    ManifestFoldPlan,
    ManifestFoldSelection,
    build_manifest_fold_plan,
)
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample
from trading_bot.domain.learning.evaluation import (
    EvaluationMetrics,
    EvaluationTrade,
    build_evaluation_metrics,
)

FoldRole = Literal["train", "validation", "test"]
FOLD_ROLES: tuple[FoldRole, ...] = ("train", "validation", "test")
EVALUATOR_VERSION = "wp06-fold-evaluator-v1"
OUTCOME_ROW_VERSION = "wp06-outcome-row-v1"
TRADE_ROW_VERSION = "wp06-trade-row-v1"
WP02_LIMITATIONS_VI = (
    "WP-02 phản ánh lãi/lỗ đã thực hiện; chưa định giá lại vị thế đang mở theo giá thị trường, "
    "cách tính cửa sổ rủi ro ngày/cuốn chiếu chưa hoàn chỉnh và giả định phí cấp vốn/chênh "
    "giá còn giới hạn. Tỷ lệ chỉ đếm kết quả của sample đã chọn, không chứng minh danh sách "
    "mẫu đầu vào đầy đủ. Khoảng bất định từ lấy mẫu theo khối mô tả mẫu này, không chứng minh "
    "lợi nhuận tương lai."
)


@dataclass(frozen=True, slots=True)
class FoldOutcomeExpectation:
    """Liên kết sample tới row trade bằng UUID row ID và hash outcome đã lưu."""

    sample_id: str
    role: FoldRole
    outcome_id: str
    outcome_hash: str
    outcome_start: datetime
    outcome_end: datetime


@dataclass(frozen=True, slots=True)
class FoldOutcomeVerificationRequest:
    """Phạm vi chính xác mà bộ xác minh phải đọc từ kho dữ liệu authoritative."""

    workspace_id: UUID
    manifest_hash: str
    split_policy_version: str
    fold_plan: ManifestFoldPlan
    fold_plan_hash: str
    fold_id: str
    expected_outcomes: tuple[FoldOutcomeExpectation, ...]
    access_receipt_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PersistedFoldTrade:
    """Bản ghi giao dịch đã lưu, kèm thứ tự và mã băm của bản ghi."""

    ordinal: int
    trade: EvaluationTrade
    row_hash: str


@dataclass(frozen=True, slots=True)
class PersistedFoldOutcome:
    """Bản ghi kết quả đã lưu; trạng thái hoàn tất lấy từ kho dữ liệu."""

    sample_id: str
    role: FoldRole
    outcome_id: str
    outcome_hash: str
    status: str
    trade_count: int
    row_hash: str
    trades: tuple[PersistedFoldTrade, ...]


@dataclass(frozen=True, slots=True)
class FoldOutcomeVerificationBatch:
    """Các kết quả đọc trong một snapshot, được bộ xác minh ràng buộc vào request."""

    workspace_id: UUID
    manifest_hash: str
    split_policy_version: str
    fold_plan_hash: str
    fold_id: str
    outcomes: tuple[PersistedFoldOutcome, ...]
    batch_hash: str


class FoldOutcomeVerifier(Protocol):
    """Cổng chỉ đọc tới các bản ghi kết quả và giao dịch đã lưu.

    Bộ chuyển đổi triển khai phải đọc trong một snapshot nhất quán, chỉ nhận kết
    quả đã hoàn tất, đối chiếu chính xác workspace/manifest/plan/fold/sample,
    kiểm mã băm bản ghi chuẩn khớp giá trị đã lưu trong DB và xác nhận thứ tự
    giao dịch liên tục từ 0 tới `trade_count - 1`. Adapter PostgreSQL yêu cầu
    transaction `REPEATABLE READ` hoặc `SERIALIZABLE`, `READ WRITE` để ghi receipt
    trong cùng snapshot trước khi payload được trả về.
    """

    async def load_verified_fold_outcomes(
        self,
        request: FoldOutcomeVerificationRequest,
    ) -> FoldOutcomeVerificationBatch: ...


class SourceRightsEligibilityGate(Protocol):
    """Kiểm tra quyền nguồn còn hiệu lực trước khi evaluator đọc outcome."""

    async def assert_eligible(self, *, workspace_id: UUID, manifest: DatasetManifest) -> None: ...


@dataclass(frozen=True, slots=True)
class FoldRoleMetrics:
    """Chỉ số và nguồn gốc của các mẫu đủ điều kiện trong một vai trò."""

    role: FoldRole
    sample_ids: tuple[str, ...]
    trade_lineage_hash: str
    metrics: EvaluationMetrics


@dataclass(frozen=True, slots=True)
class FoldEvaluation:
    """Báo cáo một fold; cấu trúc chỉ chứa train, validation và test."""

    fold_id: str
    train: FoldRoleMetrics
    validation: FoldRoleMetrics
    test: FoldRoleMetrics
    exclusions: tuple[tuple[str, str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class WalkForwardEvaluationReport:
    """Báo cáo bất biến gắn manifest, split policy, plan và kết quả từng fold."""

    manifest_hash: str
    fold_plan_hash: str
    split_policy_version: str
    evaluation_cutoff: datetime
    seed: int
    fold_evaluations: tuple[FoldEvaluation, ...]
    candidate_completeness_verified: Literal[False]
    report_hash: str
    caveat_vi: str

    @property
    def canonical_json(self) -> str:
        """Payload chuẩn đã dùng để tạo report_hash, không gồm chính mã băm."""
        return _canonical_report_json(self)


def _sample_role_ids(fold: ManifestFoldSelection) -> dict[str, FoldRole]:
    result: dict[str, FoldRole] = {}
    for role in FOLD_ROLES:
        sample_ids = getattr(fold, f"{role}_sample_ids")
        if type(sample_ids) is not tuple or any(
            not isinstance(sample_id, str) or not sample_id.strip() for sample_id in sample_ids
        ):
            raise ValueError("Fold plan chứa membership không hợp lệ.")
        for sample_id in sample_ids:
            if sample_id in result:
                raise ValueError("Fold plan gán sample_id trùng giữa các vai trò.")
            result[sample_id] = role
    return result


def _validate_plan(
    manifest: DatasetManifest,
    plan: ManifestFoldPlan,
) -> ManifestFoldPlan:
    if type(manifest) is not DatasetManifest:
        raise ValueError("manifest phải là DatasetManifest authoritative.")
    if type(plan) is not ManifestFoldPlan:
        raise ValueError("fold_plan phải là ManifestFoldPlan authoritative.")
    if plan.manifest_hash != manifest.content_hash:
        raise ValueError("Fold plan manifest hash không khớp manifest đầu vào.")
    if plan.split_policy_version != manifest.split_policy_version:
        raise ValueError("Fold plan split policy không khớp manifest.")
    if type(plan.folds) is not tuple or not plan.folds:
        raise ValueError("Fold plan phải có ít nhất một fold.")
    if any(type(fold) is not ManifestFoldSelection for fold in plan.folds):
        raise ValueError("Fold plan chứa selection không hợp lệ.")

    canonical = build_manifest_fold_plan(
        manifest,
        folds=tuple(fold.split for fold in plan.folds),
        expected_split_policy_version=plan.split_policy_version,
        evaluation_cutoff=plan.evaluation_cutoff,
    )
    if canonical.content_hash != plan.content_hash:
        raise ValueError("fold plan hash không khớp selection được tái dựng.")
    if canonical != plan:
        raise ValueError("Fold plan selection không khớp manifest authoritative.")
    return canonical


def _derived_seed(seed: int, fold_id: str, role: FoldRole) -> int:
    material = f"{seed}:{fold_id}:{role}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:4], "big")


def _validate_trade_horizon(
    trade: EvaluationTrade,
    *,
    sample: DatasetSample,
) -> None:
    if (
        trade.opened_at < sample.outcome_start
        or trade.opened_at >= sample.outcome_end
        or trade.closed_at > sample.outcome_end
    ):
        raise ValueError(
            f"Trade {trade.trade_id} nằm ngoài outcome interval của sample {sample.sample_id}."
        )


def _trade_payload(trade: EvaluationTrade) -> dict[str, str]:
    return {
        "trade_id": trade.trade_id,
        "opened_at": trade.opened_at.isoformat(),
        "closed_at": trade.closed_at.isoformat(),
        "gross_pnl": str(trade.gross_pnl),
        "fees": str(trade.fees),
        "funding": str(trade.funding),
        "slippage": str(trade.slippage),
        "exposure": str(trade.exposure),
    }


def _trade_lineage_hash(
    sample_trades: tuple[tuple[str, EvaluationTrade], ...],
) -> str:
    payload = [
        {"sample_id": sample_id, "trade": _trade_payload(trade)}
        for sample_id, trade in sample_trades
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _sha256_hex(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_hash(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def persisted_trade_row_hash(
    request: FoldOutcomeVerificationRequest,
    outcome: PersistedFoldOutcome,
    row: PersistedFoldTrade,
) -> str:
    return _canonical_hash(
        {
            "row_version": TRADE_ROW_VERSION,
            "workspace_id": str(request.workspace_id),
            "sample_id": outcome.sample_id,
            "outcome_id": outcome.outcome_id,
            "ordinal": row.ordinal,
            "trade": _trade_payload(row.trade),
        }
    )


def persisted_outcome_row_hash(
    request: FoldOutcomeVerificationRequest,
    outcome: PersistedFoldOutcome,
    trades: tuple[PersistedFoldTrade, ...],
) -> str:
    return _canonical_hash(
        {
            "row_version": OUTCOME_ROW_VERSION,
            "workspace_id": str(request.workspace_id),
            "sample_id": outcome.sample_id,
            "role": outcome.role,
            "outcome_id": outcome.outcome_id,
            "outcome_hash": outcome.outcome_hash,
            "status": outcome.status,
            "trade_count": outcome.trade_count,
            "trade_row_hashes": [trade.row_hash for trade in trades],
        }
    )


def verification_batch_hash(
    request: FoldOutcomeVerificationRequest,
    outcomes: tuple[PersistedFoldOutcome, ...],
) -> str:
    return _canonical_hash(
        {
            "workspace_id": str(request.workspace_id),
            "manifest_hash": request.manifest_hash,
            "split_policy_version": request.split_policy_version,
            "fold_plan_hash": request.fold_plan_hash,
            "fold_id": request.fold_id,
            "outcome_row_hashes": [
                (outcome.sample_id, outcome.row_hash)
                for outcome in sorted(outcomes, key=lambda item: item.sample_id)
            ],
        }
    )


def _validate_verification_batch(
    request: FoldOutcomeVerificationRequest,
    batch: FoldOutcomeVerificationBatch,
) -> dict[str, tuple[EvaluationTrade, ...]]:
    if type(batch) is not FoldOutcomeVerificationBatch:
        raise ValueError("Verifier phải trả FoldOutcomeVerificationBatch authoritative.")
    if (
        batch.workspace_id != request.workspace_id
        or batch.manifest_hash != request.manifest_hash
        or batch.split_policy_version != request.split_policy_version
        or batch.fold_plan_hash != request.fold_plan_hash
        or batch.fold_id != request.fold_id
    ):
        raise ValueError("Verifier batch không khớp workspace/manifest/plan/fold được yêu cầu.")
    if type(batch.outcomes) is not tuple or any(
        type(item) is not PersistedFoldOutcome for item in batch.outcomes
    ):
        raise ValueError("Verifier batch chứa outcome row không hợp lệ.")

    expected = {item.sample_id: item for item in request.expected_outcomes}
    if len(expected) != len(request.expected_outcomes):
        raise ValueError("Verifier request có sample membership trùng.")
    records: dict[str, PersistedFoldOutcome] = {}
    normalized_trades: dict[str, tuple[EvaluationTrade, ...]] = {}
    for outcome in batch.outcomes:
        expectation = expected.get(outcome.sample_id)
        if expectation is None or outcome.sample_id in records:
            raise ValueError("Verifier trả sample ngoài scope hoặc trùng membership.")
        if (
            outcome.role != expectation.role
            or outcome.outcome_id != expectation.outcome_id
            or outcome.outcome_hash != expectation.outcome_hash
        ):
            raise ValueError("Persisted outcome identity/role/hash không khớp manifest.")
        if outcome.status != "COMPLETED":
            raise ValueError("Persisted outcome chưa COMPLETED; từ chối report fail-closed.")
        if type(outcome.trade_count) is not int or outcome.trade_count < 0:
            raise ValueError("Persisted outcome trade_count không hợp lệ.")
        if outcome.trade_count > 1:
            raise ValueError("Lineage outcome chỉ cho phép tối đa một trade trên mỗi sample.")
        if type(outcome.trades) is not tuple or any(
            type(row) is not PersistedFoldTrade for row in outcome.trades
        ):
            raise ValueError("Persisted outcome chứa trade row không hợp lệ.")
        if any(type(row.ordinal) is not int for row in outcome.trades):
            raise ValueError("Persisted trade ordinal không hợp lệ.")
        if any(type(row.trade) is not EvaluationTrade for row in outcome.trades):
            raise ValueError("Persisted trade row có trade payload không hợp lệ.")
        if len(outcome.trades) != outcome.trade_count:
            raise ValueError("Persisted outcome thiếu hoặc thừa trade row.")
        trades = tuple(sorted(outcome.trades, key=lambda item: item.ordinal))
        if tuple(row.ordinal for row in trades) != tuple(range(outcome.trade_count)):
            raise ValueError("Ordinal của persisted trade row không liên tục từ 0.")
        for row in trades:
            if not _sha256_hex(row.row_hash):
                raise ValueError("Persisted trade row có ordinal hoặc row hash không hợp lệ.")
            if row.row_hash != persisted_trade_row_hash(request, outcome, row):
                raise ValueError("Persisted trade row hash không khớp payload authoritative.")
        if not _sha256_hex(outcome.row_hash):
            raise ValueError("Persisted outcome row hash không hợp lệ.")
        if outcome.row_hash != persisted_outcome_row_hash(request, outcome, trades):
            raise ValueError("Persisted outcome row hash không khớp payload authoritative.")
        records[outcome.sample_id] = outcome
        normalized_trades[outcome.sample_id] = tuple(row.trade for row in trades)

    if set(records) != set(expected):
        raise ValueError("Verifier thiếu persisted outcome cho sample đã chọn.")
    ordered_records = tuple(records[sample_id] for sample_id in sorted(records))
    if batch.batch_hash != verification_batch_hash(request, ordered_records):
        raise ValueError("Verifier batch hash không khớp tập persisted outcome rows.")
    return normalized_trades


def _metrics_payload(metrics: EvaluationMetrics) -> dict[str, object]:
    return {
        "observation_count": metrics.observation_count,
        "usable_observation_count": metrics.usable_observation_count,
        "trade_count": metrics.trade_count,
        "coverage": metrics.coverage,
        "trade_frequency": metrics.trade_frequency,
        "gross_pnl": str(metrics.gross_pnl),
        "net_pnl": str(metrics.net_pnl),
        "fees": str(metrics.fees),
        "funding": str(metrics.funding),
        "slippage": str(metrics.slippage),
        "expectancy": str(metrics.expectancy),
        "max_drawdown": str(metrics.max_drawdown),
        "average_exposure": str(metrics.average_exposure),
        "confidence_interval": (
            [str(value) for value in metrics.confidence_interval]
            if metrics.confidence_interval is not None
            else None
        ),
        "caveat_vi": metrics.caveat_vi,
    }


def _role_payload(role_report: FoldRoleMetrics) -> dict[str, object]:
    return {
        "role": role_report.role,
        "sample_ids": role_report.sample_ids,
        "trade_lineage_hash": role_report.trade_lineage_hash,
        "metrics": _metrics_payload(role_report.metrics),
    }


def _canonical_report_json(report: WalkForwardEvaluationReport) -> str:
    payload = {
        "evaluation_mode": "diagnostic",
        "evaluator_version": EVALUATOR_VERSION,
        "manifest_hash": report.manifest_hash,
        "fold_plan_hash": report.fold_plan_hash,
        "split_policy_version": report.split_policy_version,
        "evaluation_cutoff": report.evaluation_cutoff.isoformat(),
        "seed": report.seed,
        "candidate_completeness_verified": report.candidate_completeness_verified,
        "caveat_vi": report.caveat_vi,
        "folds": [
            {
                "fold_id": fold.fold_id,
                "train": _role_payload(fold.train),
                "validation": _role_payload(fold.validation),
                "test": _role_payload(fold.test),
                "exclusions": fold.exclusions,
            }
            for fold in report.fold_evaluations
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def walk_forward_evaluation_report_from_canonical_json(
    canonical_json: str,
    *,
    expected_report_hash: str,
) -> WalkForwardEvaluationReport:
    """Tái dựng report đã lưu sau khi xác minh canonical bytes và mã băm."""
    if not isinstance(canonical_json, str) or not _sha256_hex(expected_report_hash):
        raise ValueError("Canonical report hoặc report hash không hợp lệ.")
    if hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() != expected_report_hash:
        raise ValueError("Report hash không khớp canonical bytes.")
    try:
        payload = json.loads(canonical_json)
        if (
            not isinstance(payload, dict)
            or json.dumps(payload, sort_keys=True, separators=(",", ":")) != canonical_json
            or payload.get("evaluation_mode") != "diagnostic"
            or payload.get("evaluator_version") != EVALUATOR_VERSION
            or payload.get("candidate_completeness_verified") is not False
            or not isinstance(payload.get("folds"), list)
        ):
            raise ValueError("Report canonical không đúng contract diagnostic.")
        folds = tuple(_fold_from_payload(item) for item in payload["folds"])
        report = WalkForwardEvaluationReport(
            manifest_hash=payload["manifest_hash"],
            fold_plan_hash=payload["fold_plan_hash"],
            split_policy_version=payload["split_policy_version"],
            evaluation_cutoff=datetime.fromisoformat(payload["evaluation_cutoff"]),
            seed=payload["seed"],
            fold_evaluations=folds,
            candidate_completeness_verified=False,
            report_hash=expected_report_hash,
            caveat_vi=payload["caveat_vi"],
        )
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        raise ValueError("Canonical report diagnostic không thể tái dựng an toàn.") from None
    if report.canonical_json != canonical_json:
        raise ValueError("Report tái dựng không khớp canonical bytes.")
    return report


def _fold_from_payload(payload: object) -> FoldEvaluation:
    if not isinstance(payload, dict):
        raise ValueError("Fold report không phải object.")
    exclusions_payload = payload["exclusions"]
    if not isinstance(exclusions_payload, list):
        raise ValueError("Danh sách exclusion không hợp lệ.")
    exclusions: list[tuple[str, str, tuple[str, ...]]] = []
    for item in exclusions_payload:
        if (
            not isinstance(item, list)
            or len(item) != 3
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            or not isinstance(item[2], list)
            or any(not isinstance(reason, str) for reason in item[2])
        ):
            raise ValueError("Một exclusion trong report không hợp lệ.")
        exclusions.append((item[0], item[1], tuple(item[2])))
    return FoldEvaluation(
        fold_id=_required_text(payload["fold_id"]),
        train=_role_from_payload(payload["train"], expected_role="train"),
        validation=_role_from_payload(payload["validation"], expected_role="validation"),
        test=_role_from_payload(payload["test"], expected_role="test"),
        exclusions=tuple(exclusions),
    )


def _role_from_payload(payload: object, *, expected_role: FoldRole) -> FoldRoleMetrics:
    if not isinstance(payload, dict) or payload.get("role") != expected_role:
        raise ValueError("Role trong report không khớp vị trí.")
    sample_ids = payload.get("sample_ids")
    if not isinstance(sample_ids, list) or any(not isinstance(item, str) for item in sample_ids):
        raise ValueError("Danh sách sample trong report không hợp lệ.")
    metrics_payload = payload.get("metrics")
    if not isinstance(metrics_payload, dict):
        raise ValueError("Metrics trong report không hợp lệ.")
    confidence = metrics_payload.get("confidence_interval")
    if confidence is not None and (not isinstance(confidence, list) or len(confidence) != 2):
        raise ValueError("Khoảng tin cậy report không hợp lệ.")
    coverage = metrics_payload["coverage"]
    frequency = metrics_payload["trade_frequency"]
    if (
        isinstance(coverage, bool)
        or not isinstance(coverage, (int, float))
        or not math.isfinite(float(coverage))
        or isinstance(frequency, bool)
        or not isinstance(frequency, (int, float))
        or not math.isfinite(float(frequency))
    ):
        raise ValueError("Tỷ lệ coverage/frequency report không hợp lệ.")
    counts = tuple(
        metrics_payload[name]
        for name in ("observation_count", "usable_observation_count", "trade_count")
    )
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError("Số đếm metrics report không hợp lệ.")
    caveat = metrics_payload["caveat_vi"]
    if not isinstance(caveat, str):
        raise ValueError("Giới hạn metrics report không hợp lệ.")
    metrics = EvaluationMetrics(
        observation_count=counts[0],
        usable_observation_count=counts[1],
        trade_count=counts[2],
        coverage=float(coverage),
        trade_frequency=float(frequency),
        gross_pnl=Decimal(metrics_payload["gross_pnl"]),
        net_pnl=Decimal(metrics_payload["net_pnl"]),
        fees=Decimal(metrics_payload["fees"]),
        funding=Decimal(metrics_payload["funding"]),
        slippage=Decimal(metrics_payload["slippage"]),
        expectancy=Decimal(metrics_payload["expectancy"]),
        max_drawdown=Decimal(metrics_payload["max_drawdown"]),
        average_exposure=Decimal(metrics_payload["average_exposure"]),
        confidence_interval=(
            (Decimal(confidence[0]), Decimal(confidence[1])) if confidence is not None else None
        ),
        caveat_vi=caveat,
    )
    return FoldRoleMetrics(
        role=expected_role,
        sample_ids=tuple(sample_ids),
        trade_lineage_hash=_required_text(payload["trade_lineage_hash"]),
        metrics=metrics,
    )


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Report cần chuỗi không rỗng.")
    return value


async def build_walk_forward_evaluation_report(
    manifest: DatasetManifest,
    fold_plan: ManifestFoldPlan,
    *,
    verifier: FoldOutcomeVerifier,
    seed: int,
    source_eligibility_gate: SourceRightsEligibilityGate | None = None,
    access_receipt_id: UUID | None = None,
) -> WalkForwardEvaluationReport:
    """Đọc outcome đã persist qua verifier rồi lập report train/validation/test.

    Mỗi request ghim đúng workspace, manifest, fold plan và sample membership. Chỉ
    lập báo cáo khi adapter đọc snapshot chuẩn, trạng thái outcome là COMPLETED và
    mã băm/bậc bản ghi giao dịch khớp. Adapter PostgreSQL đã có nhưng chưa được nối
    vào composition root. Độ đầy đủ của danh sách mẫu chưa được chứng minh; hàm
    không chọn ngưỡng/nhóm tham số và không truy cập holdout.
    """
    if type(seed) is not int or seed < 0:
        raise ValueError("seed phải là số nguyên không âm.")
    if any(sample.source_dependent for sample in manifest.samples):
        if source_eligibility_gate is None:
            raise ValueError(
                "Source-dependent manifest requires a live source-rights eligibility gate."
            )
        try:
            await source_eligibility_gate.assert_eligible(
                workspace_id=manifest.workspace_id,
                manifest=manifest,
            )
        except Exception:
            raise ValueError(
                "Live source-rights eligibility gate failed; refuse evaluation."
            ) from None
    canonical_plan = _validate_plan(manifest, fold_plan)
    samples_by_id = {sample.sample_id: sample for sample in manifest.samples}
    fold_evaluations: list[FoldEvaluation] = []
    for fold in canonical_plan.folds:
        expected_roles = _sample_role_ids(fold)
        expectations = tuple(
            FoldOutcomeExpectation(
                sample_id=sample_id,
                role=expected_roles[sample_id],
                outcome_id=samples_by_id[sample_id].outcome_id,
                outcome_hash=samples_by_id[sample_id].outcome_hash,
                outcome_start=samples_by_id[sample_id].outcome_start,
                outcome_end=samples_by_id[sample_id].outcome_end,
            )
            for sample_id in sorted(expected_roles)
        )
        request = FoldOutcomeVerificationRequest(
            workspace_id=manifest.workspace_id,
            manifest_hash=manifest.content_hash,
            split_policy_version=canonical_plan.split_policy_version,
            fold_plan=canonical_plan,
            fold_plan_hash=canonical_plan.content_hash,
            fold_id=fold.fold_id,
            expected_outcomes=expectations,
            access_receipt_id=access_receipt_id,
        )
        try:
            verified_batch = await verifier.load_verified_fold_outcomes(request)
        except Exception:
            raise ValueError("Fold outcome verifier thất bại; từ chối tạo report.") from None
        trades_by_sample = _validate_verification_batch(request, verified_batch)

        sample_trades_by_id: dict[str, tuple[EvaluationTrade, ...]] = {}
        for expectation in expectations:
            sample = samples_by_id[expectation.sample_id]
            trades = trades_by_sample[expectation.sample_id]
            for trade in trades:
                _validate_trade_horizon(trade, sample=sample)
            sample_trades_by_id[expectation.sample_id] = trades

        trade_ids: set[str] = set()
        role_reports: dict[FoldRole, FoldRoleMetrics] = {}
        for role in FOLD_ROLES:
            sample_ids = tuple(
                sorted(
                    sample_id
                    for sample_id, assigned_role in expected_roles.items()
                    if role == assigned_role
                )
            )
            sample_trades = tuple(
                (sample_id, trade)
                for sample_id in sample_ids
                for trade in sample_trades_by_id[sample_id]
            )
            trades = tuple(
                sorted(
                    (trade for _, trade in sample_trades),
                    key=lambda item: (item.closed_at, item.trade_id),
                )
            )
            for trade in trades:
                if trade.trade_id in trade_ids:
                    raise ValueError("trade_id bị lặp trong cùng fold.")
                trade_ids.add(trade.trade_id)
            metrics = build_evaluation_metrics(
                trades,
                observation_count=len(sample_ids),
                usable_observation_count=len(sample_ids),
                seed=_derived_seed(seed, fold.fold_id, role),
            )
            role_reports[role] = FoldRoleMetrics(
                role=role,
                sample_ids=sample_ids,
                trade_lineage_hash=_trade_lineage_hash(sample_trades),
                metrics=metrics,
            )

        exclusions = tuple(
            sorted((item.role, item.sample_id, item.reasons) for item in fold.exclusions)
        )
        fold_evaluations.append(
            FoldEvaluation(
                fold_id=fold.fold_id,
                train=role_reports["train"],
                validation=role_reports["validation"],
                test=role_reports["test"],
                exclusions=exclusions,
            )
        )

    report = WalkForwardEvaluationReport(
        manifest_hash=manifest.content_hash,
        fold_plan_hash=canonical_plan.content_hash,
        split_policy_version=canonical_plan.split_policy_version,
        evaluation_cutoff=canonical_plan.evaluation_cutoff,
        seed=seed,
        fold_evaluations=tuple(fold_evaluations),
        candidate_completeness_verified=False,
        report_hash="",
        caveat_vi=WP02_LIMITATIONS_VI,
    )
    return replace(report, report_hash=hashlib.sha256(report.canonical_json.encode()).hexdigest())
