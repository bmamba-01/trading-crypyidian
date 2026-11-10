"""Hàm chuẩn hóa request để gắn receipt truy cập với đúng evaluation run."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC

from trading_bot.application.learning_evaluation import LearningEvaluationRequest


def evaluation_access_fingerprint(request: LearningEvaluationRequest) -> str:
    """Hash payload request; run ID riêng, không làm thay đổi fingerprint nội dung."""
    if type(request) is not LearningEvaluationRequest:
        raise ValueError("Cần LearningEvaluationRequest authoritative.")
    payload = {
        "workspace_id": str(request.workspace_id),
        "manifest_hash": request.manifest_hash,
        "folds": [
            {
                role: {
                    "start": getattr(fold, role).start.astimezone(UTC).isoformat(),
                    "end": getattr(fold, role).end.astimezone(UTC).isoformat(),
                }
                for role in ("train", "validation", "test")
            }
            for fold in request.folds
        ],
        "evaluation_cutoff": request.evaluation_cutoff.astimezone(UTC).isoformat(),
        "seed": request.seed,
        "purpose": "diagnostic-evaluation",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
