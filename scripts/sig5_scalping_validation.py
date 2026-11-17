"""SIG-5: tải nến REST công khai (tùy chọn) và chạy kiểm định scalping H18–H20 trên file cục bộ.

Hai lệnh:

- ``fetch``: gọi ``GET /fapi/v1/klines`` công khai của Binance USDⓈ-M qua adapter dự án (không
  khóa API, không endpoint ký) và lưu các trường kline vào ``<out>/<SYMBOL>-<interval>.jsonl``.
  **Chỉ chạy khi owner đồng ý tải dữ liệu.**
- ``validate``: chỉ đọc file đã lưu, dựng bundle 5m/15m, replay PAPER từng release ở kịch bản chi
  phí gốc và xấu, ghi báo cáo JSON (``validation_hash`` theo release × mã) và in bảng tóm tắt.

Nến lịch sử được coi là khả dụng ngay khi đóng (``available_at = close_time``) — đúng với replay
point-in-time; không dùng giá đóng của nến đang chạy.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

INTERVALS = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15), "1m": timedelta(minutes=1)}
WORKSPACE = UUID("51650000-0000-4000-8000-00000000c0de")
# Quy cách từ GET /fapi/v1/exchangeInfo công khai (kiểm 16/11/2026): tick, bước khối lượng, notional
# tối thiểu. Mã ngoài bảng dùng mặc định của ScalpingValidationConfig.
EXCHANGE_RULES = {
    "BTCUSDT": ("0.1", "0.001", "50"),
    "ETHUSDT": ("0.01", "0.001", "20"),
}
FIELDS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def bar_to_row(bar: Any) -> dict[str, Any]:
    return {
        "open_time": bar.open_time.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
        "close_time": bar.close_time.isoformat(),
        "quote_volume": None if bar.quote_volume is None else str(bar.quote_volume),
        "trade_count": bar.trade_count,
        "taker_buy_base_volume": (
            None if bar.taker_buy_base_volume is None else str(bar.taker_buy_base_volume)
        ),
        "taker_buy_quote_volume": (
            None if bar.taker_buy_quote_volume is None else str(bar.taker_buy_quote_volume)
        ),
    }


def row_to_bar(row: dict[str, Any], *, symbol: str, timeframe: str) -> Any:
    from trading_bot.domain.market.models import OHLCVBar

    opened = datetime.fromisoformat(row["open_time"])
    closed = datetime.fromisoformat(row["close_time"])
    cursor = f"{symbol}:{timeframe}:{row['open_time']}"
    return OHLCVBar(
        symbol=symbol,
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed,
        open=Decimal(row["open"]),
        high=Decimal(row["high"]),
        low=Decimal(row["low"]),
        close=Decimal(row["close"]),
        volume=Decimal(row["volume"]),
        quote_volume=None if row["quote_volume"] is None else Decimal(row["quote_volume"]),
        trade_count=row["trade_count"],
        taker_buy_base_volume=(
            None if row["taker_buy_base_volume"] is None else Decimal(row["taker_buy_base_volume"])
        ),
        taker_buy_quote_volume=(
            None
            if row["taker_buy_quote_volume"] is None
            else Decimal(row["taker_buy_quote_volume"])
        ),
        is_closed=True,
        source="BINANCE_USDM_REST_BACKFILL",
        source_cursor=cursor,
        checksum=hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(),
    )


def load_bars(data_dir: Path, symbol: str, timeframe: str) -> list[Any]:
    path = data_dir / f"{symbol}-{timeframe}.jsonl"
    with path.open(encoding="utf-8") as handle:
        return [
            row_to_bar(json.loads(line), symbol=symbol, timeframe=timeframe)
            for line in handle
            if line.strip()
        ]


async def fetch(symbols: Sequence[str], start: datetime, end: datetime, out: Path) -> None:
    from trading_bot.adapters.binance_usdm.market_data import BinanceUsdMMarketDataAdapter

    out.mkdir(parents=True, exist_ok=True)
    async with BinanceUsdMMarketDataAdapter() as adapter:
        for symbol in symbols:
            for timeframe in ("5m", "15m"):
                interval = INTERVALS[timeframe]
                cursor = start
                rows: list[dict[str, Any]] = []
                while cursor < end:
                    page_end = min(end, cursor + interval * 1500) - timedelta(milliseconds=1)
                    page = await adapter.get_klines(
                        symbol=symbol,
                        interval=timeframe,
                        start_time=cursor,
                        end_time=page_end,
                        limit=1500,
                    )
                    closed = [bar for bar in page.records if bar.is_closed]
                    rows.extend(bar_to_row(bar) for bar in closed)
                    cursor = page_end + timedelta(milliseconds=1)
                path = out / f"{symbol}-{timeframe}.jsonl"
                path.write_text(
                    "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                    encoding="utf-8",
                )
                print(f"{symbol} {timeframe}: {len(rows)} nến → {path}", file=sys.stderr)


def validate(
    data_dir: Path,
    symbols: Sequence[str],
    releases: Sequence[str],
    start: datetime,
    end: datetime,
    warmup: timedelta,
    folds: int,
    embargo: timedelta = timedelta(hours=1),
    chunk: timedelta | None = None,
) -> dict[str, Any]:
    from trading_bot.application.scalping_validation import (
        ScalpingValidationConfig,
        build_research_bundle,
        validate_release_chunks,
    )
    from trading_bot.domain.risk.planner import ExchangeRules

    checked_at = datetime.now(UTC).replace(microsecond=0)
    results: dict[str, Any] = {}
    for symbol in symbols:
        config = ScalpingValidationConfig(workspace_id=WORKSPACE, folds=folds, embargo=embargo)
        if symbol in EXCHANGE_RULES:
            tick, step, minimum = EXCHANGE_RULES[symbol]
            config = ScalpingValidationConfig(
                workspace_id=WORKSPACE,
                folds=folds,
                embargo=embargo,
                exchange=ExchangeRules(
                    tick_size=Decimal(tick),
                    quantity_step=Decimal(step),
                    contract_multiplier=Decimal("1"),
                    min_notional=Decimal(minimum),
                    max_notional=Decimal("10000"),
                    max_quantity=Decimal("1000"),
                ),
            )
        bars = {
            timeframe: [
                bar
                for bar in load_bars(data_dir, symbol, timeframe)
                if start <= bar.open_time < end
            ]
            for timeframe in ("5m", "15m")
        }
        windows = chunk_windows(start + warmup, end, chunk)

        def bundles(
            symbol: str = symbol,
            bars: dict[str, list[Any]] = bars,
            windows: list[tuple[datetime, datetime]] = windows,
        ) -> Iterator[Any]:
            # Mỗi đoạn mang ``warmup`` lịch sử trước khoảng đánh giá của nó; tạo dần để chỉ giữ
            # một đoạn (và quan sát của nó) trong bộ nhớ.
            for index, (evaluation_start, window_end) in enumerate(windows, start=1):
                window_start = max(start, evaluation_start - warmup)
                print(
                    f"  đoạn {index}/{len(windows)}: {evaluation_start:%Y-%m-%d}", file=sys.stderr
                )
                yield build_research_bundle(
                    {
                        timeframe: [
                            bar for bar in items if window_start <= bar.open_time < window_end
                        ]
                        for timeframe, items in bars.items()
                    },
                    workspace_id=WORKSPACE,
                    symbol=symbol,
                    start=window_start,
                    end=window_end,
                    evaluation_start=evaluation_start,
                    checked_at=checked_at,
                )

        for release_id in releases:
            print(f"Đang kiểm định {release_id} trên {symbol}…", file=sys.stderr)
            results[f"{release_id}:{symbol}"] = validate_release_chunks(
                bundles(), release_id=release_id, config=config
            )
    return results


def chunk_windows(
    evaluation_start: datetime, end: datetime, chunk: timedelta | None
) -> list[tuple[datetime, datetime]]:
    """Chia khoảng đánh giá thành các đoạn liền nhau dài ``chunk`` (None = một đoạn)."""
    if chunk is None:
        return [(evaluation_start, end)]
    if chunk <= timedelta(0):
        raise ValueError("Độ dài đoạn phải dương.")
    windows: list[tuple[datetime, datetime]] = []
    cursor = evaluation_start
    while cursor < end:
        windows.append((cursor, min(end, cursor + chunk)))
        cursor += chunk
    return windows


def summary(results: dict[str, Any]) -> str:
    lines = [
        "release:mã | n ngoài mẫu (xấu) | thắng % | hòa vốn % | kỳ vọng R gốc | kỳ vọng ròng xấu"
        " | đề xuất PAPER | lý do"
    ]
    for key, item in sorted(results.items()):
        base = item["reports"]["base"]
        stress = item["reports"]["stress-slippage-x2-taker"]
        verdict = item["verdict"]

        def pct(value: float | None) -> str:
            return "—" if value is None else f"{value * 100:.1f}"

        lines.append(
            f"{key} | {stress['out_of_sample_n']} | {pct(stress['win_rate'])} | "
            f"{pct(stress['breakeven_win_rate'])} | "
            f"{'—' if base['expectancy_r'] is None else f'{base["expectancy_r"]:.3f}'} | "
            f"{stress['expectancy_net'] or '—'} | {'CÓ' if verdict['recommend_paper'] else 'KHÔNG'}"
            f" | {', '.join(verdict['reasons']) or '—'}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_parser = sub.add_parser("fetch")
    fetch_parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    fetch_parser.add_argument("--start", required=True)
    fetch_parser.add_argument("--end", required=True)
    fetch_parser.add_argument("--out", type=Path, required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--data-dir", type=Path, required=True)
    validate_parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    validate_parser.add_argument("--releases", nargs="+", default=["h18-v1", "h19-v1", "h20-v1"])
    validate_parser.add_argument("--start", required=True)
    validate_parser.add_argument("--end", required=True)
    validate_parser.add_argument("--warmup-hours", type=int, default=24)
    validate_parser.add_argument("--folds", type=int, default=4)
    validate_parser.add_argument("--embargo-minutes", type=int, default=60)
    validate_parser.add_argument(
        "--chunk-days",
        type=int,
        default=None,
        help="Replay theo đoạn N ngày rồi gộp lệnh để giới hạn RAM (mặc định: một đoạn).",
    )
    validate_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    start, end = _utc(args.start), _utc(args.end)
    if args.command == "fetch":
        asyncio.run(fetch(args.symbols, start, end, args.out))
        return 0
    results = validate(
        args.data_dir,
        args.symbols,
        args.releases,
        start,
        end,
        timedelta(hours=args.warmup_hours),
        args.folds,
        timedelta(minutes=args.embargo_minutes),
        None if args.chunk_days is None else timedelta(days=args.chunk_days),
    )
    args.out.write_text(
        json.dumps(results, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8"
    )
    print(summary(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
