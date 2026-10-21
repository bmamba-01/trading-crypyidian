# Dữ liệu futures, vĩ mô và sự kiện cho bot USDT-M

**Ngày chốt nghiên cứu: 07/10/2026, Asia/Saigon.** Đây là đặc tả dữ liệu và giả thuyết tác động, không dự báo hướng giá hiện tại. Source calendar có thể đổi; bot phải lưu từng phiên bản đã nhận. Xem [danh mục chiến lược](./strategies-2026.md) để biết rule cụ thể.

## 1. Vai trò của macro trong crypto

Giá phản ứng với phần **khác kỳ vọng**, vị thế sẵn có và thanh khoản tại thời điểm tin, không chỉ với mức số liệu. Lạm phát cao hơn consensus có thể làm yields/USD tăng và tài sản rủi ro giảm; nhưng phản ứng có thể đảo khi thị trường đã định giá, growth shock trội hơn inflation shock hoặc có tin crypto riêng. Payroll yếu có thể tăng kỳ vọng giảm lãi suất, đồng thời làm lo suy thoái. VIX cao đo volatility kỳ vọng cổ phiếu, không tự xác định hướng BTC. Mọi dấu tác động trong bảng là giả thuyết điều kiện để kiểm định.

Tương quan cũng thay theo thời kỳ: [Liu & Tsyvinski (2018)](https://www.nber.org/papers/w24877) tìm cấu trúc return crypto khác tài sản truyền thống trên mẫu của họ; [Che và cộng sự, IMF The Crypto Cycle and US Monetary Policy (04/08/2023)](https://www.imf.org/en/publications/wp/issues/2023/08/04/the-crypto-cycle-and-us-monetary-policy-534834) nghiên cứu mức đồng biến equity tăng và kênh Fed tightening. Không dùng một correlation toàn lịch sử làm quy luật bất biến.

[Stablecoin Shocks, IMF WP2026/044 (06/03/2026)](https://www.imf.org/en/publications/wp/issues/2026/03/06/stablecoin-shocks-574528) nghiên cứu cú sốc cầu được nhận dạng bằng tin và SVAR-IV, thấy lan truyền sang Treasury/USD/crypto/equity. Thay đổi supply thô chịu mint, burn, chuyển chain và hoạt động thanh toán; không tương đương “tiền mới sẽ mua BTC”.

## 2. Contract thời gian point-in-time

Mỗi record cần những trường riêng:

```text
series_id, source, venue, symbol, units, frequency, timezone
observation_start, observation_end       # kỳ được đo, không phải lúc biết
scheduled_release_at                    # lịch dự kiến, có revision
source_published_at, source_event_at     # giờ công bố và event của nguồn
received_at, ingested_at                 # clock của hệ thống
available_at                            # lúc pipeline thực sự có thể sử dụng
vintage_id, revision_seq, value, raw_payload_hash
quality_flags, license_id, retrieval_url, parser_version
```

Join feature vào decision bằng `available_at<=decision_time`; chọn vintage gần nhất **đã có**, không chọn giá trị final hôm nay. Data lấy bù muộn lưu received_at thật; chỉ gán historical availability khi có bằng chứng archive/official release time, và ghi rõ giả định latency. ALFRED chứa vintage theo ngày; nếu cần giao dịch trong giây công bố, date-only vintage không đủ. [ALFRED API](https://fred.stlouisfed.org/docs/api/fred/alfred.html) lưu release/revision; [real-time periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html) phân biệt thông tin biết lúc đó với dữ liệu hiện tại.

Chuẩn thời gian nội bộ UTC, calendar US dùng `America/New_York` để tự xử lý DST. Việt Nam: 08:30 ET là 19:30 khi EDT và 20:30 khi EST; 14:00 ET là 01:00 hoặc 02:00 ngày tiếp theo tại Việt Nam. Không hardcode một offset UTC cho cả năm. Đồng bộ clock; lưu cả monotonic latency nếu có. Không forward-fill giữa weekend thành tín hiệu “không đổi”: lưu age/market_open/stale flags.

## 3. Bảng nguồn vĩ mô và tài sản truyền thống

Chi phí “miễn phí” trong bảng là quyền đọc nguồn công khai, không cam kết license thương mại hay latency thấp. Phải kiểm tra terms của nguồn gốc, quyền lưu, display, non-display và phân phối. Public website không tự cấp quyền streaming/redistribution.

| Feature | Nguồn gốc và tần suất | Khi biết, revisions | Công thức/ứng dụng đề xuất và hướng điều kiện | Chi phí/giới hạn |
|---|---|---|---|---|
| CPI headline/core | [BLS calendar2026](https://www.bls.gov/schedule/2026/home.htm), release tháng | Thường08:30 ET theo lịch cụ thể; seasonal series sửa lại. [BLS seasonal adjustment](https://www.bls.gov/cpi/seasonal-adjustment/) | `surprise=actual-consensus`; z surprise theo các releases trước; headline/core mom và yoy riêng. Inflation surprise dương chỉ nghiêng risk-off khi yields/USD xác nhận | Release/API public; historical consensus timestamp thường licensed. Không dùng revised seasonal series cho signal cũ |
| PCE headline/core, personal spending | [BEA release](https://www.bea.gov/news/2026/personal-income-and-outlays-august-2026) và [calendar](https://www.bea.gov/news/schedule), tháng |08:30 ET theo release; sửa tháng/annual update; tháng đo xảy ra trước ngày công bố | Surprise core mom, `core_yoy`; spending giúp tách growth/inflation. Release30/09/2026 có annual update từ01/2021, cần vintage | BEA public; API latest không thay point-in-time archive. Consensus không do BEA cung cấp |
| NFP, unemployment, wages | [BLS Employment Situation](https://www.bls.gov/news.release/empsit.htm), tháng |08:30 ET theo calendar, không giả định luôn thứ Sáu đầu tháng; initial và revisions/benchmark | `surprise_nfp`; unemployment surprise; wage mom; `revision_sum` hai tháng trước phải là số vừa công bố. Sign phụ thuộc growth/fed regime | Public actual; consensus có license. [BLS technical notes](https://www.bls.gov/web/empsit/cestn.htm) cho revisions; lưu payroll và household datasets riêng |
| FOMC target range, statement, SEP, press conference, minutes | [Fed meeting calendars](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm); scheduled + unscheduled | Statement giờ ghi trên release; [16/09/2026 statement](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm); [meeting materials](https://www.federalreserve.gov/monetarypolicy/fomcpresconf20260916.htm). Minutes/transcript không có đồng thời statement | `target_mid=(lower+upper)/2`, change và surprise vs ex-ante probability; text features có model/version/token hash; statement và press conference là hai events | Public document; real-time ingestion phụ thuộc network. Không dùng transcript công bố sau đó ở event time |
| Kỳ vọng lãi suất | [CME FedWatch methodology](https://www.cmegroup.com/articles/2023/understanding-the-cme-group-fedwatch-tool-methodology.html), futures-derived | Intraday snapshot có timestamp; end-of-day không đủ cho trước FOMC. Các giả định như increments25bps phải ghi version | `E(rate)=Σp_k*rate_k`, expected change; chênh giữa decision và expected rate. Không lấy final FedWatch screenshot cho trước tin | [FedWatch](https://www.cmegroup.com/fedwatch) public view; API/data/history và non-display usage kiểm tra [CME licensing](https://www.cmegroup.com/market-data/license-data.html) |
| US Treasury nominal2Y/10Y, real10Y, slope | [Treasury yield methodology](https://home.treasury.gov/policy-issues/financing-the-government/interest-rate-statistics/treasury-yield-curve-methodology); daily | Indicative inputs gần15:30 ET; thường public gần18:00 ET, có thể chậm. Không biết daily value lúc08:30 event | `Δy2`, `Δy10`, real yield change, `slope=y10-y2` tính bps, không percent return; rising real yield là conditional risk headwind | Daily official rẻ; tick yields/Treasury futures cần vendor/exchange feed. Daily curves không phải tradable execution quotes |
| USD DXY/USDX | [ICE USDX](https://www.ice.com/forex/usdx), [methodology](https://www.ice.com/publicdocs/data/ICE_FX_Indexes_Methodology.pdf), intraday theo feed | Exchange timestamps và publish/receive riêng; nghỉ phiên/weekend; futures roll khác cash index | `ret_DXY`, rolling z return, interaction với yields. USD tăng có thể giảm appetite risk, cần kiểm định beta | ICE index/market data proprietary; license non-display/historical. Không dùng Yahoo chart làm archive đáng tin cho micro-event |
| USD broad index thay thế chậm | [Fed H.10 indexes](https://www.federalreserve.gov/releases/h10/summary/), [FRED DTWEXBGS](https://fred.stlouisfed.org/series/DTWEXBGS) | Observation daily nhưng publication schedule riêng, không mặc định same-day instant | Dùng regime ngày/tuần với release age. **Không phải DXY**, basket và weighting khác | Public series; snapshot revisions và source terms. Không dùng như proxy tick DXY |
| Equity risk: S&P500/Nasdaq100 futures hoặc index | CME equity contracts/feeds; official index administrator/vendor theo entitlement | US cash calendar, futures session nghỉ hàng ngày; settlement khác last trade; return weekly/weekend mismatch phải xử lý | `ret_ES`, `ret_NQ`, rolling crypto beta/corr, residual crypto return; beta dùng trailing matched timestamps | Live tick và continuous futures history thường licensed; [CME data](https://www.cmegroup.com/market-data/license-data.html). Roll phải lưu contract ID, không lấy adjusted series làm trade price |
| VIX và volatility term structure | [Cboe VIX FAQ](https://www.cboe.com/tradable_products/vix/faqs) và [methodology](https://cdn.cboe.com/api/global/us_indices/governance/VIX_Methodology.pdf) | Index từ SPX option quotes, đo kỳ vọng30d; Cboe session khác crypto24/7 | `ΔVIX`, level percentile, optional VIX futures1/2 slope. Risk-off filter hoặc sizing, không direct short BTC chỉ vì VIX tăng | Cboe index/futures feeds theo license. Spot VIX không phải VIX futures; weekend/stale không là zero volatility |
| Fed balance sheet/reserves | [H.4.1](https://www.federalreserve.gov/releases/h41/), tuần | Thường thứ Năm16:30; holiday có thể chuyển ngày. Report date và observation date khác | Δassets, reserves, past4weeks; feature liquidity regime chậm, không đồng nhất QE announcement với reserves change | Public releases; archive bản công bố. Không nội suy tuần thành thông tin đã biết từng ngày |
| Retail sales/growth hỗ trợ | [Census release schedule](https://www.census.gov/retail/release_schedule.html), tháng; GDP từBEA | Advance sales08:30 theo lịch; revisions sau. GDP advance/second/third khác vintages | Surprise sales/GDP, revision; giúp phân loại bad-growth vs hot-inflation | Public actual/calendar; consensus và revisions snapshot cần pipeline. Optional cho v1 |

Nguồn lịch cập nhật trực tiếp tốt hơn quy tắc “thứ Sáu đầu tháng” vì ngày có thể đổi. Ví dụ tại ngày chốt: [BLS](https://www.bls.gov/schedule/2026/home.htm) ghi CPI tháng09 công bố14/10/2026 lúc08:30 ET; [BEA](https://www.bea.gov/news/2026/personal-income-and-outlays-august-2026) ghi PCE tháng09 công bố29/10/2026 lúc08:30 EDT; [Fed calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) ghi cuộc họp27–28/10/2026. Đây là **lịch đã công bố**, không phải số liệu tương lai đã biết.

## 4. ETF và stablecoin/on-chain

| Feature | Nguồn và công thức | Availability cần lưu | Caveat/chi phí và giả thuyết |
|---|---|---|---|
| Spot BTC/ETH ETF creations/redemptions | Holdings, shares outstanding, NAV từ từng issuer; ví dụ [iShares IBIT](https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf). `flow_proxy_t=(shares_t-shares_prev)*NAV_t` | As-of date không phải publication time. Ghi received_at mỗi file; cuối ngày Mỹ/chậm tới ngày sau tùy issuer, không hardcode giờ. Snapshot mọi issuer, không chỉ một quỹ | Proxy creation/redemption, không “volume giao dịch ETF = flow”. ΔAUM gồm price change; holdings changes còn fee/cash/in-kind. Public files nhưng archive/licensing phải kiểm tra; flow dương có thể hỗ trợ demand hoặc chỉ hedge/arbitrage |
| Total ETF flow và intensity | `Σflow_proxy` trên universe issuer lịch sử; `flow/AUM_prev` hoặc `flow/spot_quote_volume` | Chỉ total khi mọi thành phần có availability phù hợp; incomplete flag, không backfill giờ sớm từ bảng tổng hợp cuối ngày | Thiếu fund, corrections, methodology vendor và changing baskets. SEC periodic reports đáng tin audit nhưng quá chậm cho signal ngày; không coi total proxy là issuer official total |
| USDC net issuance | [Circle transparency](https://www.circle.com/transparency) disclose tuần reserve/mint-burn, assurance tháng. `net=issued-redeemed`, Δcirculation7d/30d | Report reference date, publication time, file receive time riêng; weekly reserve không dùng như intraday mint feed | Tín hiệu liquidity chậm; supply mở rộng còn payment/cross-chain demand. Public issuer report là self disclosure/assurance phạm vi riêng; không gọi assurance là bảo đảm alpha |
| USDT circulation | [Tether transparency](https://tether.to/en/transparency/) nói metrics thường cập nhật ngày. `Δln(net_circulation)` và chain breakdown | Lưu snapshot và `as_of`, publication time nếu có; “typically daily” không là SLA | Authorized-not-issued/treasury inventory khác circulation; chain swaps có thể inflate mint/burn gross. Public read; không suy mint chắc mua BTC |
| On-chain balances, issuance/transfer | Canonical chain RPC/contract events, block height/hash/finality; provider ví dụ [Coin Metrics API](https://docs.coinmetrics.io/api/v4/) | `block_time`, `observed_at`, confirmations/finality; reorg tạo revision. Vendor daily metrics có calculation/publish lag | Bridge wrapped supply tránh double-count. Labels exchange wallet là heuristic, có đổi lịch sử. [Community tier](https://gitbook-docs.coinmetrics.io/packages/coin-metrics-community-data) subset/limit/license; Pro cần mua, API software license không đồng nghĩa dataset license |
| Exchange netflow/balance, BTC dominance, crypto breadth | Own chain calculations hoặc licensed vendor with methodology; breadth từ universe Binance lịch sử | Wallet-label version và observed cutoff; cap/supply data publication lag; constituent version | Exchange transfer không tự là intent sell; internal wallets/OTC/custody làm nhiễu. Dominance market cap không giống futures tradable index. Optional, không prerequisite v1 |

Không lấy biểu đồ aggregate nguồn thứ ba làm “dữ liệu gốc ETF” nếu chưa có phương pháp và license. Công thức flow proxy được tài liệu này đề xuất; nếu vendor có series flows chính thức theo phương pháp khác, giữ series_id riêng và reconciliation, không ghi đè.

## 5. Binance futures features và dữ liệu execution

Các URL cũ chuyển về [Binance USD-M market data catalog](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data) tại ngày chốt. Những giới hạn nguồn đã xác nhận: OI history chỉ tháng gần nhất, taker volume và basis30 ngày; OI timestamp là cuối kỳ nhưng taker/basis timestamp là đầu kỳ. Funding history có settlement time; fundingInfo báo điều chỉnh interval/cap/floor. Dữ liệu liquidation stream chỉ snapshot mới nhất mỗi1000ms, **không phải toàn bộ liquidation tape**. [Nguồn liquidation](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Liquidation-Order-Streams).

| Feature | Endpoint/nguồn | Công thức nghiên cứu | Availability, unit, giới hạn |
|---|---|---|---|
| OHLCV contract, taker volume nến | `/fapi/v1/klines`; [archive chính chủ](https://github.com/binance/binance-public-data) | returns/logreturns; volume z; `sell_quote=total_quote-taker_buy_quote`; `TI=(buy_quote-sell_quote)/total_quote` | Chỉ nến đóng và nhận; preserve open/close time, corrections/archive checksum. Base vs quote volume không trộn; dữ liệu thiếu không tạo nến volume0 để trade |
| Mark/index/premium | `/fapi/v1/premiumIndex`, `markPriceKlines`, `indexPriceKlines`, `premiumIndexKlines` | `premium=(mark-index)/index`; `contract_mark_gap=(last-mark)/mark` | Mark cho valuation/trigger policy, contract orderbook cho fills. Thời điểm các giá phải đồng bộ; index composite không là spot executable |
| Funding đã settlement | `/fapi/v1/fundingRate` | Hour-normalized rate=`f/actual_interval_hours`; PnL funding theo vị thế tại mỗi settlement | `fundingTime` không tiết lộ rate cuối ngay từ đầu interval. Tách settled series với observed estimated/last rate; tránh leak next settled funding |
| Funding snapshot/schedule | `premiumIndex` snapshot và `/fapi/v1/fundingInfo` | carry expectation, time-to-next-funding; cap-adjust flag | Archive từng observed rate và nextFundingTime; cấu hình interval thay đổi phải effective-dated. Không hardcode mọi symbol8h; annualization dùng schedule thực |
| OI current/history | `/fapi/v1/openInterest`, `/futures/data/openInterestHist` | `Δln(OI_units)`; OI notional/volume/liquidity ratio | Base/contract units và USD value riêng; notional tăng có thể chỉ do price. Timestamp cuối kỳ; polling receive time mới là availability thực |
| Taker volume history | `/futures/data/takerlongshortRatio` | signed imbalance, CVD, price-flow divergence | Timestamp đầu kỳ không cho phép dùng ngay đầu kỳ; đợi end+publish/receive lag. Validate units và sum match nến/trades |
| Perp/dated basis | `/futures/data/basis` hoặc synchronous book/index snapshots | Perp basis=`(perp-spot)/spot`; dated=`ln(F/S)*365/days_to_expiry` | Annualized dated basis không thay funding annualized. Metadata expiry/contractType cần đúng; API timestamp đầu kỳ phải gate tới available_at |
| Public liquidation snapshots | `<symbol>@forceOrder`, `!forceOrder@arr` | Sampled signed notional, percentile by past window, shock flag | Throttle tạo lower-information sample; side SELL thường là forced sell, không user opening short. Không cộng thành “tổng liquidation thị trường”; nhiều venue coverage khác |
| Signed trades | aggTrades/stream và public archive | `m=true` buyer maker => aggressive sell; CVD/volume bars | Aggregate record không đủ queue history; event/receive clocks riêng, ID dedup. API online retention không thay lịch sử dài; checksum archive |
| L2/depth/BBO | REST snapshot + sequenced diff stream, bookTicker | spread, depth1/5/10, imbalance, microprice=`(ask*bid_qty+bid*ask_qty)/(bid_qty+ask_qty)` | Gap/out-of-order book mất validity cho tới resync; hidden/RPI depth không nên coi fully visible. L2 archive storage/vendor tốn phí; BBO không suy được queue priority |
| Long/short account/position ratio | Global/top trader ratio endpoints | Crowding feature, z-score time series | Account count và notional position tỷ lệ khác khái niệm; không đại diện toàn thị trường hay retail/institution trực tiếp. Không cộng long/short ratio nhiều venue thiếu weights |
| Executions/position/funding ledger | Private order/fill/account/income events, REST reconciliation | Actual fee/slippage/realized PnL, hedge mismatch, exposure | Account-specific; lưu update IDs/recv time, reconcile snapshots. Market-data estimate không thay cashflow ledger; stop trigger/fill/position update khác events |

Ý nghĩa kinh tế của funding/basis tham khảo [BIS Crypto carry (2023, sửa10/2025)](https://www.bis.org/publ/work1087.pdf); kết quả historical crash prediction trong nguồn không cấp quyền short tự động mỗi khi funding cao. [Funding-Aware Optimal Market Making2026](https://arxiv.org/abs/2605.06405) là nghiên cứu DEX/mô phỏng, chỉ hỗ trợ xem funding như state variable cho inventory.

## 6. Feature engineering để dùng được trong bot

Các công thức dưới là thiết kế nghiên cứu do tài liệu đề xuất, không kết quả thực nghiệm đã đạt:

1. **Surprise:** `s=(actual_initial-consensus_asof_before_release)/sd(previous_surprises)`. Consensus snapshot phải có cutoff và số contributor/methodology; denominator train-only. Không có consensus hợp lệ thì giữ actual feature và missing flag, không gọi actual-minus-previous là surprise.
2. **Revisions:** `revision_t=value_previous_period_in_current_release-value_previous_period_as_first_known`; giữ revision_signal riêng với current_actual. Thông tin revision chỉ available ở release hiện tại.
3. **Rate expectation:** probability series và actual target change có unit bps; meeting-month Fed Funds futures phản ánh average daily rate, cần phương pháp lịch CME, không `100-price` rồi gán trực tiếp cho target của cuộc họp.
4. **Regime:** rolling90d BTC/equity corr, US2Y/real-yield change, broad USD/DXY return, VIX percentile; không biến mọi feature thành cùng tần suất giả. Include age và stale flags; fit model/routing chỉ lịch sử trước decision.
5. **Funding:** preserve raw settlement rate, interval, cumulative funding24h/7d, latest estimate snapshot, basis và time-to-settlement. `annualized_simple=Σfunding_past_window * year_duration/window_duration` là mô tả quá khứ, không forward yield cam kết; không ghép thêm compounding tự động.
6. **OI:** `dOI=ln(OI_t/OI_prev)` theo units; compare với return và taker imbalance. OI chỉ đo số vị thế mở, mỗi contract có hai phía; không riêng lượng “long inflow”.
7. **ETF/stablecoin:** net flow/lagged AUM, trailing7d flow, change of circulation, net issuance; no forward-fill future publication. Model lag1–3 ngày là choice nghiên cứu phải khóa trước test.
8. **Cross-market beta:** `beta=cov(r_crypto,r_equity)/var(r_equity)` trên timestamp matched; `residual=r_crypto-beta*r_equity`; không dùng daily closing prices chưa biết lúc intraday. Equity feed stale thì không hedge theo extrapolated price.

Dữ liệu tin/news: lưu raw text, canonical URL, authored_at/published_at/updated_at/received_at, timezone, publisher/source, dedup và hashes; version LLM/extractor. Social/news scraped hôm nay chứa outcome tương lai so với backtest cũ; historical retrieval phải chứng minh thời điểm document tồn tại. LLM có knowledge leakage riêng; timestamp-aware prompt không đủ loại bỏ training-memory contamination.

## 7. Event policy và kiểm soát chất lượng

Phiên bản đầu dùng calendar như **risk gate**: CPI/PCE/NFP/FOMC blackout -15m đến+5m; press conference gate riêng. Các ngưỡng là lựa chọn thiết kế cần sensitivity, không chuẩn bắt buộc thị trường. Tại release, chỉ feature `RECEIVED_VALID` được đưa vào model. Delay, parse failure hoặc actual/consensus mâu thuẫn: `UNKNOWN_EVENT_OUTCOME`, vẫn giữ blackout, không fill actual0. Calendar revisions/unscheduled Fed statement tạo event mới có recorded_at; không thêm vào lịch sử như đã biết từ đầu.

State cho mỗi series: `FRESH`, `STALE`, `MISSING`, `REVISED`, `QUARANTINED`. Strategy chỉ nhận feature đúng frequency/units/availability; risk engine vẫn hoạt động khi macro collector lỗi. Required feed thiếu thì tắt entry strategy đó. Optional feed thiếu xử lý bằng mô hình đã huấn luyện với missing flag, không tự đổi logic sau một outage.

Quality checks cần đặc tả khi triển khai: duplicate event/ID, monotonic timestamp, observation_end<=available_at theo ngữ nghĩa, impossible values/units, source-vendor reconciliation, contract status/onboard/delist, OI count-vs-value, candle trade sum, book sequence gap, schema drift, checksums và source terms expiry. Điều kiện timestamp đầu kỳ cho taker/basis phải dùng period completion; test đúng case này khi code được triển khai. Tài liệu hiện tại không đã thực thi collector hay những checks đó.

UI nên hiển thị table freshness: value, units, observation period, published/received, age, revision, source, quality; calendar overlay trên chart với UTC và giờ Việt Nam. Funding chart tách settled/estimated, OI base/notional tách series, liquidation đặt nhãn sampled. User đọc được “model không đủ dữ liệu” thay vì một score có vẻ chắc chắn.

## 8. Chi phí và roadmap dữ liệu

| Giai đoạn | Dataset tối thiểu | Điều kiện mở rộng |
|---|---|---|
| V1 nghiên cứu OHLCV | Binance candles/trades archive, mark/index, funding settlements, symbol metadata lịch sử; BLS/BEA/Fed calendar snapshots | H01/H03/H06, event blackout; không cần mua consensus hoặc DXY tick ngay |
| V2 futures features | OI/taker/basis snapshots với long history, account fee/funding ledger, production BBO | Archive online từ hôm nay hoặc vendor verified; không suy lịch sử nhiều năm từ REST retention một tháng |
| V3 macro daily | Official releases+ALFRED vintages, Treasury/H.10/H.4.1, issuer ETF files, Circle/Tether snapshots | Phiên bản slow regime; actual publication time và revisions đủ; không dùng cho tin trong giây |
| V4 event/intraday | Historical consensus point-in-time, licensed rates/DXY/equity/VIX feed, latency logs | Net benefit phải vượt subscription+turnover cost, attribution ablation xác nhận feature hữu ích |
| V5 microstructure | Full L2 snapshots/deltas, historical BBO/trades, queue-aware replay/capacity, infrastructure latency đo được | H14/market making chỉ đủ bằng chứng execution mới canary; candle backtest không thay L2 |

Chưa chốt giá subscription, license, coverage và archive của vendor tại ngày chốt; không nêu con số chi phí giả. Dữ liệu free cho nghiên cứu có thể hạn chế dùng thương mại, hiển thị hoặc phân phối. Số liệu kinh tế official public có thể lấy chậm qua website/API; đây không phải feed cam kết trước thị trường. Ngân sách tính cả storage, data recovery, clock monitoring, parser maintenance và lịch sử revisions, không chỉ API request.

## 9. Giới hạn nghiên cứu

Nguồn official xác nhận lịch/methodology/fields, không xác nhận quan hệ hướng giá bảo đảm. Chưa tải/tái lập series lịch sử, audit vendor hoặc đo real-time publication lag. Một số nguồn public (issuer/chain) thiếu historical publication timestamps; cần archive hoặc assumptions ghi rõ để backtest hợp lệ. Quan hệ macro cần out-of-sample theo regime, shock attribution và price confirmation; không quy “CPI cao = short”, “ETF inflow = long” thành rule tuyệt đối. Mọi điều kiện áp dụng phải đánh giá net fees/funding/slippage cùng độ trễ.
