"""Migration bù ba bảng live market của ``b4d6f8a0c2e5`` cho DB đi theo nhánh ``main`` (CRY-88).

``b4d6f8a0c2e5`` chỉ có trên ``dev`` và được chèn giữa chuỗi (``d2e4f6a8b0c1`` trỏ về nó). Sau khi
hợp nhất, Alembic coi ``b4d6`` là tổ tiên của head cũ của ``main`` nên DB đã ở ``645ed2998ab1``
không bao giờ chạy nó. Migration bù chỉ tạo bảng khi còn thiếu cả ba.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

VERSIONS = Path(__file__).parents[2] / "migrations/versions"
BACKFILL = VERSIONS / "7707d82de246_backfill_live_market_tables.py"
TABLES = ("market_mark_price", "market_liquidation", "market_trade_flow")


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_is_the_single_head_after_merge_revision() -> None:
    module = _load(BACKFILL)
    assert module.revision == "7707d82de246"
    assert module.down_revision == "c1fec3af85ea"
    # Đúng ba bảng của b4d6 (đọc từ chính migration gốc, không chép lại danh sách).
    assert tuple(_load(VERSIONS / "b4d6f8a0c2e5_live_market_event_tables.py")._TABLES) == TABLES


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        (set(TABLES), "skip"),  # DB đi theo dev hoặc DB mới: đã có bảng
        (set(), "create"),  # DB đi theo main: thiếu cả ba
        ({"market_mark_price"}, "error"),  # trạng thái lạ: không tự đoán
    ],
)
def test_backfill_creates_tables_only_when_all_three_are_missing(
    monkeypatch: pytest.MonkeyPatch, present: set[str], expected: str
) -> None:
    module = _load(BACKFILL)
    calls: list[str] = []
    live_market = SimpleNamespace(_TABLES=TABLES, upgrade=lambda: calls.append("upgrade"))
    monkeypatch.setattr(module, "_live_market_module", lambda: live_market)
    monkeypatch.setattr(module.op, "get_bind", lambda: object(), raising=False)
    inspector = SimpleNamespace(has_table=lambda name: name in present)
    monkeypatch.setattr(module.sa, "inspect", lambda _bind: inspector)
    if expected == "error":
        with pytest.raises(RuntimeError, match="market_mark_price"):
            module.upgrade()
        assert calls == []
        return
    module.upgrade()
    assert calls == (["upgrade"] if expected == "create" else [])


def test_backfill_downgrade_never_drops_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(BACKFILL)
    executed: list[Any] = []
    monkeypatch.setattr(module.op, "drop_table", executed.append, raising=False)
    monkeypatch.setattr(module.op, "execute", executed.append, raising=False)
    module.downgrade()
    assert executed == []
