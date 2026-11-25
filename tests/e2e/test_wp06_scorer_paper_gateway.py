"""Compose fail-closed cho wiring gateway PAPER của scorer đã train.

- Manifest source-item WP05 (v1–v3) với canonical source TRAINING-only bị chặn trước khi
  train: source-dependent manifest v3 thiếu producer lineage bị từ chối bằng
  `source_use_lineage_missing`, nên không có artifact/deployment.
- Manifest replay v5 (QA) train được; từ lát B (`d4f6a8b0c2e4`) gateway đọc được lineage v5
  (không còn `MANIFEST_MISSING`). Artifact chưa có TEST proof/report nên preflight PAPER không
  có canonical source để kiểm và gateway từ chối `WP06_SCORER_DEPLOYMENT_REPORT_NOT_FOUND`,
  không đổi pointer/event/score. Đường có proof + report (QA bị
  `WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED`) do phase deployment của
  `test_wp06_source_pinned_replay_persistence.py[source-linked-test]` phủ.

- Replay v5 nguồn non-QA `BINANCE_USDM_REST` có policy chỉ BACKTEST_RESEARCH + TRAINING (không
  PAPER_DEMO), train thật và có TEST proof: preflight PAPER lấy canonical source từ bundle của
  proof và gateway từ chối đúng `WP06_SCORER_PAPER_SOURCE_RIGHTS_DENIED` trước lookup report,
  không đổi pointer/event/score.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from scripts.docker_image import build_image_reference, ensure_image_available

from tests.e2e.test_wp06_source_pinned_replay_persistence import _compose_probe

GATEWAY_PROBE = "/e2e-tests/e2e/probes/wp06_scorer_paper_gateway.py"
EMPTY_LEDGER = {
    "learning_scorer_artifact": 0,
    "learning_score_event": 0,
    "learning_scoring_deployment_event": 0,
    "enabled_deployment_pointer": 0,
}


def test_real_deployment_gateway_fails_closed_for_trained_and_legacy_sources() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    project = f"crypidian-wp06-paper-gateway-{uuid.uuid4().hex[:10]}"
    replay_workspace = str(uuid.uuid4())
    binance_workspace = str(uuid.uuid4())
    image = build_image_reference(root)
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image,
        "CRYPIDIAN_E2E_WORKSPACE_ID": replay_workspace,
    }
    image_created = ensure_image_available(root, image)
    volume = f"{root / 'tests'}:/e2e-tests:ro"

    def compose(*args):
        return _compose_probe(root, project, environment, *args)

    def run_json(service, script, *arguments, values=None):
        options = ["run", "--rm", "--no-deps", "--volume", volume]
        for key, value in (values or {}).items():
            options.extend(["-e", f"{key}={value}"])
        result = compose(*options, service, "python", script, *arguments)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def normalized(service, phase, values=None):
        return run_json(
            service,
            "/e2e-tests/e2e/probes/wp06_normalized_fold_samples.py",
            values={"WP06_NORMALIZED_PHASE": phase} | (values or {}),
        )

    def train_replay(workspace_id, *seed_flags, through_proof=False):
        """Seed replay pinned rồi đi stage → approve → seal → enqueue → train (→ proof)."""
        state = run_json(
            "migrator",
            "/e2e-tests/e2e/probes/wp06_source_pinned_replay_persistence.py",
            "--scorer-seed",
            *seed_flags,
            values={"CRYPIDIAN_E2E_WORKSPACE_ID": workspace_id},
        )
        state["evaluation_run_id"] = str(uuid.uuid4())
        phases = [
            ("candidate-proof-builder", "stage"),
            ("admin-web", "approve"),
            ("candidate-proof-builder", "seal"),
            ("admin-web", "enqueue"),
            ("trainer-worker", "train"),
        ]
        if through_proof:
            phases.append(("candidate-proof-builder", "proof"))
        for service, phase in phases:
            state |= run_json(
                service,
                "/e2e-tests/e2e/probes/wp06_source_linked_scorer_test.py",
                phase,
                values={"WP06_SOURCE_LINKED_STATE": json.dumps(state)},
            )
        assert state["status"] == "succeeded"
        return state

    def denied_by_gateway(workspace_id, attempt_id, expected):
        return run_json(
            "admin-web",
            GATEWAY_PROBE,
            values={
                "WP06_WORKSPACE_ID": workspace_id,
                "WP06_ATTEMPT_ID": attempt_id,
                "WP06_EXPECTED_DENIAL": expected,
                "WP06_EXPECTED_ARTIFACT_STATE": "ready_for_evaluation",
            },
        )

    denial_result = {
        "real_gateway_paper_rights_denied": True,
        "pointer_and_event_ledger_unchanged": True,
        "diagnostic_artifact_only": True,
        "positive_rights_evidence": False,
    }

    def ledger(workspace_id):
        return run_json(
            "migrator",
            GATEWAY_PROBE,
            values={"WP06_GATEWAY_PHASE": "ledger", "WP06_WORKSPACE_ID": workspace_id},
        )

    try:
        compose("up", "--detach", "--wait", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")

        # 1) Canonical source TRAINING-only qua manifest source-item WP05: bị chặn trước train.
        source = normalized("admin-web", "source-admin-seed")
        source_values = {
            ("WP06_SOURCE_ID" if key == "source_id" else f"WP06_SOURCE_{key.upper()}"): value
            for key, value in source.items()
        }
        normalized("ingestion-worker", "source-item-seed", source_values)
        manifest = normalized("migrator", "source-manifest-seed", source_values)
        with pytest.raises(AssertionError) as legacy:
            normalized(
                "candidate-proof-builder",
                "builder",
                {
                    "WP06_WORKSPACE_ID": manifest["workspace_id"],
                    "WP06_MANIFEST_HASH": manifest["manifest_hash"],
                },
            )
        assert "source_use_lineage_missing" in str(legacy.value)
        assert normalized(
            "candidate-proof-builder",
            "source-rights-verify-unchanged",
            {
                "WP06_SOURCE_WORKSPACE_ID": manifest["workspace_id"],
                "WP06_SOURCE_MANIFEST_HASH": manifest["manifest_hash"],
                "WP06_EXPECTED_SOURCE_RECEIPTS": "0",
                "WP06_EXPECTED_NORMALIZED_SAMPLES": "0",
            },
        ) == {"receipt_unchanged": True, "normalized_sample_count_unchanged": True}
        assert ledger(manifest["workspace_id"]) == EMPTY_LEDGER

        # 2) Replay QA manifest v5 train thật, chưa có TEST report: gateway từ chối.
        state = train_replay(replay_workspace)
        before = ledger(replay_workspace)
        assert before == EMPTY_LEDGER | {"learning_scorer_artifact": 1}
        assert (
            denied_by_gateway(
                replay_workspace, state["attempt_id"], "WP06_SCORER_DEPLOYMENT_REPORT_NOT_FOUND"
            )
            == denial_result
        )
        assert ledger(replay_workspace) == before

        # 3) Nguồn non-QA chỉ TRAINING, có TEST proof: chặn đúng ở gate PAPER.
        binance = train_replay(binance_workspace, "--binance", through_proof=True)
        assert binance["qa_provider"] == "BINANCE_USDM_REST"
        before = ledger(binance_workspace)
        assert before == EMPTY_LEDGER | {"learning_scorer_artifact": 1}
        assert (
            denied_by_gateway(
                binance_workspace, binance["attempt_id"], "WP06_SCORER_PAPER_SOURCE_RIGHTS_DENIED"
            )
            == denial_result
        )
        assert ledger(binance_workspace) == before
    finally:
        compose("down", "--volumes", "--remove-orphans")
        if image_created:
            subprocess.run(
                ["docker", "image", "rm", image], capture_output=True, check=True, timeout=120
            )
