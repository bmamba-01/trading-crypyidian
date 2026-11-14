"""Kiểm toán chuỗi cung ứng (SBOM CycloneDX) và cách ly network egress theo vai trò (OPS07D)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_EXCHANGE_HOST_MARKERS: tuple[str, ...] = (
    "binance.com",
    "binance.info",
    "fapi.binance",
    "fstream.binance",
    "testnet.binancefuture",
)
_EXCHANGE_CAPABLE_ROLES: frozenset[str] = frozenset({"execution-gateway", "market-ingestor"})


@dataclass(frozen=True)
class SbomAuditResult:
    """Kết quả kiểm tra tệp SBOM CycloneDX."""

    bom_format: str
    spec_version: str
    component_count: int
    unpinned_components: tuple[str, ...]
    passed: bool


@dataclass(frozen=True)
class WorkerEgressRule:
    """Quy tắc mạng đầu ra được phép của một vai trò tiến trình."""

    role: str
    allowed_hosts: tuple[str, ...]


@dataclass(frozen=True)
class EgressIsolationReport:
    """Kết quả kiểm tra nguyên tắc cách ly egress tới sàn giao dịch."""

    passed: bool
    violations: tuple[str, ...]


def audit_sbom_file(sbom_path: Path) -> SbomAuditResult:
    """Đọc và xác minh tệp SBOM CycloneDX có đầy đủ tên và phiên bản đã ghim."""
    raw = json.loads(sbom_path.read_text(encoding="utf-8"))
    bom_format = str(raw.get("bomFormat", ""))
    spec_version = str(raw.get("specVersion", ""))
    components = raw.get("components", [])
    if not isinstance(components, list):
        components = []

    unpinned: list[str] = []
    for item in components:
        if not isinstance(item, dict):
            unpinned.append("<invalid-component>")
            continue
        name = str(item.get("name", "")).strip()
        version = str(item.get("version", "")).strip()
        if not name or not version or "*" in version:
            unpinned.append(name or "<unnamed>")

    passed = bom_format == "CycloneDX" and len(components) > 0 and len(unpinned) == 0
    return SbomAuditResult(
        bom_format=bom_format,
        spec_version=spec_version,
        component_count=len(components),
        unpinned_components=tuple(unpinned),
        passed=passed,
    )


def verify_egress_isolation(rules: Iterable[WorkerEgressRule]) -> EgressIsolationReport:
    """Đảm bảo các worker như admin-web, trainer, research không có egress tới host sàn."""
    violations: list[str] = []
    for rule in rules:
        role_key = rule.role.strip().lower()
        if role_key in _EXCHANGE_CAPABLE_ROLES:
            continue
        for host in rule.allowed_hosts:
            host_lower = host.strip().lower()
            if any(marker in host_lower for marker in _EXCHANGE_HOST_MARKERS):
                violations.append(
                    f"Vai trò '{rule.role}' không được phép kết nối trực tiếp tới '{host}'."
                )
    return EgressIsolationReport(
        passed=len(violations) == 0,
        violations=tuple(violations),
    )
