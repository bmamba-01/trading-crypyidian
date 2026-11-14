# Rà soát API Binance USDⓈ-M và độ phủ wrapper (2026-11-14)

**Ngày kiểm tra:** 2026-11-14. **Mã được rà:** nhánh `dev` tại commit `c4d77a0`, thư mục `src/trading_bot/adapters/binance_usdm/`.
**Phạm vi:** dữ liệu thị trường (market data) phục vụ đo đạc và phát hiện tín hiệu (signal). Không gọi endpoint ký (signed), không đặt lệnh.
Tệp này không chứa credential; key nằm ở `.secrets/` (đã gitignore).

## Nguồn đã kiểm tra

| Nguồn | Nội dung dùng |
|---|---|
| [General info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info) | Base URL, rate limit, mã lỗi |
| [REST market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data) | Danh sách endpoint, weight |
| [WebSocket market streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams) và [Connect](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Connect) | Stream, route `/public` `/market` `/private` |
| [Change log](https://developers.binance.com/docs/derivatives/change-log) | Thay đổi 2025-01 → 2026-10 |

Trang reference mới bị cắt sau mục RPI Order Book nên weight của nhóm ticker, tỷ lệ long/short và trading schedule lấy từ legacy docs (Binance ghi là không còn bảo trì); cần đọc lại trước khi ghim vào code.

## Kết quả gọi thử trực tiếp

- `GET /fapi/v1/exchangeInfo` mainnet: 946 symbol, trong đó 565 `PERPETUAL` và 237 `TRADIFI_PERPETUAL` đang `TRADING`, 137 `PERPETUAL` `SETTLING`; quote gồm USDT 899, USDC 41, USD1 3, U 2, BTC 1. `REQUEST_WEIGHT` 2400/phút.
- Demo (`demo-fapi.binance.com`): 744 symbol, `REQUEST_WEIGHT` 6000/phút.
- `ticker/price` v2, `premiumIndex` và 23 endpoint market data công khai khác trả 200 trên mainnet. `/fapi/v1/allForceOrders` trả 404 (không còn tồn tại).
- API key được cấp là key **mainnet**: `historicalTrades` (chỉ cần key) trả 200 trên `fapi.binance.com` và `-2015` trên `demo-fapi.binance.com`.

## Độ phủ REST market data

| Endpoint | Wrapper | Ghi chú |
|---|---|---|
| `/fapi/v1/exchangeInfo` | Có | Bỏ qua `onboardDate`, `deliveryDate`, `underlyingType`, `PERCENT_PRICE`, `MAX_NUM_ORDERS` |
| `/fapi/v1/klines` | Có | Parser bỏ số trade, taker buy base/quote (cột 8–10) |
| `/fapi/v1/fundingRate` | Có | Đã đọc `rateType` |
| `/fapi/v1/openInterest`, `/futures/data/openInterestHist` | Có | Lịch sử chỉ 1 tháng |
| `/fapi/v1/premiumIndex`, `fundingInfo` | Thiếu | Mark/index price, funding dự kiến, chu kỳ và cap funding theo symbol |
| `/fapi/v1/ticker/24hr`, `ticker/price`, `ticker/bookTicker` | Thiếu | Giá hiện tại, spread, xếp hạng thanh khoản universe |
| `/fapi/v1/depth`, `rpiDepth` | Thiếu | L2 cho H14 và mô hình slippage |
| `/fapi/v1/trades`, `aggTrades`, `historicalTrades` | Thiếu | Taker flow/CVD cho H12–H13 |
| `markPriceKlines`, `indexPriceKlines`, `premiumIndexKlines`, `continuousKlines` | Thiếu | Stop theo mark, basis |
| `topLongShortPositionRatio`, `topLongShortAccountRatio`, `globalLongShortAccountRatio`, `takerlongshortRatio`, `basis` | Thiếu | Crowding (H10), flow (H12); chỉ lưu 30 ngày nên cần thu thập liên tục |
| `symbolAdlRisk`, `insuranceBalance`, `constituents`, `tradingSchedule`, `indexInfo`, `assetIndex` | Thiếu | Rủi ro và lịch phiên TradFi perp |

### Cập nhật 2026-11-14 (vòng 2)

Adapter đã có thêm `get_premium_index`, `get_price_tickers` (v2), `get_book_tickers`, `get_24h_tickers`, `get_funding_info`, `get_long_short_ratio` (ba loại), `get_taker_buy_sell_volume` và `get_kline_flows` (số trade, taker buy volume của nến). Các hàng tương ứng trong bảng trên không còn "Thiếu" ở tầng wrapper; dữ liệu này **chưa** được lưu vào PostgreSQL hay đưa vào feature. Vẫn thiếu: `depth`, `rpiDepth`, `trades`, `aggTrades`, `historicalTrades`, các kline mark/index/premium, `basis`, `symbolAdlRisk`, `tradingSchedule`.

## Độ phủ WebSocket market stream

Chưa có client WebSocket nào cho market data, và `pyproject.toml` chưa có thư viện WebSocket. Thiếu toàn bộ: `@kline_*`, `@markPrice`, `@aggTrade`, `@bookTicker`, `@depth*`, `@forceOrder`, `!contractInfo`, `!ticker@arr`, `tradingSession`.
`@forceOrder` là nguồn duy nhất cho dữ liệu thanh lý (liquidation) vì REST không còn endpoint công khai; từ 2026-04-14 stream chỉ đẩy lệnh lớn nhất mỗi 1000 ms.

## Lệch so với tài liệu hiện hành

1. Từ 2026-03-05 WebSocket tách route: `/public` (book, depth), `/market` (kline, aggTrade, markPrice, forceOrder), `/private` (user data). URL cũ `/ws` và `/stream` chỉ còn nhận dữ liệu `/public` và có thông báo ngừng từ 2026-04-23. `gateway_boundary.py` và `demo_target.py` chỉ kiểm host, chưa mô hình hóa route.
2. `PublicRequestLimiter` mặc định 2400/phút và tự cập nhật từ `exchangeInfo`; khớp mainnet. Giới hạn 500 lần/5 phút của `fundingRate` dùng chung với `fundingInfo`.
3. Lệnh điều kiện đã được mô hình hóa theo Algo Service (`algoOrder`, `ALGO_UPDATE`, mã `-4120`); khớp change log 2025-11-06.

## Khoảng trống về đấu nối

Trạng thái dưới đây là tại commit `c4d77a0`, trước khi có worker ingest live (xem mục việc đề xuất).

- `BinanceUsdMMarketDataAdapter` chỉ được khởi tạo trong test; không có process nào trong `app.py` hay `cli/` gọi nó.
- Service `ingestion-worker` trong `compose.yaml` chạy `ResearchRightsMaintenanceWorker`, không thu nến.
- `market-signal-worker` đọc `market_bar` nguồn `BINANCE_USDM` từ PostgreSQL nhưng chưa có luồng live nào ghi nguồn đó; `append_funding_rate` và `append_open_interest` chỉ được gọi từ test và script seed.
- Không có code đọc `.secrets/binance_api_key`; `SecretProvider.resolve_for_gateway` chưa có nơi dùng cho market data.

## Kết luận

Wrapper đủ cho backfill nến của H01, H03, H06 (chỉ cần OHLCV 15m/1h/4h). Chưa đủ để đo đạc và bắt tín hiệu thời gian thực, và chưa đủ dữ liệu cho H10, H12, H13, H14.

## Việc đề xuất theo thứ tự

1. ~~Worker ingest live: nến đóng qua `/market` `@kline_*` kèm REST lấp khoảng trống, ghi `market_bar` nguồn `BINANCE_USDM`.~~ Đã làm ngày 2026-11-14 (`market_stream.py`, `live_kline_ingestion.py`, `live_market_ingestion_worker.py`); mặc định tắt, bật bằng `TRADING_BOT_INGESTION_LIVE_SYMBOLS`.
2. Giữ cột taker buy và số trade của kline; thêm `premiumIndex`, `fundingInfo`, `ticker/24hr`, `bookTicker`.
3. Thu thập định kỳ OI, funding, tỷ lệ long/short, taker ratio, basis vì sàn chỉ giữ 30 ngày.
4. Stream `@markPrice`, `@forceOrder`, `@aggTrade`; L2 depth chỉ khi làm H14.
5. Mô hình hóa route `/public` `/market` `/private` trong allowlist WebSocket.
