"""Strict schema tests for the one-shot candidate builder CLI."""

from __future__ import annotations

import json

import pytest

from trading_bot.cli.learning_split_candidate import parse_config, parse_request


def _config() -> str:
    return json.dumps(
        {
            "holdout_start": "2025-01-05T00:00:00+00:00",
            "evaluation_cutoff": "2025-01-04T00:00:00+00:00",
            "folds": [
                {
                    "train": ["2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00"],
                    "validation": [
                        "2025-01-02T00:00:00+00:00",
                        "2025-01-03T00:00:00+00:00",
                    ],
                    "test": ["2025-01-03T00:00:00+00:00", "2025-01-04T00:00:00+00:00"],
                }
            ],
        }
    )


def test_stage_config_accepts_only_timestamp_windows_and_holdout_cutoff() -> None:
    config = parse_config(_config())

    assert config.holdout_start.isoformat() == "2025-01-05T00:00:00+00:00"
    assert len(config.folds) == 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"caller_samples": []}, "config cần"),
        ({"holdout_start": "2025-01-05T00:00:00"}, "timezone"),
    ],
)
def test_stage_config_rejects_extra_payload_and_naive_timestamp(change, message) -> None:
    payload = json.loads(_config())
    if "caller_samples" in change:
        payload.update(change)
    else:
        payload["holdout_start"] = change["holdout_start"]

    with pytest.raises(ValueError, match=message):
        parse_config(json.dumps(payload))


def test_cli_separates_stage_and_seal_scopes() -> None:
    workspace_id = "9b311d98-b69b-4f73-9837-9af065d4c51d"
    trial_id = "f2bef971-992f-45fd-9e9f-caa47e8be055"
    candidate_id = "96f9d877-a525-46c0-b88b-c8e11d33c921"

    stage = parse_request(
        [
            "stage",
            "--workspace-id",
            workspace_id,
            "--trial-record-id",
            trial_id,
            "--config-json",
            _config(),
        ]
    )
    seal = parse_request(["seal", "--workspace-id", workspace_id, "--candidate-id", candidate_id])

    assert stage.action == "stage"
    assert seal.action == "seal"
