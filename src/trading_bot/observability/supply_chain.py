"""Kiểm toán chuỗi cung ứng (SBOM CycloneDX), CI gate, quét secret và hardening container."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading_bot.common.secret_inventory import (
    SYSTEM_SECRET_INVENTORY,
    SecretFileCheck,
    SecretInventoryReport,
    SecretInventorySpec,
    derive_mfa_key_id,
    verify_secret_inventory_and_permissions,
)
from trading_bot.observability._yaml_parser import parse_yaml_document
from trading_bot.observability.redaction import contains_secret_canary

_EXCHANGE_HOST_MARKERS: tuple[str, ...] = (
    "binance.com",
    "binance.info",
    "fapi.binance",
    "fstream.binance",
    "testnet.binancefuture",
)
_EXCHANGE_CAPABLE_ROLES: frozenset[str] = frozenset({"execution-gateway", "market-ingestor"})

_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
_POSTGRES_TAG_RE = re.compile(
    r"^postgres:(?P<major>\d+)\.(?P<minor>\d+)-bookworm@sha256:[0-9a-f]{64}$"
)
_MEM_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([bkmgBKMG](?:i?[bB])?)?\s*$")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")
_REAL_TG_TOKEN_RE = re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_-]{33}\b")
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_TEXT_SKIP_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".pdf",
        ".pyc",
        ".db",
        ".sqlite",
    }
)
_SCAN_SKIP_DIRS: frozenset[str] = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


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


@dataclass(frozen=True, slots=True)
class ComposeHardeningReport:
    """Kết quả kiểm tra giới hạn tài nguyên và bind mount :ro trong compose.yaml."""

    passed: bool
    checked_services: tuple[str, ...]
    violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SupplyChainWorkflowReport:
    """Kết quả kiểm tra tĩnh workflow GitHub Actions cổng chuỗi cung ứng."""

    passed: bool
    verified_gates: tuple[str, ...]
    violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SecretScanReport:
    """Kết quả quét rò rỉ canary/secret trong mã nguồn."""

    passed: bool
    scanned_files: int
    findings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImagePinReport:
    """Kết quả kiểm tra ghim digest SHA-256 cho Dockerfile và compose.yaml."""

    passed: bool
    checked_images: tuple[str, ...]
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


def verify_sbom_matches_lockfile(sbom_path: Path, lock_path: Path) -> tuple[bool, tuple[str, ...]]:
    """Đối chiếu danh sách thành phần và phiên bản giữa CycloneDX SBOM và uv.lock."""
    sbom_raw = json.loads(sbom_path.read_text(encoding="utf-8"))
    lock_raw = tomllib.loads(lock_path.read_text(encoding="utf-8"))

    root_name = ""
    meta_comp = sbom_raw.get("metadata", {}).get("component", {})
    if isinstance(meta_comp, dict):
        root_name = str(meta_comp.get("name", "")).strip().lower()

    lock_pkgs = {
        (str(pkg["name"]).strip().lower(), str(pkg["version"]).strip())
        for pkg in lock_raw.get("package", [])
        if isinstance(pkg, dict) and str(pkg.get("name", "")).strip().lower() != root_name
    }
    sbom_pkgs = {
        (str(comp.get("name", "")).strip().lower(), str(comp.get("version", "")).strip())
        for comp in sbom_raw.get("components", [])
        if isinstance(comp, dict)
    }
    diffs: list[str] = []
    for name, version in sorted(lock_pkgs - sbom_pkgs):
        diffs.append(f"Thiếu trong SBOM: {name}=={version}")
    for name, version in sorted(sbom_pkgs - lock_pkgs):
        diffs.append(f"Dư/lệch phiên bản trong SBOM: {name}=={version}")
    return len(diffs) == 0, tuple(diffs)


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


def _parse_memory_bytes(raw: object) -> int | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = int(raw)
        return val if val > 0 else None
    if not isinstance(raw, str):
        return None
    match = _MEM_SIZE_RE.fullmatch(raw)
    if match is None:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower().rstrip("b")
    multipliers = {
        "": 1,
        "k": 1024,
        "ki": 1024,
        "m": 1024**2,
        "mi": 1024**2,
        "g": 1024**3,
        "gi": 1024**3,
    }
    factor = multipliers.get(unit)
    if factor is None or number <= 0:
        return None
    return int(number * factor)


def _parse_positive_float(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        val = float(str(raw).strip())
    except ValueError:
        return None
    return val if val > 0 else None


def _parse_positive_int(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        val = int(raw.strip())
        return val if val > 0 else None
    return None


def _is_bind_mount_source(source: str, named_volumes: frozenset[str]) -> bool:
    clean = source.strip()
    if clean in named_volumes:
        return False
    return clean.startswith((".", "/", "~", "${")) or "/" in clean


def _check_service_limits(
    service_name: str,
    service: Mapping[str, Any],
) -> list[str]:
    violations: list[str] = []
    deploy = service.get("deploy")
    deploy_limits: Mapping[str, Any] = {}
    if isinstance(deploy, Mapping):
        resources = deploy.get("resources")
        if isinstance(resources, Mapping) and isinstance(resources.get("limits"), Mapping):
            deploy_limits = resources["limits"]

    top_cpus = _parse_positive_float(service.get("cpus"))
    top_mem = _parse_memory_bytes(service.get("mem_limit"))
    top_pids = _parse_positive_int(service.get("pids_limit"))
    dep_cpus = _parse_positive_float(deploy_limits.get("cpus")) if deploy_limits else None
    dep_mem = _parse_memory_bytes(deploy_limits.get("memory")) if deploy_limits else None
    dep_pids = _parse_positive_int(deploy_limits.get("pids")) if deploy_limits else None

    if top_cpus is None:
        violations.append(f"Service '{service_name}' thiếu giới hạn `cpus` hợp lệ (> 0).")
    if top_mem is None:
        violations.append(f"Service '{service_name}' thiếu giới hạn `mem_limit` hợp lệ (> 0).")
    if top_pids is None:
        violations.append(f"Service '{service_name}' thiếu giới hạn `pids_limit` hợp lệ (> 0).")

    if deploy_limits:
        if dep_cpus is not None and top_cpus is not None and abs(dep_cpus - top_cpus) > 1e-6:
            violations.append(
                f"Service '{service_name}' lệch `cpus` ({top_cpus}) với "
                f"`deploy.resources.limits.cpus` ({dep_cpus})."
            )
        if dep_mem is not None and top_mem is not None and dep_mem != top_mem:
            violations.append(
                f"Service '{service_name}' lệch `mem_limit` với `deploy.resources.limits.memory`."
            )
        if dep_pids is not None and top_pids is not None and dep_pids != top_pids:
            violations.append(
                f"Service '{service_name}' lệch `pids_limit` với `deploy.resources.limits.pids`."
            )
    return violations


def _check_service_volumes(
    service_name: str,
    volumes: Any,
    named_volumes: frozenset[str],
    writable_bind_targets: frozenset[str],
) -> list[str]:
    violations: list[str] = []
    if not isinstance(volumes, list):
        return violations

    for entry in volumes:
        if isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) < 2:
                continue
            source = parts[0]
            if len(parts) >= 3 and parts[1].startswith("/"):
                target, mode_str = parts[1], parts[2]
            elif len(parts) >= 3:
                target, mode_str = parts[1], parts[-1]
            else:
                target, mode_str = parts[1], ""
            if "docker.sock" in source:
                violations.append(
                    f"Service '{service_name}' không được mount Docker socket '{source}'."
                )
            if _is_bind_mount_source(source, named_volumes) and target not in writable_bind_targets:
                options = {opt.strip() for opt in mode_str.split(",") if opt.strip()}
                if "ro" not in options:
                    violations.append(
                        f"Service '{service_name}' bind mount '{source}:{target}' thiếu cờ `:ro`."
                    )
        elif isinstance(entry, Mapping):
            vol_type = str(entry.get("type", "")).strip().lower()
            source = str(entry.get("source", "")).strip()
            target = str(entry.get("target", "")).strip()
            if "docker.sock" in source:
                violations.append(
                    f"Service '{service_name}' không được mount Docker socket '{source}'."
                )
            is_bind = vol_type == "bind" or (
                bool(source) and _is_bind_mount_source(source, named_volumes)
            )
            if (
                is_bind
                and target not in writable_bind_targets
                and entry.get("read_only") is not True
            ):
                violations.append(
                    f"Service '{service_name}' bind mount '{source}:{target}' "
                    "phải đặt `read_only: true`."
                )
    return violations


def verify_compose_container_hardening(
    compose_path: Path,
    *,
    required_services: Iterable[str] | None = None,
    writable_bind_targets: frozenset[str] = frozenset(),
) -> ComposeHardeningReport:
    """Xác minh compose.yaml giới hạn mem/cpu/pids cho mọi service và bind mount chỉ đọc `:ro`."""
    if not compose_path.exists():
        return ComposeHardeningReport(
            passed=False,
            checked_services=(),
            violations=(f"Không tìm thấy tệp Compose '{compose_path}'.",),
        )
    doc = parse_yaml_document(compose_path.read_text(encoding="utf-8"))
    if not isinstance(doc, Mapping) or not isinstance(doc.get("services"), Mapping):
        return ComposeHardeningReport(
            passed=False,
            checked_services=(),
            violations=("Tệp Compose không chứa mục `services` hợp lệ.",),
        )

    services: Mapping[str, Any] = doc["services"]
    raw_volumes = doc.get("volumes")
    named_volumes = (
        frozenset(str(k) for k in raw_volumes.keys())
        if isinstance(raw_volumes, Mapping)
        else frozenset()
    )
    violations: list[str] = []
    checked_services = tuple(str(name) for name in services.keys())

    if required_services is not None:
        missing = sorted(set(required_services) - set(checked_services))
        if missing:
            violations.append("Compose thiếu các service bắt buộc: " + ", ".join(missing))

    for service_name, raw_service in services.items():
        if not isinstance(raw_service, Mapping):
            violations.append(f"Cấu hình service '{service_name}' không hợp lệ.")
            continue
        violations.extend(_check_service_limits(str(service_name), raw_service))
        violations.extend(
            _check_service_volumes(
                str(service_name),
                raw_service.get("volumes"),
                named_volumes,
                writable_bind_targets,
            )
        )
        if raw_service.get("privileged") is True:
            violations.append(f"Service '{service_name}' không được bật `privileged: true`.")
        if str(raw_service.get("network_mode", "")).strip().lower() == "host":
            violations.append(f"Service '{service_name}' không được dùng `network_mode: host`.")

        image = str(raw_service.get("image", ""))
        if "crypidian-app" in image and service_name != "mfa-volume-provisioner":
            if raw_service.get("read_only") is not True:
                violations.append(f"Service ứng dụng '{service_name}' thiếu `read_only: true`.")
            cap_drop = raw_service.get("cap_drop")
            if not isinstance(cap_drop, list) or "ALL" not in cap_drop:
                violations.append(f"Service ứng dụng '{service_name}' thiếu `cap_drop: [\"ALL\"]`.")
            sec_opt = raw_service.get("security_opt")
            if not isinstance(sec_opt, list) or "no-new-privileges:true" not in sec_opt:
                violations.append(
                    f"Service ứng dụng '{service_name}' thiếu `no-new-privileges:true`."
                )
            if str(raw_service.get("user", "")).strip() in {"", "0", "0:0", "root"}:
                violations.append(f"Service ứng dụng '{service_name}' không được chạy bằng root.")

    return ComposeHardeningReport(
        passed=len(violations) == 0,
        checked_services=checked_services,
        violations=tuple(violations),
    )


def verify_compose_egress_isolation(compose_path: Path) -> EgressIsolationReport:
    """Kiểm tra tách mạng egress và secret Binance trong compose.yaml."""
    doc = parse_yaml_document(compose_path.read_text(encoding="utf-8"))
    if not isinstance(doc, Mapping) or not isinstance(doc.get("services"), Mapping):
        return EgressIsolationReport(
            passed=False,
            violations=("Tệp Compose không chứa `services` hợp lệ.",),
        )
    violations: list[str] = []
    networks = doc.get("networks")
    if not isinstance(networks, Mapping):
        violations.append("Compose thiếu khai báo `networks`.")
    else:
        backend = networks.get("backend")
        if not isinstance(backend, Mapping) or backend.get("internal") is not True:
            violations.append("Mạng `backend` bắt buộc cấu hình `internal: true`.")

    services: Mapping[str, Any] = doc["services"]
    network_owners = {
        "exchange_egress": {"execution-gateway"},
        "source_egress": {"ingestion-worker"},
        "telegram_egress": {"telegram-signal-worker"},
    }
    rules: list[WorkerEgressRule] = []
    for svc_name, raw_svc in services.items():
        if not isinstance(raw_svc, Mapping):
            continue
        svc_nets = raw_svc.get("networks")
        net_names: set[str] = set()
        if isinstance(svc_nets, list):
            net_names = {str(item) for item in svc_nets}
        elif isinstance(svc_nets, Mapping):
            net_names = {str(k) for k in svc_nets.keys()}

        allowed_hosts: list[str] = []
        if "exchange_egress" in net_names:
            allowed_hosts.append("fapi.binance.com")
        if "source_egress" in net_names:
            allowed_hosts.append("fstream.binance.com")
        if "telegram_egress" in net_names:
            allowed_hosts.append("api.telegram.org")
        if "backend" in net_names:
            allowed_hosts.append("postgres")

        mapped_role = "market-ingestor" if svc_name == "ingestion-worker" else str(svc_name)
        rules.append(WorkerEgressRule(role=mapped_role, allowed_hosts=tuple(allowed_hosts)))

        for net_name, expected_services in network_owners.items():
            if net_name in net_names and svc_name not in expected_services:
                violations.append(
                    f"Service '{svc_name}' không được phép gắn vào mạng '{net_name}'."
                )

        svc_secrets = raw_svc.get("secrets")
        secret_sources: set[str] = set()
        if isinstance(svc_secrets, list):
            for sec in svc_secrets:
                if isinstance(sec, str):
                    secret_sources.add(sec)
                elif isinstance(sec, Mapping) and "source" in sec:
                    secret_sources.add(str(sec["source"]))
        if (
            {"binance_api_key", "binance_api_secret"} & secret_sources
        ) and svc_name != "execution-gateway":
            violations.append(f"Service '{svc_name}' không được gắn secret Binance API key/secret.")

    base_report = verify_egress_isolation(rules)
    violations.extend(base_report.violations)
    return EgressIsolationReport(
        passed=len(violations) == 0,
        violations=tuple(violations),
    )


def verify_pinned_container_images(dockerfile_path: Path, compose_path: Path) -> ImagePinReport:
    """Kiểm tra mọi base image và third-party image đều ghim digest @sha256."""
    violations: list[str] = []
    checked: list[str] = []

    docker_lines = dockerfile_path.read_text(encoding="utf-8").splitlines()
    aliases: set[str] = set()
    arg_images: dict[str, str] = {}
    for line in docker_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.upper().startswith("ARG "):
            spec = stripped[4:].strip()
            if "=" in spec:
                arg_name, arg_val = spec.split("=", 1)
                arg_images[arg_name.strip()] = arg_val.strip()
                if "IMAGE" in arg_name.upper():
                    checked.append(arg_val.strip())
                    if not _DIGEST_RE.search(arg_val.strip()):
                        violations.append(f"Dockerfile ARG '{arg_name.strip()}' chưa ghim @sha256.")
        elif stripped.upper().startswith("FROM "):
            parts = stripped.split()
            image_ref = parts[1]
            if len(parts) >= 4 and parts[2].upper() == "AS":
                aliases.add(parts[3])
            if image_ref.startswith("${") and image_ref.endswith("}"):
                var_name = image_ref[2:-1]
                if var_name not in arg_images:
                    violations.append(
                        f"Dockerfile FROM '{image_ref}' thiếu ARG mặc định ghim digest."
                    )
                continue
            if image_ref in aliases:
                continue
            checked.append(image_ref)
            if not _DIGEST_RE.search(image_ref):
                violations.append(f"Dockerfile FROM '{image_ref}' chưa ghim @sha256.")

    compose_doc = parse_yaml_document(compose_path.read_text(encoding="utf-8"))
    services = compose_doc.get("services", {}) if isinstance(compose_doc, Mapping) else {}
    for svc_name, svc in services.items():
        if not isinstance(svc, Mapping):
            continue
        image_ref = str(svc.get("image", "")).strip()
        if not image_ref or "crypidian-app" in image_ref:
            continue
        checked.append(image_ref)
        if not _DIGEST_RE.search(image_ref):
            violations.append(
                f"Compose service '{svc_name}' image '{image_ref}' chưa ghim @sha256."
            )
        if svc_name == "postgres":
            pg_match = _POSTGRES_TAG_RE.fullmatch(image_ref)
            if pg_match is None:
                violations.append(f"Postgres image '{image_ref}' không đúng chuẩn tag+digest.")
            elif (int(pg_match.group("major")), int(pg_match.group("minor"))) < (18, 6):
                violations.append(f"Postgres image '{image_ref}' cũ hơn bản vá 18.6.")

    return ImagePinReport(
        passed=len(violations) == 0,
        checked_images=tuple(checked),
        violations=tuple(violations),
    )


def _iter_scannable_files(repo_root: Path) -> tuple[list[Path], list[str]]:
    findings: list[str] = []
    if (repo_root / ".git").exists():
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            files: list[Path] = []
            for raw_item in proc.stdout.split(b"\x00"):
                if not raw_item:
                    continue
                rel = raw_item.decode("utf-8", errors="replace")
                if rel.startswith(".secrets/") or rel == ".secrets":
                    findings.append(f"Tệp bí mật '{rel}' bị track trong Git.")
                    continue
                if Path(rel).name.startswith(".env") and Path(rel).name != ".env.example":
                    findings.append(f"Tệp môi trường '{rel}' không được phép track trong Git.")
                    continue
                candidate = repo_root / rel
                if candidate.is_file():
                    files.append(candidate)
            return files, findings

    files = []
    for path in sorted(repo_root.rglob("*")):
        rel_parts = path.relative_to(repo_root).parts
        if any(part in _SCAN_SKIP_DIRS for part in rel_parts):
            continue
        if rel_parts and rel_parts[0] == ".secrets":
            rel_str = path.relative_to(repo_root).as_posix()
            findings.append(f"Thư mục bí mật '{rel_str}' tồn tại trong cây quét.")
            continue
        if path.is_file():
            files.append(path)
    return files, findings


def scan_repository_for_leaked_secrets(
    repo_root: Path,
    *,
    extra_canaries: Iterable[str] = (),
) -> SecretScanReport:
    """Quét mã nguồn tìm private key, token thật, canary rò rỉ hoặc tệp .secrets bị track."""
    canaries = tuple(c for c in extra_canaries if c and len(c) >= 8)
    files, findings = _iter_scannable_files(repo_root)
    scanned = 0
    for file_path in files:
        if file_path.suffix.lower() in _TEXT_SKIP_SUFFIXES:
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        scanned += 1
        rel = file_path.relative_to(repo_root).as_posix()
        if canaries and contains_secret_canary(text, canaries):
            findings.append(f"Phát hiện rò rỉ secret canary trong '{rel}'.")
        if _PRIVATE_KEY_RE.search(text):
            findings.append(f"Phát hiện tiêu đề private key PEM trong '{rel}'.")
        if _REAL_TG_TOKEN_RE.search(text):
            findings.append(f"Phát hiện mẫu Telegram Bot token thật trong '{rel}'.")
        if _AWS_KEY_RE.search(text):
            findings.append(f"Phát hiện mẫu AWS Access Key ID trong '{rel}'.")

    return SecretScanReport(
        passed=len(findings) == 0,
        scanned_files=scanned,
        findings=tuple(findings),
    )


def _active_shell_lines(run_script: str) -> str:
    active: list[str] = []
    for raw_line in run_script.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        active.append(stripped)
    return "\n".join(active)


def _validate_workflow_permissions(
    permissions: Any,
    *,
    context: str,
    violations: list[str],
) -> bool:
    if isinstance(permissions, str):
        if permissions.strip().lower() == "write-all":
            violations.append(f"{context} dùng `permissions: write-all` không an toàn.")
            return False
        if permissions.strip().lower() == "read-all":
            return True
        return False
    if not isinstance(permissions, Mapping):
        return False
    for scope in ("contents", "packages", "id-token", "actions", "security-events"):
        if str(permissions.get(scope, "")).strip().lower() == "write":
            violations.append(f"{context} cấp quyền ghi dư thừa `{scope}: write`.")
    return str(permissions.get("contents", "")).strip().lower() == "read"


def verify_supply_chain_ci_workflow(workflow_path: Path) -> SupplyChainWorkflowReport:
    """Kiểm tra tĩnh file GitHub Actions workflow không bị xóa hoặc bỏ bước cổng SEC-45."""
    if not workflow_path.exists():
        return SupplyChainWorkflowReport(
            passed=False,
            verified_gates=(),
            violations=(f"Thiếu tệp CI workflow '{workflow_path}'.",),
        )
    doc = parse_yaml_document(workflow_path.read_text(encoding="utf-8"))
    if not isinstance(doc, Mapping):
        return SupplyChainWorkflowReport(
            passed=False,
            verified_gates=(),
            violations=("Workflow CI phải là YAML mapping hợp lệ.",),
        )

    violations: list[str] = []
    triggers = doc.get("on")
    if (
        not isinstance(triggers, Mapping)
        or "push" not in triggers
        or "pull_request" not in triggers
    ):
        violations.append("Workflow CI phải kích hoạt cho cả `push` và `pull_request`.")

    has_top_read = _validate_workflow_permissions(
        doc.get("permissions"), context="Workflow CI", violations=violations
    )

    jobs = doc.get("jobs")
    if not isinstance(jobs, Mapping) or not jobs:
        violations.append("Workflow CI phải định nghĩa ít nhất một job.")
        return SupplyChainWorkflowReport(
            passed=False,
            verified_gates=(),
            violations=tuple(violations),
        )

    active_runs: list[str] = []
    all_jobs_have_read = True
    for job_name, raw_job in jobs.items():
        if not isinstance(raw_job, Mapping):
            violations.append(f"Cấu hình job '{job_name}' không hợp lệ.")
            continue
        job_perm = raw_job.get("permissions")
        if job_perm is not None:
            if not _validate_workflow_permissions(
                job_perm, context=f"Job '{job_name}'", violations=violations
            ):
                all_jobs_have_read = False
        elif not has_top_read:
            all_jobs_have_read = False

        steps = raw_job.get("steps")
        if not isinstance(steps, list):
            violations.append(f"Job '{job_name}' thiếu danh sách `steps`.")
            continue
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            run_text = step.get("run")
            if not isinstance(run_text, str):
                continue
            cleaned = _active_shell_lines(run_text)
            if step.get("continue-on-error") is True:
                step_name = step.get("name", "<unnamed>")
                violations.append(
                    f"Job '{job_name}' bước '{step_name}' không được đặt `continue-on-error: true`."
                )
                continue
            active_runs.append(cleaned)

    if not has_top_read and not all_jobs_have_read:
        violations.append(
            "Workflow CI thiếu cấu hình quyền tối thiểu `permissions: contents: read`."
        )

    combined_run = "\n".join(active_runs)
    required_gates: tuple[tuple[str, tuple[str, ...], str], ...] = (
        ("uv_lock_check", ("uv lock --check",), "Thiếu cổng `uv lock --check`."),
        ("uv_sync_frozen", ("uv sync --frozen",), "Thiếu cổng `uv sync --frozen`."),
        ("uv_audit", ("uv audit",), "Thiếu cổng `uv audit` kiểm tra CVE."),
        ("ruff_check", ("ruff check",), "Thiếu cổng `ruff check`."),
        ("mypy", ("mypy",), "Thiếu cổng `mypy`."),
        (
            "sbom_audit",
            ("audit_sbom_file", "--check-sbom"),
            "Thiếu cổng kiểm tra SBOM CycloneDX (`audit_sbom_file`).",
        ),
        (
            "sbom_export",
            ("cyclonedx",),
            "Thiếu bước xuất kiểm chứng SBOM CycloneDX từ uv.lock.",
        ),
        (
            "secret_scan",
            ("scan_repository_for_leaked_secrets", "--scan-secrets", "contains_secret_canary"),
            "Thiếu cổng quét rò rỉ canary/secret trong mã nguồn.",
        ),
        (
            "container_hardening",
            ("verify_compose_container_hardening", "--check-containers"),
            "Thiếu cổng kiểm tra giới hạn container / bind mount `:ro`.",
        ),
        (
            "egress_isolation",
            ("verify_compose_egress_isolation", "verify_egress_isolation", "--check-containers"),
            "Thiếu cổng kiểm tra cách ly egress.",
        ),
        (
            "compose_config",
            ("docker compose",),
            "Thiếu bước xác thực `docker compose ... config`.",
        ),
        (
            "secret_inventory",
            ("verify_secret_inventory_and_permissions", "--check-secrets-dir"),
            "Thiếu cổng kiểm tra danh mục secret & quyền `0600`.",
        ),
    )

    verified_gates: list[str] = []
    for gate_id, markers, error_message in required_gates:
        if any(marker in combined_run for marker in markers):
            verified_gates.append(gate_id)
        else:
            violations.append(error_message)

    return SupplyChainWorkflowReport(
        passed=len(violations) == 0,
        verified_gates=tuple(verified_gates),
        violations=tuple(violations),
    )


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kiểm toán chuỗi cung ứng & container SEC-44..46")
    parser.add_argument("--check-sbom", action="store_true")
    parser.add_argument("--sbom-file", type=Path, default=Path("docs/implementation/sbom.cdx.json"))
    parser.add_argument("--lock-file", type=Path, default=Path("uv.lock"))
    parser.add_argument("--scan-secrets", action="store_true")
    parser.add_argument("--check-containers", action="store_true")
    parser.add_argument("--check-workflow", action="store_true")
    parser.add_argument("--check-secrets-dir", type=Path, default=None)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)

    root = Path(".")
    failures: list[str] = []

    if args.all or args.check_sbom:
        sbom_res = audit_sbom_file(args.sbom_file)
        if not sbom_res.passed:
            failures.append(f"SBOM không hợp lệ: {sbom_res.unpinned_components}")
        matched, diffs = verify_sbom_matches_lockfile(args.sbom_file, args.lock_file)
        if not matched:
            failures.extend(diffs)

    if args.all or args.scan_secrets:
        scan = scan_repository_for_leaked_secrets(root)
        if not scan.passed:
            failures.extend(scan.findings)

    if args.all or args.check_containers:
        hardening = verify_compose_container_hardening(root / "compose.yaml")
        if not hardening.passed:
            failures.extend(hardening.violations)
        egress = verify_compose_egress_isolation(root / "compose.yaml")
        if not egress.passed:
            failures.extend(egress.violations)
        images = verify_pinned_container_images(root / "Dockerfile", root / "compose.yaml")
        if not images.passed:
            failures.extend(images.violations)

    if args.all or args.check_workflow:
        wf = verify_supply_chain_ci_workflow(root / ".github/workflows/supply-chain-security.yml")
        if not wf.passed:
            failures.extend(wf.violations)

    if args.check_secrets_dir is not None:
        sec_report = verify_secret_inventory_and_permissions(args.check_secrets_dir)
        if not sec_report.passed:
            failures.extend(sec_report.violations)

    if failures:
        for item in failures:
            print(f"[FAIL] {item}", file=sys.stderr)
        return 1
    print("[OK] Toàn bộ kiểm tra chuỗi cung ứng, secret và container đều đạt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = [
    "SYSTEM_SECRET_INVENTORY",
    "ComposeHardeningReport",
    "EgressIsolationReport",
    "ImagePinReport",
    "SbomAuditResult",
    "SecretFileCheck",
    "SecretInventoryReport",
    "SecretInventorySpec",
    "SecretScanReport",
    "SupplyChainWorkflowReport",
    "WorkerEgressRule",
    "audit_sbom_file",
    "derive_mfa_key_id",
    "scan_repository_for_leaked_secrets",
    "verify_compose_container_hardening",
    "verify_compose_egress_isolation",
    "verify_egress_isolation",
    "verify_pinned_container_images",
    "verify_sbom_matches_lockfile",
    "verify_secret_inventory_and_permissions",
    "verify_supply_chain_ci_workflow",
]
