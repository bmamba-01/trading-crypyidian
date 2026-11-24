"""SIG-5: báo cáo kiểm định scalping theo setup và kết luận đề xuất PAPER theo tiêu chí #5."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.portfolio.scalping_validation import (
    CRITERION_VERSION,
    MIN_OUT_OF_SAMPLE_TRADES,
    ScalpTrade,
    paper_enable_verdict,
    setup_report,
    walk_forward_folds,
)

START = datetime(2026, 1, 5, tzinfo=UTC)  # Thứ Hai
END = START + timedelta(days=28)


def _trade(day: int, hour: int, net: str, *, setup: str = "h18-v1", risk: str = "10") -> ScalpTrade:
    opened = START + timedelta(days=day, hours=hour)
    return ScalpTrade(
        trade_id=f"{setup}:{day}:{hour}:{net}",
        setup_id=setup,
        symbol="BTCUSDT",
        opened_at=opened,
        closed_at=opened + timedelta(minutes=30),
        net_pnl=Decimal(net),
        fees=Decimal("1"),
        risk_amount=Decimal(risk),
    )


def test_walk_forward_folds_are_contiguous_with_embargo() -> None:
    folds = walk_forward_folds(START, END, folds=4, embargo=timedelta(hours=1))
    assert len(folds) == 4
    assert folds[0].start == START + timedelta(hours=1)
    assert folds[-1].end == END
    for previous, current in zip(folds, folds[1:], strict=False):
        assert current.start == previous.end + timedelta(hours=1)
    with pytest.raises(ValueError, match="fold"):
        walk_forward_folds(START, END, folds=0, embargo=timedelta(0))


def test_report_counts_breakeven_expectancy_r_and_sample_flag() -> None:
    trades = [
        _trade(0, 9, "20"),  # +2R, London
        _trade(1, 14, "-10"),  # −1R, London (New York mở 14:30 UTC theo giờ EST)
        _trade(2, 15, "5"),  # +0,5R, London + New York
        _trade(3, 2, "-10"),  # −1R, phiên Á
    ]
    folds = walk_forward_folds(START, END, folds=4, embargo=timedelta(0))
    report = setup_report("h18-v1", trades, folds=folds)
    assert report["n"] == 4
    assert report["out_of_sample_n"] == 4
    assert report["win_rate"] == pytest.approx(0.5)
    # Hòa vốn = lỗ TB / (lãi TB + lỗ TB) = 10 / (12,5 + 10).
    assert report["breakeven_win_rate"] == pytest.approx(10 / 22.5)
    assert Decimal(report["expectancy_net"]) == Decimal("1.25")
    assert report["expectancy_r"] == pytest.approx(0.125)
    assert report["r_distribution"] == {"≤ −1R": 2, "−1R…0": 0, "0…1R": 1, "1R…2R": 0, "≥ 2R": 1}
    assert report["by_session"]["asia"]["n"] == 1
    assert report["by_session"]["london"]["n"] == 3
    assert report["by_session"]["new_york"]["n"] == 1
    assert report["by_hour_utc"]["9"]["n"] == 1
    assert [fold["n"] for fold in report["folds"]] == [4, 0, 0, 0]
    assert report["insufficient_sample"] is True
    assert report["setup_id"] == "h18-v1"


def test_report_excludes_trades_in_embargo_from_out_of_sample() -> None:
    folds = walk_forward_folds(START, END, folds=2, embargo=timedelta(days=1))
    # Fold 1 bắt đầu sau 1 ngày embargo; lệnh ngày 0 nằm trong embargo → không tính ngoài mẫu.
    report = setup_report("h18-v1", [_trade(0, 9, "5"), _trade(3, 9, "5")], folds=folds)
    assert report["n"] == 2
    assert report["out_of_sample_n"] == 1


def test_report_hash_is_deterministic_and_order_independent() -> None:
    trades = [_trade(0, 9, "20"), _trade(1, 14, "-10")]
    folds = walk_forward_folds(START, END, folds=2, embargo=timedelta(0))
    first = setup_report("h18-v1", trades, folds=folds)
    second = setup_report("h18-v1", list(reversed(trades)), folds=folds)
    assert first["report_hash"] == second["report_hash"]
    changed = setup_report("h18-v1", [*trades, _trade(2, 9, "1")], folds=folds)
    assert changed["report_hash"] != first["report_hash"]


def test_report_rejects_mixed_setups_and_non_positive_risk() -> None:
    folds = walk_forward_folds(START, END, folds=1, embargo=timedelta(0))
    with pytest.raises(ValueError, match="setup"):
        setup_report("h18-v1", [_trade(0, 9, "1", setup="h19-v1")], folds=folds)
    with pytest.raises(ValueError, match="rủi ro"):
        _trade(0, 9, "1", risk="0")


def _many(n: int, net: str) -> list[ScalpTrade]:
    return [_trade(day % 28, 9 + day // 28 % 8, net) for day in range(n)]


def test_verdict_requires_sample_and_positive_stress_expectancy() -> None:
    assert CRITERION_VERSION == "sig5-paper-criterion-v1"
    assert MIN_OUT_OF_SAMPLE_TRADES == 200
    folds = walk_forward_folds(START, END, folds=4, embargo=timedelta(0))
    base = setup_report("h18-v1", _many(220, "3"), folds=folds)
    stress_good = setup_report("h18-v1", _many(220, "1"), folds=folds)
    stress_bad = setup_report("h18-v1", _many(220, "-1"), folds=folds)
    small = setup_report("h18-v1", _many(50, "3"), folds=folds)

    ok = paper_enable_verdict(base=base, stress=stress_good)
    assert ok["recommend_paper"] is True
    assert ok["reasons"] == []
    assert ok["criterion_version"] == CRITERION_VERSION

    bad = paper_enable_verdict(base=base, stress=stress_bad)
    assert bad["recommend_paper"] is False
    assert "STRESS_EXPECTANCY_NOT_POSITIVE" in bad["reasons"]

    few = paper_enable_verdict(base=small, stress=small)
    assert few["recommend_paper"] is False
    assert "INSUFFICIENT_OUT_OF_SAMPLE_TRADES" in few["reasons"]
    with pytest.raises(ValueError, match="setup"):
        paper_enable_verdict(
            base=base,
            stress=setup_report("h19-v1", [_trade(0, 9, "1", setup="h19-v1")], folds=folds),
        )


def test_validate_release_on_replay_bundle_reports_both_cost_scenarios() -> None:
    from uuid import UUID

    from tests.fixtures.sig4_runtime_replay import build_sig4_bundle

    from trading_bot.application.scalping_validation import (
        BASE,
        STRESS,
        ScalpingValidationConfig,
        validate_release,
    )

    workspace = UUID("51650000-0000-4000-8000-000000000001")
    config = ScalpingValidationConfig(workspace_id=workspace, folds=2, embargo=timedelta(0))
    first = validate_release(build_sig4_bundle(workspace), release_id="h18-v1", config=config)
    second = validate_release(build_sig4_bundle(workspace), release_id="h18-v1", config=config)
    assert first["validation_hash"] == second["validation_hash"]  # cùng bundle → cùng báo cáo
    base = first["reports"][BASE.scenario_id]
    stress = first["reports"][STRESS.scenario_id]
    assert base["n"] == stress["n"] == 1
    # Lệnh H18 của fixture: TP1 1R + TP2 2R → R ròng dương nhưng < 1,5R sau phí và trượt giá.
    assert 0 < base["expectancy_r"] < 1.5
    assert Decimal(stress["expectancy_net"]) < Decimal(base["expectancy_net"])
    assert Decimal(stress["fees"]) > 0
    verdict = first["verdict"]
    assert verdict["recommend_paper"] is False
    assert verdict["reasons"] == ["INSUFFICIENT_OUT_OF_SAMPLE_TRADES"]
    assert first["scenarios"][STRESS.scenario_id]["slippage_rate"] == "0.0004"


def test_validate_release_chunks_merges_contiguous_windows_like_single_bundle() -> None:
    from uuid import UUID

    from tests.fixtures.sig4_runtime_replay import (
        EVALUATION_START,
        RANGE_END,
        START,
        sig4_bars,
    )

    from trading_bot.application.scalping_validation import (
        BASE,
        STRESS,
        ScalpingValidationConfig,
        build_research_bundle,
        validate_release,
        validate_release_chunks,
    )

    workspace = UUID("51650000-0000-4000-8000-000000000003")
    config = ScalpingValidationConfig(workspace_id=workspace, folds=2, embargo=timedelta(0))

    def bundle(evaluation_start: datetime, end: datetime):  # type: ignore[no-untyped-def]
        # Mỗi đoạn mang lại toàn bộ lịch sử khởi động trước evaluation_start (chồng lấn warmup).
        return build_research_bundle(
            {tf: [bar for bar in sig4_bars(tf) if bar.open_time < end] for tf in ("5m", "15m")},
            workspace_id=workspace,
            symbol="BTCUSDT",
            start=START,
            end=end,
            evaluation_start=evaluation_start,
            checked_at=RANGE_END,
        )

    middle = EVALUATION_START + timedelta(minutes=30)
    single = validate_release(
        bundle(EVALUATION_START, RANGE_END), release_id="h18-v1", config=config
    )
    chunked = validate_release_chunks(
        [bundle(EVALUATION_START, middle), bundle(middle, RANGE_END)],
        release_id="h18-v1",
        config=config,
    )
    for scenario in (BASE.scenario_id, STRESS.scenario_id):
        # Mã lệnh băm theo nội dung bundle nên khác giữa các đoạn; kinh tế của lệnh phải trùng.
        for key in ("n", "out_of_sample_n", "net_pnl", "fees", "expectancy_r", "by_hour_utc"):
            assert chunked["reports"][scenario][key] == single["reports"][scenario][key]
    assert chunked["reports"][BASE.scenario_id]["n"] == 1
    assert chunked["evaluation_start"] == single["evaluation_start"]
    assert chunked["range_end"] == single["range_end"]
    assert len(chunked["dataset_hashes"]) == 2
    assert len(chunked["validation_hash"]) == 64
    # Đoạn có khe hở hoặc chồng lấn khoảng đánh giá bị từ chối.
    with pytest.raises(ValueError, match="liền nhau"):
        validate_release_chunks(
            [
                bundle(EVALUATION_START, middle),
                bundle(middle + timedelta(minutes=15), RANGE_END),
            ],
            release_id="h18-v1",
            config=config,
        )
    with pytest.raises(ValueError, match="ít nhất một"):
        validate_release_chunks([], release_id="h18-v1", config=config)


def test_scalp_trades_refuses_rejected_replay_instead_of_reporting_zero_trades() -> None:
    from types import SimpleNamespace

    from trading_bot.application.backtest import ReplayRejected
    from trading_bot.application.scalping_validation import scalp_trades

    rejected = ReplayRejected.__new__(ReplayRejected)
    run = SimpleNamespace(
        sensitivity_run=SimpleNamespace(
            scenario=SimpleNamespace(scenario_id="base"), result=rejected
        ),
        trade_outcomes=(),
    )
    with pytest.raises(ValueError, match="bị từ chối"):
        scalp_trades(
            SimpleNamespace(runs=(run,)),  # type: ignore[arg-type]
            {"base": SimpleNamespace(positions=lambda: ())},  # type: ignore[dict-item]
            release_id="h18-v1",
            symbol="BTCUSDT",
        )


def test_replay_observation_window_matches_producer_and_keeps_full_history_default() -> None:
    from uuid import UUID

    from tests.fixtures.sig4_runtime_replay import build_sig4_bundle

    from trading_bot.application.backtest_dataset_replay_observations import (
        build_backtest_dataset_replay_observations,
    )

    bundle = build_sig4_bundle(UUID("51650000-0000-4000-8000-000000000002"))
    full = build_backtest_dataset_replay_observations(bundle, strategy_release_id="h18-v1")
    windowed = build_backtest_dataset_replay_observations(
        bundle, strategy_release_id="h18-v1", max_bars_per_timeframe=256
    )
    wide = build_backtest_dataset_replay_observations(
        bundle, strategy_release_id="h18-v1", max_bars_per_timeframe=10_000
    )
    assert [item.snapshot for item in wide] == [item.snapshot for item in full]
    lengths = [len(item.snapshot.get_series("5m").bars) for item in windowed]
    assert max(lengths) == 256
    assert all(
        item.snapshot.get_series("5m").bars[-1] == other.snapshot.get_series("5m").bars[-1]
        for item, other in zip(windowed, full, strict=True)
    )


def test_cli_chunk_windows_are_contiguous_and_cover_range() -> None:
    from scripts import sig5_scalping_validation as cli

    end = START + timedelta(days=10)
    windows = cli.chunk_windows(START, end, timedelta(days=4))
    assert windows == [
        (START, START + timedelta(days=4)),
        (START + timedelta(days=4), START + timedelta(days=8)),
        (START + timedelta(days=8), end),
    ]
    assert cli.chunk_windows(START, end, None) == [(START, end)]
    with pytest.raises(ValueError, match="dương"):
        cli.chunk_windows(START, end, timedelta(0))


def test_cli_validate_reads_local_files_and_writes_report(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    from scripts import sig5_scalping_validation as cli
    from tests.fixtures.sig4_runtime_replay import RANGE_END, START, sig4_bars

    for timeframe in ("5m", "15m"):
        rows = [cli.bar_to_row(bar) for bar in sig4_bars(timeframe)]
        # Nến trước --start trong file phải bị bỏ qua (file tải về thường rộng hơn khoảng chạy).
        earlier = dict(rows[0])
        step = cli.INTERVALS[timeframe]
        for key in ("open_time", "close_time"):
            earlier[key] = (datetime.fromisoformat(rows[0][key]) - step).isoformat()
        rows.insert(0, earlier)
        (tmp_path / f"BTCUSDT-{timeframe}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    restored = cli.load_bars(tmp_path, "BTCUSDT", "5m")
    assert [bar.close for bar in restored[1:]] == [bar.close for bar in sig4_bars("5m")]
    assert restored[1].taker_buy_base_volume == sig4_bars("5m")[0].taker_buy_base_volume
    out = tmp_path / "report.json"
    assert (
        cli.main(
            [
                "validate",
                "--data-dir",
                str(tmp_path),
                "--symbols",
                "BTCUSDT",
                "--releases",
                "h18-v1",
                "--start",
                START.replace(tzinfo=None).isoformat(),
                "--end",
                RANGE_END.replace(tzinfo=None).isoformat(),
                "--warmup-hours",
                "21",
                "--folds",
                "2",
                "--embargo-minutes",
                "0",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    item = report["h18-v1:BTCUSDT"]
    assert item["reports"]["base"]["n"] == 1
    assert item["verdict"]["recommend_paper"] is False
    assert len(item["validation_hash"]) == 64


def test_backtest_replay_executor_uses_producer_window() -> None:
    """CRY-236: replay job dùng đúng cửa sổ nến của producer live (tránh O(n²) theo lịch sử)."""
    from trading_bot.application import backtest_replay_executor as executor
    from trading_bot.application.market_signal_composition import PRODUCER_MAX_BARS_PER_STREAM

    assert executor.REPLAY_MAX_BARS_PER_TIMEFRAME == PRODUCER_MAX_BARS_PER_STREAM
    assert executor.REPLAY_CONTEXT_PROVIDER.endswith("window256")


def test_sig5_execution_uses_1m_bars_when_available(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """CRY-415: mặc định execution_interval=1m và CLI dùng 1m khi có file."""
    import json
    from decimal import Decimal
    from typing import Any

    from scripts import sig5_scalping_validation as cli
    from tests.fixtures.sig4_runtime_replay import RANGE_END, START, sig4_bars

    from trading_bot.application import scalping_validation as sv
    from trading_bot.domain.market.models import OHLCVBar

    assert sv.ScalpingValidationConfig(workspace_id=cli.WORKSPACE).execution_interval == "1m"

    bars_5m = sig4_bars("5m")
    bars_15m = sig4_bars("15m")
    bars_1m: list[OHLCVBar] = []
    for bar in bars_5m:
        for idx in range(5):
            opened = bar.open_time + timedelta(minutes=idx)
            closed = opened + timedelta(minutes=1) - timedelta(milliseconds=1)
            bars_1m.append(
                OHLCVBar(
                    symbol=bar.symbol,
                    timeframe="1m",
                    open_time=opened,
                    close_time=closed,
                    event_at=closed,
                    received_at=closed,
                    available_at=closed,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume / Decimal("5"),
                    quote_volume=(
                        None if bar.quote_volume is None else bar.quote_volume / Decimal("5")
                    ),
                    trade_count=bar.trade_count,
                    taker_buy_base_volume=(
                        None
                        if bar.taker_buy_base_volume is None
                        else bar.taker_buy_base_volume / Decimal("5")
                    ),
                    taker_buy_quote_volume=(
                        None
                        if bar.taker_buy_quote_volume is None
                        else bar.taker_buy_quote_volume / Decimal("5")
                    ),
                    source=bar.source,
                    is_closed=True,
                )
            )

    observed_exec_intervals: list[str] = []
    observed_confirmations: list[str] = []
    real_replay = sv.run_backtest_dataset_runtime_replay

    def _spy_replay(*args: Any, **kwargs: Any) -> Any:
        observed_exec_intervals.append(str(kwargs.get("execution_interval")))
        seed_factory = kwargs["context_seed_factory"]
        seed = seed_factory(kwargs["scenarios"][0], None)
        observed_confirmations.append(
            str(seed.published_settings.settings.strategy.confirmation_timeframe)
        )
        return real_replay(*args, **kwargs)

    monkeypatch.setattr(sv, "run_backtest_dataset_runtime_replay", _spy_replay)

    bundle = sv.build_research_bundle(
        {"1m": bars_1m, "5m": bars_5m, "15m": bars_15m},
        workspace_id=cli.WORKSPACE,
        symbol="BTCUSDT",
        start=START,
        end=RANGE_END,
        evaluation_start=START + timedelta(hours=21),
        checked_at=RANGE_END,
    )
    comparison, _ = sv.run_release_replay(
        bundle,
        release_id="h18-v1",
        config=sv.ScalpingValidationConfig(workspace_id=cli.WORKSPACE),
    )
    assert len(comparison.runs) >= 1
    assert observed_exec_intervals[-1] == "1m"
    assert observed_confirmations[-1] == "5m"

    for tf, tf_bars in (("1m", bars_1m), ("5m", bars_5m), ("15m", bars_15m)):
        (tmp_path / f"BTCUSDT-{tf}.jsonl").write_text(
            "".join(json.dumps(cli.bar_to_row(b)) + "\n" for b in tf_bars),
            encoding="utf-8",
        )
    out = tmp_path / "report-1m.json"
    assert (
        cli.main(
            [
                "validate",
                "--data-dir",
                str(tmp_path),
                "--symbols",
                "BTCUSDT",
                "--releases",
                "h18-v1",
                "--start",
                START.replace(tzinfo=None).isoformat(),
                "--end",
                RANGE_END.replace(tzinfo=None).isoformat(),
                "--warmup-hours",
                "21",
                "--folds",
                "2",
                "--embargo-minutes",
                "0",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert observed_exec_intervals[-1] == "1m"
    assert out.is_file()


def test_validation_body_records_execution_interval() -> None:
    """CRY-414 module review: báo cáo phải cho biết khung execution đã dùng."""
    from uuid import UUID

    from tests.fixtures.sig4_runtime_replay import build_sig4_bundle

    from trading_bot.application.scalping_validation import (
        ScalpingValidationConfig,
        validate_release,
    )

    workspace = UUID("51650000-0000-4000-8000-000000000001")
    config = ScalpingValidationConfig(workspace_id=workspace, folds=2, embargo=timedelta(0))
    body = validate_release(build_sig4_bundle(workspace), release_id="h18-v1", config=config)
    assert body["execution_interval"] == "1m"
