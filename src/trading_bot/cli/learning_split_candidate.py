"""Entrypoint stage/seal candidate split bằng credential riêng của builder."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from trading_bot.application.learning_split_candidate import (
    LearningSplitCandidateBuilderService,
    LearningSplitCandidateConfig,
)
from trading_bot.cli.candidate_proof import create_builder_pool
from trading_bot.domain.learning.evaluation import TemporalWindow, WalkForwardSplit

MAX_CONFIG_BYTES = 32_768


def parse_request(arguments: list[str] | None = None) -> argparse.Namespace:
    """Stage nhận trial/config; seal chỉ nhận candidate ID và workspace."""
    parser = argparse.ArgumentParser(
        description="Stage hoặc seal manifest split WP06 bằng principal builder riêng."
    )
    commands = parser.add_subparsers(dest="action", required=True)
    stage = commands.add_parser("stage", help="Dựng preview metadata từ một BASE trial.")
    stage.add_argument("--workspace-id", type=UUID, required=True)
    stage.add_argument("--trial-record-id", type=UUID, required=True)
    stage.add_argument("--config-json", required=True)
    seal = commands.add_parser("seal", help="Seal candidate đã được reviewer phê duyệt.")
    seal.add_argument("--workspace-id", type=UUID, required=True)
    seal.add_argument("--candidate-id", type=UUID, required=True)
    return parser.parse_args(arguments)


def parse_config(value: str) -> LearningSplitCandidateConfig:
    """Từ chối key thêm, timestamp thiếu timezone và quá nhiều fold."""
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("config JSON vượt giới hạn kích thước.")
    try:
        payload: Any = json.loads(value)
    except (TypeError, ValueError) as error:
        raise ValueError("config JSON không hợp lệ.") from error
    if type(payload) is not dict or set(payload) != {
        "holdout_start",
        "evaluation_cutoff",
        "folds",
    }:
        raise ValueError("config cần holdout_start, evaluation_cutoff và folds.")
    raw_folds = payload["folds"]
    if type(raw_folds) is not list or not raw_folds:
        raise ValueError("folds phải là danh sách không rỗng.")
    return LearningSplitCandidateConfig(
        holdout_start=_timestamp(payload["holdout_start"]),
        evaluation_cutoff=_timestamp(payload["evaluation_cutoff"]),
        folds=tuple(_fold(item) for item in raw_folds),
    )


async def run(request: argparse.Namespace) -> dict[str, object]:
    """Chạy một thao tác; chỉ trả metadata review và định danh đã ghi."""
    engine, sessions = create_builder_pool()
    try:
        service = LearningSplitCandidateBuilderService(sessions)
        if request.action == "stage":
            stage_result = await service.stage(
                workspace_id=request.workspace_id,
                trial_record_id=request.trial_record_id,
                config=parse_config(request.config_json),
            )
            return {
                "action": "staged",
                "candidate_id": str(stage_result.candidate_id),
                "proof_record_id": str(stage_result.proof_record_id),
                "manifest_hash": stage_result.manifest_hash,
                "fold_plan_hash": stage_result.fold_plan_hash,
                "duplicate": stage_result.duplicate,
                "preview": json.loads(stage_result.preview_json),
            }
        if request.action == "seal":
            seal_result = await service.seal(
                workspace_id=request.workspace_id,
                candidate_id=request.candidate_id,
            )
            return {
                "action": "sealed",
                "candidate_id": str(seal_result.candidate_id),
                "normalized_dataset_id": str(seal_result.normalized_dataset_id),
                "manifest_hash": seal_result.manifest_hash,
                "fold_plan_hash": seal_result.fold_plan_hash,
                "approval_hash": seal_result.approval_hash,
                "duplicate": seal_result.duplicate,
            }
        raise ValueError("Thao tác builder không được hỗ trợ.")
    finally:
        await engine.dispose()


def main() -> None:
    request = parse_request()
    print(json.dumps(asyncio.run(run(request)), ensure_ascii=False))


def _fold(value: object) -> WalkForwardSplit:
    if type(value) is not dict or set(value) != {"train", "validation", "test"}:
        raise ValueError("Mỗi fold chỉ chứa train, validation và test.")
    return WalkForwardSplit(
        train=_window(value["train"]),
        validation=_window(value["validation"]),
        test=_window(value["test"]),
    )


def _window(value: object) -> TemporalWindow:
    if type(value) is not list or len(value) != 2:
        raise ValueError("Mỗi window cần đúng hai timestamp.")
    return TemporalWindow(_timestamp(value[0]), _timestamp(value[1]))


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp phải là chuỗi ISO-8601.")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Timestamp ISO-8601 không hợp lệ.") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp phải có timezone.")
    return result


if __name__ == "__main__":
    main()
