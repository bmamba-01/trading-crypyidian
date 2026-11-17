"""TG-3: hợp đồng migration d5f7b9c1e3a6 — delivery ledger và quyền đọc ledger tối thiểu."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "d5f7b9c1e3a6_tg3_paper_trade_results.py"
)
WORKER = "crypidian_signal_worker"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tg3_trade_result_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(monkeypatch: pytest.MonkeyPatch, step: str) -> list[str]:
    module = _load()
    recorded: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda statement: recorded.append(str(statement)))
    monkeypatch.setattr(module.op, "f", lambda name: name)
    for name in ("create_table", "create_index", "drop_table", "drop_index"):
        monkeypatch.setattr(module.op, name, lambda *_args, **_kwargs: None)
    getattr(module, step)()
    return [re.sub(r"\s+", " ", statement).strip() for statement in recorded]


def test_revision_chains_after_tg2_head() -> None:
    module = _load()

    assert module.revision == "d5f7b9c1e3a6"
    assert module.down_revision == "c3e5a7b9d1f4"


def test_signal_worker_gets_only_column_select_on_ledger_with_bot_scoped_paper_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _statements(monkeypatch, "upgrade")
    ledger_grants = [
        item
        for item in statements
        if item.startswith("GRANT")
        and WORKER in item
        and any(
            f"public.{table} " in item
            for table in ("trade", "fill", "position_event", "risk_reservation", "bot_instance")
        )
    ]

    assert len(ledger_grants) == 5
    # Chỉ SELECT theo danh sách cột; không có quyền mức bảng hay quyền ghi.
    assert all(item.startswith("GRANT SELECT (") for item in ledger_grants)
    assert not any(
        token in item for item in ledger_grants for token in ("INSERT", "UPDATE", "DELETE")
    )
    trade_grant = next(item for item in ledger_grants if "public.trade " in item)
    for forbidden in ("lineage", "gross_pnl", "fee_total", "plan_hash", "candidate_id"):
        assert forbidden not in trade_grant
    assert "reserved_risk" in next(
        item for item in ledger_grants if "public.risk_reservation " in item
    )
    policies = [item for item in statements if "telegram_trade_result_worker_select" in item]
    assert len(policies) == 5
    assert all("FOR SELECT TO crypidian_signal_worker" in item for item in policies)
    assert all("app.bot_id" in item for item in policies)
    for table in ("trade", "fill", "position_event"):
        policy = next(item for item in policies if f"ON public.{table} " in item)
        assert "mode = 'PAPER'" in policy
    assert any("TG3_SIGNAL_WORKER_LEDGER_PRIVILEGE_TOO_WIDE" in item for item in statements)
    for table in ("order_intent", "trade_cost", "exchange_order", "venue_order_intent"):
        assert not any(
            item.startswith("GRANT") and WORKER in item and f"public.{table} " in item
            for item in statements
        )


def test_delivery_tables_have_forced_rls_and_scoped_worker_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _statements(monkeypatch, "upgrade")

    for table in ("telegram_trade_notification", "telegram_paper_report_delivery"):
        assert f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY' in statements
        assert f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, crypidian_app" in statements
        assert f"GRANT SELECT, INSERT ON TABLE public.{table} TO {WORKER}" in statements
        assert (
            "GRANT UPDATE (status, attempts, available_at, lease_owner, lease_expires_at, "
            f"sent_at, last_error_code) ON TABLE public.{table} TO {WORKER}"
        ) in statements
    notification_policies = [
        item for item in statements if "telegram_trade_notification_worker_" in item
    ]
    assert len(notification_policies) == 3
    assert all("app.bot_id" in item for item in notification_policies)


def test_downgrade_revokes_ledger_reads_and_drops_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _statements(monkeypatch, "downgrade")

    assert sum(1 for item in statements if item.startswith("REVOKE SELECT (")) == 5
    assert sum(1 for item in statements if "DROP POLICY telegram_trade_result_worker" in item) == 5
