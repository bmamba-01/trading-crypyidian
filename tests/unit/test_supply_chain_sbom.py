"""Unit test cho kiểm tra Supply Chain, SBOM và Egress Isolation (OPS07D)."""

from __future__ import annotations

from pathlib import Path

from trading_bot.observability.supply_chain import (
    WorkerEgressRule,
    audit_sbom_file,
    verify_egress_isolation,
)


def test_repo_sbom_is_valid_cyclonedx_with_pinned_versions() -> None:
    """Kiểm tra docs/implementation/sbom.cdx.json hợp lệ và dependency đều ghim phiên bản."""
    sbom_path = Path("docs/implementation/sbom.cdx.json")
    result = audit_sbom_file(sbom_path)
    assert result.bom_format == "CycloneDX"
    assert result.component_count > 0
    assert result.unpinned_components == ()
    assert result.passed is True


def test_egress_isolation_blocks_exchange_hosts_on_non_gateway_workers() -> None:
    """Admin, trainer và research worker không được có egress tới host sàn giao dịch."""
    rules = [
        WorkerEgressRule(role="admin-web", allowed_hosts=("postgres", "redis")),
        WorkerEgressRule(role="trainer", allowed_hosts=("postgres", "object-store")),
        WorkerEgressRule(role="execution-gateway", allowed_hosts=("fapi.binance.com", "postgres")),
    ]
    report = verify_egress_isolation(rules)
    assert report.passed is True
    assert report.violations == ()

    bad_rules = [
        WorkerEgressRule(role="trainer", allowed_hosts=("fapi.binance.com", "postgres")),
    ]
    bad_report = verify_egress_isolation(bad_rules)
    assert bad_report.passed is False
    assert len(bad_report.violations) == 1
    assert "trainer" in bad_report.violations[0]
