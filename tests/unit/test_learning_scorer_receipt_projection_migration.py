"""Kiểm rewrite projection trên function SQL thật và chặn function body bị drift."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

VERSIONS = Path(__file__).parents[2] / "migrations/versions"


def _load(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), VERSIONS / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _original_definition() -> tuple[str, str]:
    original = _load("d7f9a1b3c5e8_wp06_scorer_current_rights.py")
    qa = _load("8ac4e2f6b0d1_wp06_first_party_qa_source.py")
    statements: list[str] = []
    original.__dict__["op"] = SimpleNamespace(execute=statements.append)
    original._create_deployment_rights_gateway()
    definition = statements[0]
    for previous, replacement, count in qa.DEPLOYMENT_PATCHES:
        assert definition.count(previous) == count
        definition = definition.replace(previous, replacement)
    return definition, _source(definition)


def _source(definition: str) -> str:
    match = re.search(r"AS \$\$([\s\S]+?)\$\$", definition)
    assert match is not None
    return match.group(1)


def _migration() -> ModuleType:
    return _load("f4a6c8e0b2d5_wp06_scorer_receipt_membership_projection.py")


def test_corrected_receipt_record_projects_every_referenced_field() -> None:
    definition, source = _original_definition()
    fixed = _migration()._rewrite_definition(definition, source, forward=True)
    projection = re.search(r"SELECT\s+(receipt\.[\s\S]+?)\s+INTO receipt_row\s+FROM", fixed)
    assert projection is not None
    projected_fields = set(re.findall(r"receipt\.([a-z_]+)", projection.group(1)))
    referenced_fields = set(re.findall(r"receipt_row\.([a-z_]+)", fixed))
    assert "test_membership_digest" in projected_fields
    assert referenced_fields <= projected_fields
    # QA denial và mọi câu lệnh khác phải giữ nguyên byte.
    assert (
        fixed.replace(
            "receipt.receipt_hash,\n                   receipt.test_membership_digest",
            "receipt.receipt_hash",
            1,
        )
        == definition
    )


def test_downgrade_restores_exact_historical_definition() -> None:
    definition, source = _original_definition()
    migration = _migration()
    fixed = migration._rewrite_definition(definition, source, forward=True)
    restored = migration._rewrite_definition(fixed, _source(fixed), forward=False)
    assert restored == definition


@pytest.mark.parametrize("forward", [True, False])
def test_rewrite_rejects_unrelated_body_drift(forward: bool) -> None:
    definition, source = _original_definition()
    migration = _migration()
    if not forward:
        definition = migration._rewrite_definition(definition, source, forward=True)
    definition = definition.replace(
        "WP06_SCORER_DEPLOYMENT_REPORT_INVALID", "WP06_SCORER_DEPLOYMENT_REPORT_DRIFT"
    )
    with pytest.raises(RuntimeError, match="PREIMAGE_UNEXPECTED"):
        migration._rewrite_definition(definition, _source(definition), forward=forward)


def test_rewrite_rejects_definition_body_mismatch() -> None:
    definition, source = _original_definition()
    with pytest.raises(RuntimeError, match="PREIMAGE_UNEXPECTED"):
        _migration()._rewrite_definition(definition.replace(source, ""), source, forward=True)
