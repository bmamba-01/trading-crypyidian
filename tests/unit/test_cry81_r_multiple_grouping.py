"""Unit test cho hậu kiểm R-multiple theo nhãn MI-7 (CRY-81)."""

from __future__ import annotations

from decimal import Decimal

from trading_bot.domain.portfolio.report_analytics import r_multiples_by_dimensions
from trading_bot.domain.portfolio.reporting import _candidate_explanation


def _trade(
    *,
    trade_id: str,
    net: str,
    risk: str | None = "10",
    strategy: str = "H03",
    entry: str = "DONCHIAN_BREAKOUT",
    exit_reason: str = "TAKE_PROFIT",
    mode: str = "TRENDING",
) -> dict[str, object]:
    entry_reason: dict[str, object] = {
        "strategy_id": strategy,
        "reason_code": entry,
        "context_regime": mode,
    }
    if risk is not None:
        entry_reason["estimated_loss"] = risk
    return {
        "trade_id": trade_id,
        "strategy_release_id": strategy,
        "net_pnl": net,
        "exit_reason": exit_reason,
        "entry_reason": entry_reason,
    }


def test_nhom_r_theo_bon_chieu_va_tong_hop_chinh_xac() -> None:
    trades = [
        _trade(trade_id="t1", net="20", risk="10"),
        _trade(trade_id="t2", net="-5", risk="10"),
        _trade(trade_id="t3", net="4", risk="10", exit_reason="STOP_LOSS", mode="CHOP"),
    ]
    report = r_multiples_by_dimensions(trades)
    assert report["trade_count"] == 3
    assert report["priced_trade_count"] == 3
    assert report["unpriced_trade_count"] == 0
    assert len(report["rows"]) == 2

    rows = report["rows"]
    trending = next(
        row
        for row in rows
        if row["exit_reason"] == "TAKE_PROFIT" and row["market_mode"] == "TRENDING"
    )
    assert trending["strategy"] == "H03"
    assert trending["entry_reason"] == "DONCHIAN_BREAKOUT"
    assert trending["trade_count"] == 2
    assert trending["winning_trades"] == 1
    assert trending["losing_trades"] == 1
    assert Decimal(trending["sum_r"]) == Decimal("1.5")
    assert Decimal(trending["avg_r"]) == Decimal("0.75")
    assert Decimal(trending["best_r"]) == Decimal("2")
    assert Decimal(trending["worst_r"]) == Decimal("-0.5")

    chop = next(row for row in rows if row["exit_reason"] == "STOP_LOSS")
    assert chop["market_mode"] == "CHOP"
    assert Decimal(chop["avg_r"]) == Decimal("0.4")


def test_thieu_1r_khong_gan_r_bang_khong() -> None:
    trades = [
        _trade(trade_id="t1", net="20", risk=None),
        _trade(trade_id="t2", net="10", risk="0"),
        _trade(trade_id="t3", net="5", risk="abc"),
    ]
    report = r_multiples_by_dimensions(trades)
    assert report["unpriced_trade_count"] == 3
    assert report["priced_trade_count"] == 0
    for row in report["rows"]:
        assert row["avg_r"] is None
        assert row["sum_r"] is None
        assert row["priced_trade_count"] == 0


def test_uu_tien_planned_risk_va_override_theo_trade_id() -> None:
    trade = _trade(trade_id="t1", net="30", risk="10")
    trade["entry_reason"]["planned_risk_amount"] = "15"  # type: ignore[index]
    plain = r_multiples_by_dimensions([trade])["rows"][0]
    assert Decimal(plain["avg_r"]) == Decimal("2")

    overridden = r_multiples_by_dimensions([trade], risk_amounts={"t1": "30"})["rows"][0]
    assert Decimal(overridden["avg_r"]) == Decimal("1")


def test_nhan_thieu_duoc_ghi_ro_khong_doan() -> None:
    trade = _trade(trade_id="t1", net="7", risk="7")
    del trade["exit_reason"]
    del trade["entry_reason"]["context_regime"]  # type: ignore[attr-defined]
    row = r_multiples_by_dimensions([trade])["rows"][0]
    assert row["entry_reason"] == "DONCHIAN_BREAKOUT"
    assert row["exit_reason"] == "UNKNOWN"
    assert row["market_mode"] == "UNKNOWN"
    assert Decimal(row["avg_r"]) == Decimal("1")


def test_khong_lo_du_lieu_khong_lien_quan() -> None:
    trade = _trade(trade_id="t1", net="1", risk="1")
    trade["telegram_chat_id"] = "987654321"  # type: ignore[index]
    trade["entry_reason"]["detail_vi"] = "SĐT 0912345678"  # type: ignore[index]
    serialized = str(r_multiples_by_dimensions([trade]))
    assert "987654321" not in serialized
    assert "0912345678" not in serialized


def test_ly_do_vao_mang_nhan_ai_sang_report() -> None:
    """MI-7 cần nhãn AI có trong payload trade; chỉ lấy field có thật, không đoán."""
    explanation = _candidate_explanation(
        {
            "strategy_id": "H03",
            "reason_code": "DONCHIAN_BREAKOUT",
            "context_regime": "TRENDING",
            "estimated_loss": "10.00",
            "risk_budget": "20.00",
            "secret": "không được lộ",
        }
    )
    assert explanation is not None
    assert explanation["context_regime"] == "TRENDING"
    assert explanation["estimated_loss"] == "10.00"
    assert explanation["risk_budget"] == "20.00"
    assert "secret" not in explanation
    assert _candidate_explanation({}) is None
