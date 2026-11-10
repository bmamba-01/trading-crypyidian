"""Trang read-only tiếng Việt cho quan sát và drift metadata WP06."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from trading_bot.admin.web_router import _templates
from trading_bot.application.learning_observations_admin import (
    LearningObservationsAdminService,
)
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity

router = APIRouter(tags=["admin web"])

_KIND_TITLES = {
    "observation": "Quan sát gần đây",
    "assessment": "Đánh giá chất lượng dữ liệu",
    "proposal": "Đề xuất xem xét huấn luyện lại",
}
_OBSERVATION_REASONS = {
    "UNCALIBRATED_SCORE": "Mô hình chưa được hiệu chuẩn",
    "FEATURE_SCHEMA_MISMATCH": "Lược đồ dữ liệu không khớp",
    "SOURCE_RIGHTS_INELIGIBLE": "Chưa đủ quyền sử dụng nguồn dữ liệu",
    "MODEL_UNAVAILABLE": "Mô hình chưa sẵn sàng",
    "MISSING_EVIDENCE": "Thiếu bằng chứng cần thiết",
}
_DRIFT_REASONS = {
    "baseline_missing": "Thiếu quyết định đối chiếu",
    "coverage_below_threshold": "Độ bao phủ dưới ngưỡng",
    "low_sample": "Số lượng quan sát còn thấp",
    "performance_not_evaluable": "Chưa đủ dữ liệu để đánh giá hiệu năng",
    "quality_below_threshold": "Chất lượng dữ liệu dưới ngưỡng",
    "rights_unverified": "Quyền sử dụng dữ liệu chưa xác minh",
    "schema_incompatible": "Lược đồ dữ liệu không tương thích",
}
_DATA_STATUS = {
    "detected": "Phát hiện vấn đề chất lượng dữ liệu",
    "no_drift": "Chưa phát hiện vấn đề theo policy hiện hành",
    "inconclusive": "Chưa đủ bằng chứng để kết luận",
}


@router.get("/admin/learning/observations")
async def learning_observations_admin_page(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Hiển thị tối đa 50 dòng metadata trong workspace của Principal."""
    response.headers["Cache-Control"] = "no-store"
    service = cast(
        LearningObservationsAdminService | None,
        getattr(request.app.state, "learning_observations_admin_service", None),
    )
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Dịch vụ quan sát mô hình chưa sẵn sàng.",
            headers={"Cache-Control": "no-store"},
        )
    try:
        records = await service.for_principal(identity.principal).list_recent(limit=50)
    except PermissionError as error:
        raise HTTPException(
            status_code=403,
            detail="Không đủ quyền xem quan sát mô hình.",
            headers={"Cache-Control": "no-store"},
        ) from error
    groups = tuple(
        {
            "kind": kind,
            "title": _KIND_TITLES[kind],
            "records": tuple(_present_record(record) for record in records if record.kind == kind),
        }
        for kind in ("observation", "assessment", "proposal")
    )
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/learning_observations.html",
        context={"identity": identity, "groups": groups, "record_count": len(records)},
        headers={"Cache-Control": "no-store"},
    )


def _present_record(record: Any) -> dict[str, object]:
    metadata = record.metadata
    fields: tuple[tuple[str, str], ...]
    if record.kind == "observation":
        fields = (
            ("Mã mô hình thử nghiệm", _short(metadata.get("challenger_registry_id"))),
            ("Phiên bản mô hình", _display(metadata.get("challenger_revision"))),
            (
                "Lý do tạm dừng",
                _OBSERVATION_REASONS.get(metadata.get("challenger_reason"), "Chưa xác định"),
            ),
            ("Quyết định chiến lược", _incumbent_status(metadata.get("incumbent_status"))),
            ("Độ bao phủ snapshot", _percent(metadata.get("snapshot_coverage"))),
            ("Chất lượng snapshot", _boolean(metadata.get("snapshot_quality_ok"))),
            ("Tương thích lược đồ", _boolean(metadata.get("schema_compatible"))),
            ("Quyền dữ liệu", _boolean(metadata.get("rights_eligible"))),
            ("Phiên bản mô hình đang dùng", _display(metadata.get("active_pointer_version"))),
        )
        status = "ABSTAIN — chưa đánh giá hiệu năng"
    elif record.kind == "assessment":
        reasons = metadata.get("reason_codes")
        reason_codes = reasons if isinstance(reasons, list) else []
        reason_labels = tuple(
            _DRIFT_REASONS.get(item, "Lý do chưa nhận diện")
            for item in reason_codes
            if isinstance(item, str)
        )
        fields = (
            ("Mã mô hình trong nhóm", _short(metadata.get("cohort_registry_id"))),
            ("Phiên bản mô hình", _display(metadata.get("cohort_revision"))),
            (
                "Số quan sát / ngưỡng tối thiểu",
                f"{_display(metadata.get('member_count'))} / "
                f"{_display(metadata.get('minimum_observations'))}",
            ),
            ("Quan sát đủ điều kiện", _display(metadata.get("eligible_count"))),
            (
                "Đánh giá dữ liệu",
                _DATA_STATUS.get(metadata.get("data_quality_status"), "Chưa xác định"),
            ),
            ("Đánh giá hiệu năng", "Chưa đủ bằng chứng"),
            ("Độ bao phủ trung bình", _percent(metadata.get("snapshot_coverage_rate"))),
            ("Tỷ lệ snapshot đạt chất lượng", _percent(metadata.get("quality_rate"))),
            ("Tỷ lệ ABSTAIN", _percent(metadata.get("abstention_rate"))),
            ("Lý do", " · ".join(reason_labels) if reason_labels else "Không có"),
        )
        status = _DATA_STATUS.get(metadata.get("data_quality_status"), "Chưa xác định")
    else:
        reasons = metadata.get("reason_codes")
        reason_codes = reasons if isinstance(reasons, list) else []
        reason_labels = tuple(
            _DRIFT_REASONS.get(item, "Lý do chưa nhận diện")
            for item in reason_codes
            if isinstance(item, str)
        )
        fields = (
            ("Mã đánh giá", _short(metadata.get("assessment_id"))),
            ("Phiên bản policy", _display(metadata.get("policy_version"))),
            ("Lý do đề xuất", " · ".join(reason_labels) if reason_labels else "Không có"),
            ("Dấu vân tay nhóm dữ liệu", _short(metadata.get("membership_digest"))),
        )
        status = "Chờ người đánh giá"
    return {
        "record_id": str(record.record_id),
        "occurred_at": record.occurred_at,
        "status": status,
        "fields": fields,
    }


def _display(value: object) -> str:
    return "Chưa có" if value is None else str(value)


def _short(value: object, *, length: int = 12) -> str:
    if value is None:
        return "Chưa có"
    text = str(value)
    return text if len(text) <= length else f"{text[:length]}…"


def _percent(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "Chưa đủ dữ liệu"
    return f"{value * 100:.1f}%".replace(".", ",")


def _boolean(value: object) -> str:
    return "Đạt" if value is True else "Chưa đạt" if value is False else "Chưa xác minh"


def _incumbent_status(value: object) -> str:
    labels = {
        "CANDIDATE": "Đề xuất vào lệnh",
        "ABSTAIN": "Tạm bỏ qua",
        "VETO": "Bị chặn bởi chiến lược",
    }
    return _lookup_label(labels, value, "Chưa xác định")


def _lookup_label(labels: Mapping[str, str], value: object, default: str) -> str:
    return labels.get(value, default) if isinstance(value, str) else default


__all__ = ["learning_observations_admin_page", "router"]
