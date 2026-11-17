"""Kiểm tĩnh guard downgrade QA của 8ac4e2f6b0d1.

Từ `b7d9f1a3c5e8` receipt replay TRAINING đã ghi chặn downgrade trước, nên E2E source-pinned
có dữ liệu không còn chạm tới các guard này; cùng tiền lệ downgrade-che-guard của WP07.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

MIGRATION = Path(__file__).parents[2] / (
    "migrations/versions/8ac4e2f6b0d1_wp06_first_party_qa_source.py"
)


def _downgrade_blocks() -> list[str]:
    spec = importlib.util.spec_from_file_location("wp06_first_party_qa_source", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    module.downgrade()
    return statements


@pytest.mark.parametrize(
    ("index", "signature", "exception", "marker"),
    [
        (
            0,
            "public.wp06_verify_scorer_deployment_rights(uuid, uuid, uuid)",
            "WP06_SCORER_DEPLOYMENT_QA_GUARD_DOWNGRADE",
            "WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED",
        ),
        (
            1,
            "public.wp06_recheck_scorer_current_rights(uuid, uuid)",
            "WP06_SCORER_CURRENT_RIGHTS_QA_DOWNGRADE",
            "CRYPIDIAN_FIRST_PARTY_QA",
        ),
        (
            2,
            "public.wp06_lock_scorer_test_sources(uuid, uuid)",
            "WP06_SCORER_TEST_QA_LOCK_DOWNGRADE",
            "CRYPIDIAN_FIRST_PARTY_QA",
        ),
    ],
)
def test_qa_downgrade_rejects_drifted_definitions_before_and_after_rewrite(
    index: int, signature: str, exception: str, marker: str
) -> None:
    block = _downgrade_blocks()[index]
    assert f"pg_get_functiondef('{signature}'::regprocedure)" in block
    # Marker QA phải có trước và biến mất sau; mỗi patch đảo phải khớp đúng số lần.
    assert f"pg_catalog.strpos(v_definition, '{marker}') = 0" in block
    assert f"RAISE EXCEPTION '{exception}_MARKER_PREIMAGE_UNEXPECTED'" in block
    assert f"RAISE EXCEPTION '{exception}_PREIMAGE_UNEXPECTED'" in block
    assert f"RAISE EXCEPTION '{exception}_MARKER_POSTIMAGE_UNEXPECTED'" in block
    assert block.index("_MARKER_PREIMAGE_UNEXPECTED") < block.index("EXECUTE v_definition")
    assert block.index("EXECUTE v_definition") < block.index("_MARKER_POSTIMAGE_UNEXPECTED")


def test_qa_downgrade_order_reverses_upgrade_and_keeps_data() -> None:
    blocks = _downgrade_blocks()
    assert len(blocks) == 3
    assert "wp06_verify_scorer_deployment_rights" in blocks[0]
    assert "wp06_recheck_scorer_current_rights" in blocks[1]
    assert "wp06_lock_scorer_test_sources" in blocks[2]
    assert not any(word in "\n".join(blocks) for word in ("DELETE FROM", "TRUNCATE", "DROP TABLE"))
