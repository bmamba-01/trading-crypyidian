"""SEC-01: migration thu hẹp quyền process role; kiểm SQL sinh ra mà không cần PostgreSQL."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "f4d6b8c0e2a5_process_role_least_privilege.py"
)
WORKERS = (
    "crypidian_execution_gateway",
    "crypidian_ingestion_worker",
    "crypidian_trainer_worker",
)
TENANT_TABLES = (
    "user_membership",
    "role_binding",
    "session",
    "mfa_factor",
    "telegram_binding",
    "trading_account",
    "secret_reference",
    "config_version",
    "risk_profile_version",
    "approval_request",
    "watchlist_version",
    "watchlist_item",
    "strategy_release",
)
ADMIN_ONLY_POLICIES = (
    "user_membership",
    "role_binding",
    "session",
    "mfa_factor",
    "telegram_binding",
    "secret_reference",
    "watchlist_version",
    "watchlist_item",
)
SHARED_POLICIES = (
    "trading_account",
    "risk_profile_version",
    "strategy_release",
    "workspace_settings_version",
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sec01_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(module: ModuleType, step: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda statement: recorded.append(str(statement)))
    getattr(module, step)()
    return [re.sub(r"\s+", " ", statement).strip() for statement in recorded]


def test_revision_chain_is_reserved_for_controller_integration() -> None:
    module = _load()

    assert module.revision == "f4d6b8c0e2a5"
    assert module.down_revision == "c7a90d3f8e21"


def test_upgrade_removes_group_identity_access_and_worker_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _statements(_load(), "upgrade", monkeypatch)

    for table in (*TENANT_TABLES, "workspace", "workspace_settings_version"):
        assert f'REVOKE ALL ON TABLE "{table}" FROM crypidian_app' in statements
    for role in WORKERS:
        assert f"REVOKE crypidian_app FROM {role}" in statements
        assert f"GRANT USAGE ON SCHEMA public TO {role}" in statements
        # Worker không nhận grant trực tiếp nào trên bảng identity/config.
        assert not [
            s
            for s in statements
            if s.startswith("GRANT") and f"TO {role}" in s and "SCHEMA" not in s
        ]
    for table in ADMIN_ONLY_POLICIES:
        assert f'ALTER POLICY workspace_isolation ON "{table}" TO crypidian_admin_web' in statements
    for table in SHARED_POLICIES:
        assert (
            f'ALTER POLICY workspace_isolation ON "{table}" '
            "TO crypidian_admin_web, crypidian_trading_runtime" in statements
        )
    assert not [s for s in statements if "DELETE" in s and s.startswith("GRANT")]
    assert not [s for s in statements if s.startswith("GRANT") and "crypidian_app" in s]


def test_upgrade_grants_runtime_only_reads_and_lock_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _statements(_load(), "upgrade", monkeypatch)
    runtime = sorted(
        s
        for s in statements
        if s.endswith("TO crypidian_trading_runtime") and s.startswith("GRANT")
    )

    assert runtime == sorted(
        [
            'GRANT INSERT ON TABLE "trading_account" TO crypidian_trading_runtime',
            'GRANT SELECT ON TABLE "approval_request" TO crypidian_trading_runtime',
            'GRANT SELECT ON TABLE "config_version" TO crypidian_trading_runtime',
            'GRANT SELECT ON TABLE "risk_profile_version" TO crypidian_trading_runtime',
            'GRANT SELECT ON TABLE "strategy_release" TO crypidian_trading_runtime',
            'GRANT SELECT ON TABLE "trading_account" TO crypidian_trading_runtime',
            'GRANT SELECT ON TABLE "workspace" TO crypidian_trading_runtime',
            'GRANT UPDATE (updated_at) ON TABLE "trading_account" TO crypidian_trading_runtime',
        ]
    )


def test_upgrade_limits_admin_updates_to_columns_code_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _statements(_load(), "upgrade", monkeypatch)
    admin_updates = sorted(
        s for s in statements if s.startswith("GRANT UPDATE") and "crypidian_admin_web" in s
    )

    assert admin_updates == sorted(
        [
            'GRANT UPDATE (csrf_token_hash, mfa_verified_at, revoked_at) ON TABLE "session" '
            "TO crypidian_admin_web",
            'GRANT UPDATE (last_used_counter, verified_at) ON TABLE "mfa_factor" '
            "TO crypidian_admin_web",
            # Lock-only: không code admin nào (kể cả MB-02 e53331b) sửa cột account/membership.
            'GRANT UPDATE (updated_at) ON TABLE "trading_account" TO crypidian_admin_web',
            'GRANT UPDATE (created_at) ON TABLE "user_membership" TO crypidian_admin_web',
        ]
    )


def test_downgrade_restores_legacy_group_grants_and_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    upgrade = _statements(module, "upgrade", monkeypatch)
    downgrade = _statements(module, "downgrade", monkeypatch)

    for table in TENANT_TABLES:
        assert (
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "{table}" TO crypidian_app' in downgrade
        )
    assert 'GRANT SELECT ON TABLE "workspace" TO crypidian_app' in downgrade
    assert (
        'GRANT SELECT, INSERT, UPDATE ON TABLE "workspace_settings_version" TO crypidian_app'
        in downgrade
    )
    for role in WORKERS:
        assert f"GRANT crypidian_app TO {role}" in downgrade
        assert f"REVOKE USAGE ON SCHEMA public FROM {role}" in downgrade
    for table in (*ADMIN_ONLY_POLICIES, *SHARED_POLICIES):
        assert f'ALTER POLICY workspace_isolation ON "{table}" TO PUBLIC' in downgrade
    # Mỗi grant trực tiếp mới của upgrade có REVOKE tương ứng; grant cũ không bị đụng.
    granted = [s for s in upgrade if s.startswith("GRANT") and " ON TABLE " in s]
    for statement in granted:
        privilege, rest = statement.removeprefix("GRANT ").split(" ON TABLE ", 1)
        table, role = rest.split(" TO ", 1)
        assert f"REVOKE {privilege} ON TABLE {table} FROM {role}" in downgrade, statement
    assert 'REVOKE INSERT ON TABLE "workspace" FROM crypidian_admin_web' not in downgrade
