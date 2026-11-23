"""Unit test TDD cho CRY-37 ([OPS-1]):
1. `audit_workspace_releases` phát hiện release không còn trong catalog và lệch content_hash.
2. `scripts/seed_signal_test_bots.py check` trả mã thoát 0 khi sạch, mã thoát 2 khi có lệch.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from scripts import seed_signal_test_bots as script

from trading_bot.admin.strategy_release_sync import (
    ExistingReleaseRow,
    audit_workspace_releases,
)
from trading_bot.application.strategy_catalog import strategy_registry

WORKSPACE = uuid4()


def test_audit_workspace_releases_reports_clean_when_all_published_match_registry() -> None:
    registry = strategy_registry()
    h01 = registry.get_release("h01-v1")
    h03 = registry.get_release("h03-v3-1h")
    rows = [
        ExistingReleaseRow(
            id=uuid4(),
            name="h01-v1",
            version=1,
            status="PUBLISHED",
            content_hash=h01.content_hash,
        ),
        ExistingReleaseRow(
            id=uuid4(),
            name="h03-v3-1h",
            version=1,
            status="PUBLISHED",
            content_hash=h03.content_hash,
        ),
        # Bản cũ đã SUPERSEDED lệch hash không bị coi là lỗi.
        ExistingReleaseRow(
            id=uuid4(),
            name="h01-v1",
            version=0,
            status="SUPERSEDED",
            content_hash="0" * 64,
        ),
    ]
    report = audit_workspace_releases(
        workspace_id=WORKSPACE, existing_rows=rows, registry=registry
    )
    assert report.clean is True
    assert report.unknown_releases == ()
    assert report.hash_mismatches == ()
    assert len(report.ok_releases) == 2


def test_audit_workspace_releases_detects_unknown_and_hash_mismatch() -> None:
    registry = strategy_registry()
    h01 = registry.get_release("h01-v1")
    rows = [
        ExistingReleaseRow(
            id=uuid4(),
            name="h01-v1",
            version=1,
            status="PUBLISHED",
            content_hash="f" * 64,  # lệch hash so với registry
        ),
        ExistingReleaseRow(
            id=uuid4(),
            name="legacy-removed-v1",
            version=1,
            status="PUBLISHED",
            content_hash="a" * 64,  # không còn trong catalog
        ),
    ]
    report = audit_workspace_releases(
        workspace_id=WORKSPACE, existing_rows=rows, registry=registry
    )
    assert report.clean is False
    assert len(report.unknown_releases) == 1
    assert report.unknown_releases[0].release_id == "legacy-removed-v1"
    assert len(report.hash_mismatches) == 1
    mismatch = report.hash_mismatches[0]
    assert mismatch.release_id == "h01-v1"
    assert mismatch.expected_hash == h01.content_hash
    assert mismatch.published_hashes == ("f" * 64,)


def test_seed_signal_test_bots_cli_check_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Trạng thái sạch -> return 0
    monkeypatch.setattr(
        script,
        "_run_phase",
        lambda project, service, phase, state: {
            "workspace_id": str(WORKSPACE),
            "clean": True,
            "unknown_releases": [],
            "hash_mismatches": [],
            "ok_releases": ["h01-v1"],
        },
    )
    code_ok = script.main(["check", "--workspace-id", str(WORKSPACE)])
    assert code_ok == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok["clean"] is True

    # 2. Phát hiện lệch release/hash -> return 2
    monkeypatch.setattr(
        script,
        "_run_phase",
        lambda project, service, phase, state: {
            "workspace_id": str(WORKSPACE),
            "clean": False,
            "unknown_releases": [{"release_id": "legacy-v0"}],
            "hash_mismatches": [{"release_id": "h01-v1"}],
            "ok_releases": [],
        },
    )
    code_dirty = script.main(["check", "--workspace-id", str(WORKSPACE)])
    assert code_dirty == 2
    out_dirty = json.loads(capsys.readouterr().out)
    assert out_dirty["clean"] is False
