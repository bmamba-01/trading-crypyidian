# Đối chiếu tín hiệu kiểu "radarastro", 3 chỉ báo TradingView và scalping — nghiên cứu 15/11/2026

**Phạm vi:** đối chiếu cách các app/chỉ báo bên ngoài (owner cung cấp ảnh ngày 15/11/2026) đưa ra tín hiệu với năng lực hiện có của Crypidian; đánh giá bằng chứng cho scalping; chọn chart miễn phí giống TradingView. **Kế hoạch triển khai:** [WBS SIG](../superpowers/plans/2026-11-15-sig-market-structure-scalping-chart-wbs.md). Thuật ngữ mới ở [glossary mục AA](./glossary-vi.md#aa-cấu-trúc-thị-trường-phiên-thanh-khoản-và-scalping-15112026).

> Nguồn trader/mạng xã hội (tweet "3 chỉ báo cho chart 5m", app radarastro) là **ý kiến**, không phải bằng chứng. Mô tả chỉ báo của tác giả TradingView là tài liệu kỹ thuật; chính tác giả ghi không phải tín hiệu và không cam kết hiệu suất.

## 1. Tóm tắt cho người quản lý

- **Crypidian chưa đưa ra được tín hiệu giống ảnh.** Lõi có sẵn (dữ liệu point-in-time, 3 chiến lược H01/H03/H06, planner + risk gate, chart Lightweight Charts, Telegram signal-only) nhưng thiếu: khung 1m/5m trong runtime, dữ liệu mua/bán chủ động, cấu trúc thị trường (BOS/CHoCH, trendline), phiên Á/Âu/Mỹ và quét thanh khoản, volume profile, vùng vào lệnh, nhiều mức chốt lời, "độ tin cậy" có hiệu chuẩn, và chưa có nguồn nến trực tiếp.
- **Chart:** giữ **TradingView Lightweight Charts** (đã tự host bản 5.2.1, giấy phép Apache-2.0). Bản 5.3.0 có ví dụ chính thức cho volume profile, tô phiên, hình chữ nhật và trendline nên vẽ được gần như ảnh TradingView mà không vi phạm CSP. Không dùng Advanced Charts/widget của TradingView (giấy phép chỉ cho web công khai, widget không hiện được tín hiệu của mình).
- **Scalping khả thi về kỹ thuật nhưng rủi ro chi phí rất cao.** Với phí taker 0,05%/chiều, stop 0,25% và chốt 1,5R, tỷ lệ thắng hòa vốn tăng từ 40% lên ~56%. Bot chỉ nên thử scalping ở PAPER, ưu tiên lệnh limit (maker), stop ≥ 3 lần chi phí khứ hồi, chạy trong phiên London/New York, và chỉ bật sau backtest có số mẫu đủ.
- **"Độ tin cậy 82%" và "3/6 nguồn đồng thuận" trên ảnh là con số không kiểm chứng được.** Crypidian sẽ hiện hạng (cao/trung bình/thấp) cho tới khi có xác suất đã hiệu chuẩn bằng dữ liệu walk-forward; đếm nguồn phải trừ các nguồn tương quan (dùng `AntiConsensusAnalyzer` đang bị bỏ quên).

## 2. Ảnh owner cung cấp — tín hiệu gồm những gì

| Thành phần trên ảnh | Ý nghĩa | Crypidian hiện có? | Bằng chứng trong code | Gói kế hoạch |
| --- | --- | --- | --- | --- |
| Khung M1, M3, M5, M10, M15, M30, H1, H4, D1 | Chọn khung phân tích | **Một phần.** Config nhận 1m–1d (`config/workspace_settings.py:23`), nhưng runtime chỉ chạy 15m/1h/4h (`application/strategy_market_requirements.py:13-19`); chart admin có 5m/15m/1h/4h/1d (`application/bot_reporting.py:62-68`). M10 không phải interval Binance. | — | SIG-2 |
| Kiểu lệnh SCALPING / SWING / DCA | Hồ sơ thời gian giữ lệnh | **Một phần.** H06 15m giữ ≤16 nến gần với "intraday"; H01/H03 1h là swing. Chưa có scalping (M1–M5); DCA không thuộc phạm vi (tăng rủi ro, xung đột với hard cap). | `domain/strategy/rules.py` | SIG-4 |
| Bộ não "AI ASTRO" / "Nội bộ" | Hai bộ phân tích | **Khác hướng.** Crypidian dùng luật xác định + scorer AI chỉ được veto/abstain (khóa SHADOW/PAPER_GATE). Không để AI tự sinh lệnh. | `domain/learning/scoring.py:25-31` | Giữ nguyên |
| MUA/BÁN + vùng vào (Entry zone) | Hướng và khoảng giá vào | **Một phần.** Có hướng và giá tham chiếu = giá đóng nến; chưa có vùng vào. Card L6 có zone nhưng mất khi chuyển qua `planner_bridge.py:30-112`. | `domain/strategy/contracts.py:366-387` | SIG-3 |
| SL, TP1, TP2, TP3 | Cắt lỗ và thang chốt lời | **Một phần.** `TradePlan` có một stop và **một** target (`domain/risk/planner.py:266-305`); PAPER chưa có chốt lời từng phần theo thang. | — | SIG-3 |
| R:R (TP2) 1:1.8 | Tỷ lệ lời/lỗ | **Có**, tốt hơn ảnh: planner tính R:R **ròng sau phí** và chặn < 1,0 (FX). Lưu ý ảnh có lỗi logic: lệnh BÁN nhưng TP3 3964.93 nằm **trên** vùng vào — Crypidian phải kiểm thứ tự giá theo hướng. | `domain/risk/planner.py` | SIG-3 (kiểm thứ tự) |
| Độ tin cậy 82% / 75% | Xác suất thắng? | **Không** ở contract chiến lược; card pipeline có `confidence = trigger score` nhưng bị bỏ khi qua bridge. Chưa có hiệu chuẩn cho tín hiệu (WP06 có temperature calibration cho scorer). | `strategy_pipeline/pipeline_engine.py:287` | SIG-6 |
| "3/6 nguồn đồng thuận" + checklist (xu hướng đa khung, cá mập 60/15 phút, sổ lệnh lệch, khung M15, lý thuyết Dow) | Bỏ phiếu đồng thuận | **Không.** Có `gate_details`/`detail_vi` cho lý do; `AntiConsensusAnalyzer` (`domain/strategy_card/orthogonality.py:96`) được khởi tạo nhưng không gọi, `has_consensus_warning` gán cứng `False`. | `admin/strategy_card_service.py:88,122` | SIG-6, SIG-8 (MI-1) |
| Điểm xu hướng −97, MACD −94, RSI 18, dòng tiền −100 | Chỉ báo chuẩn hóa | **Một phần.** Có RSI, ADX, EMA, ATR, Bollinger, z-score, VWAP ngày, ER (`domain/market/features.py`). **Không có** MACD, dòng tiền (delta/CVD), OBV. | grep 0 kết quả | SIG-1 |
| Cá mập 60/15 phút (whale flow) | Lệnh lớn | **Không.** Không thu aggTrades; trường taker buy của kline bị bỏ khi parse (`adapters/binance_usdm/market_data.py:496-530`). | — | SIG-0, SIG-1 |
| Sổ lệnh lệch / tường lệnh | Order book imbalance | **Không** và **không đề xuất làm ngay**: không backtest được nếu chưa lưu sổ lệnh (đã ghi ở khảo sát 14/11 §3.2). | — | Hoãn (H14) |
| Monte Carlo 4 giờ | Mô phỏng giá | **Không.** Monte Carlo trên bước ngẫu nhiên chỉ tái hiện biến động, không cho thông tin hướng. Chỉ chấp nhận làm "dải biến động dự kiến", không làm lý do vào lệnh. | — | SIG-9 (tùy chọn) |
| Vùng giải thích bằng câu | Diễn giải | **Một phần:** `detail_vi`, popup chiến lược, Microscope. Chưa có checklist có cấu trúc (MI-1) hay tường thuật từ sự kiện (MI-6). | — | SIG-8 |
| Chart TradingView với volume profile theo phiên | Trực quan | **Một phần:** chart nến + entry/SL/TP + marker fill (`static/trade-chart.js:191-294`); chưa có volume, chỉ báo phủ, phiên, profile. | — | SIG-7 |

## 3. Ba chỉ báo TradingView và cách Crypidian cài lại

**Giấy phép.** Cả ba script gắn nhãn "open-source"; quy định TradingView mặc định MPL-2.0 cho script mã mở và House Rules hạn chế tái sử dụng ([script publishing rules](https://www.tradingview.com/support/solutions/43000590599-script-publishing-rules/), kiểm 15/11/2026; chưa mở header mã từng script). **Quyết định:** không chép hay dịch máy mã Pine; cài lại "clean-room" từ khái niệm công khai, ghi rõ trong docstring.

### 3.1 Auto Trendlines & Market Structure MTF → cấu trúc

Theo [trang mô tả](https://www.tradingview.com/script/YhyU49QG-Auto-Trendlines-Market-Structure-MTF/): pivot có lookback thích ứng theo ATR; trendline nối 2 pivot cùng phía gần nhất, loại nếu pivot cũ vượt line > 0,3 ATR, vô hiệu khi đóng cửa vượt > 0,5 ATR; 7 trạng thái cấu trúc theo dấu độ dốc hai line; bảng đa khung dùng giá trị khung lớn đã xác nhận (lùi 1 nến).

Crypidian:
- **Pivot đã xác nhận:** đỉnh tại t nếu `high[t] > high[t-i]` (i=1..L) và `high[t] >= high[t+j]` (j=1..R); chỉ dùng được từ `confirmed_at = close_time[t+R]`. Tái dùng `domain/strategy/geometry.py:43` (đã có pivot nhưng chưa ai gọi).
- **Nhãn swing** HH/HL/LH/LL; **BOS** = đóng cửa vượt swing cùng chiều xu hướng; **CHoCH** = lần đóng cửa đầu tiên vượt swing ngược chiều (máy trạng thái). Chọn cố định "phá bằng giá đóng", không dùng râu.
- **Trendline tự động** từ 2 pivot đã xác nhận, độ dốc theo ATR/nến, dung sai/vô hiệu theo ATR như trên (tham số, không chép mã).
- **Đa khung:** chỉ dùng nến khung lớn đã đóng (shift 1).

### 3.2 Session Liquidity Architecture → quét thanh khoản

Theo [trang mô tả](https://www.tradingview.com/script/TVsSOe65/): phiên Tokyo 09:00–18:00 JST, London 08:00–17:00 giờ Anh, New York 09:30–16:00 ET theo timezone IANA (tự xử lý DST); mỗi mức đỉnh/đáy phiên và đỉnh/đáy ngày trước có máy trạng thái **armed → swept → rejected/accepted**, hoặc **expired**; xếp hạng độ rộng phiên theo phân vị; thống kê tần suất kèm cỡ mẫu.

Crypidian:
- Lịch phiên bằng `zoneinfo` (Asia/Tokyo, Europe/London, America/New_York) đổi sang UTC theo từng ngày; **không** cứng giờ UTC.
- Mức thanh khoản: đỉnh/đáy từng phiên, PDH/PDL (ngày UTC trước), PWH/PWL (tuần UTC trước), đỉnh/đáy bằng nhau (≥2 pivot chênh ≤ k·ATR).
- **Sweep** = râu vượt mức và đóng cửa quay lại trong cùng nến (hoặc reclaim trong N nến, tham số cố định trước khi backtest).
- Hiện "base rate" kèm `n` và cờ "chưa đủ mẫu", giống cách tác giả làm — phù hợp chuẩn trung thực của dự án.

### 3.3 Volume Pressure Atlas → áp lực mua/bán

Theo [trang mô tả](https://kr.tradingview.com/script/w7Biyqnb-Volume-Pressure-Atlas-MQLSoftware/): giả định đường đi trong nến (nến tăng O→L→H→C, giảm O→H→L→C), chia volume đều theo quãng đường thành phần "đi lên" U và "đi xuống" D cho từng hàng giá; lõi = 2·min(U,D), cánh = |U−D|, mất cân bằng E = |U−D|/(U+D), chuẩn Eref = Σ|U−D|/Σ(U+D); "hoạt động cao" ≥ 1,25× trung bình, "mất cân bằng thấp" E ≤ Eref/2, "cao" E ≥ 2·Eref; atlas một ngày chỉ chốt ở nến đầu ngày sau. Tác giả nói rõ đây **không** phải order flow thật.

Crypidian làm tốt hơn nhờ dữ liệu Binance:
- Kline USDⓈ-M có trường `[9]` taker buy base volume ([Binance Kline docs](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data)) ⇒ delta mỗi nến = 2·takerBuy − volume là **dữ liệu thật** của sàn, không phải giả định đường đi. Hiện adapter **bỏ** trường này → SIG-0 phải lưu.
- Phân bổ volume và delta của nến vào các bin giá vẫn là xấp xỉ (theo đường đi OHLC); bản chính xác theo giá cần aggTrades (`isBuyerMaker`), REST chỉ giữ 48 giờ nên phải thu liên tục nếu muốn.
- Volume profile: N bin, POC, Value Area 70% mở rộng từ POC; dùng profile của **phiên/ngày đã đóng**, hoặc bản "developing" tính tới nến t−1.

## 4. Bằng chứng cho scalping

| Nguồn | Phát hiện | Cách dùng |
| --- | --- | --- |
| [Cont, Kukanov & Stoikov 2014](https://arxiv.org/abs/1011.6402) | Biến động giá ngắn hạn tuyến tính theo order flow imbalance (OFI) ở mức giá tốt nhất — quan hệ **đồng thời**, không phải dự báo | Không coi delta là "tín hiệu dự báo"; dùng làm xác nhận |
| [arXiv 2602.00776](https://arxiv.org/pdf/2602.00776) (Binance perpetual, 1 giây, 2022–2025) | Tác động OFI đơn điệu, lõm ở cực trị; spread rộng làm giảm khả năng dự báo | Lọc theo spread/thanh khoản |
| [Easley, O'Hara, Yang & Zhang 2024](https://stoye.economics.cornell.edu/docs/Easley_ssrn-4814346.pdf); phản biện Andersen & Bondarenko | VPIN/Roll có sức dự báo trên crypto; bị phản biện là không thêm thông tin khi đã kiểm soát volume/biến động | Chưa đưa vào; ghi giả thuyết |
| [Eross et al. 2019](https://econpapers.repec.org/RePEc:eee:riibaf:v:49:y:2019:i:c:p:71-81); Baur et al. 2019 | Biến động/khối lượng theo giờ có chu kỳ bền; bất thường lợi suất theo giờ không bền | **Phiên quyết định biến động, không quyết định hướng** → dùng phiên làm bộ lọc giờ chạy, không để đoán Long/Short |
| [Wen, Bouri, Xu & Zhao 2022](https://ideas.repec.org/a/eee/ecofin/v62y2022ics1062940822000833.html) | Momentum/reversal trong ngày trên crypto | Chưa đọc bản đầy đủ — không trích kết luận |

**Phí và hòa vốn** ([Binance FAQ phí futures](https://www.binance.com/en/support/faq/360033544231), cập nhật 01/05/2026, ghi là mức ví dụ; bảng phí trực tiếp chưa xác nhận): VIP0 USDⓈ-M maker 0,02%, taker 0,05%, giảm 10% khi trả BNB. Funding mặc định 8 giờ, có symbol 4h/2h/1h ([thông báo Binance](https://www.binance.com/en/support/announcement/3243c81a35bd4f0c86a37315c3dc96cc)).

Phép tính (ước lượng, trượt giá 0,01–0,02%/chiều cần đo trên PAPER): chi phí khứ hồi taker ≈ 0,12–0,14%.

| Stop | Chi phí tính theo R | Chốt 1,5R: lãi ròng / lỗ ròng | Tỷ lệ thắng hòa vốn |
| --- | --- | --- | --- |
| 0,25% | ≈ 0,4R | 1,1R / 1,4R | ≈ 56% (không phí: 40%) |
| 0,15% | ≈ 0,67R | 0,83R / 1,67R | ≈ 67% |

⇒ Ràng buộc cứng cho scalping: **stop ≥ 3× chi phí khứ hồi**, ưu tiên limit post-only (maker), chốt ≥ 2R hoặc chỉ chạy khi ATR cao, và planner tiếp tục chặn R:R ròng < 1,0.

**Nhìn trước / vẽ lại:** pivot chỉ biết sau R nến; khung lớn phải đã đóng; profile chỉ dùng phiên đã đóng hoặc bản tới t−1; fill ở giá mở nến sau, không dùng high/low của chính nến tín hiệu; test "chạy tăng dần (streaming) = chạy cả lô (batch)" bắt buộc cho mọi feature mới.

## 5. Đánh giá tín hiệu kiểu radarastro

- **Hợp lý:** luôn có SL/TP và R:R; xem nhiều khung; dùng order flow có cơ sở (OFI).
- **Mang tính quảng cáo:** "độ tin cậy %" không có định nghĩa hay dữ liệu hiệu chuẩn; "3/6 nguồn" là bỏ phiếu không trọng số giữa các nguồn tương quan mạnh (xu hướng, MACD, Dow, M15 đều đo momentum) nên thổi phồng độ chắc chắn; Monte Carlo bước ngẫu nhiên không cho thông tin hướng; không công bố track record và chi phí; ảnh 1 có TP3 sai phía.
- **Cách Crypidian làm "độ tin cậy" trung thực:** nhãn rõ = P(chạm TP trước SL trong H nến) từ outcome đã lưu; mô hình xác suất (logistic, hiệu chuẩn isotonic/temperature) fit walk-forward; báo Brier score, log-loss, reliability diagram 10 bin kèm n từng bin; chỉ hiện % khi đạt ngưỡng hiệu chuẩn, nếu chưa thì hiện hạng. Chỉ phát tín hiệu khi p > p_hòa_vốn + biên an toàn. Đếm nguồn đồng thuận dùng **số nguồn độc lập hiệu dụng** sau khi trừ tương quan.

## 6. Bộ setup scalping đề xuất (giả thuyết, chỉ PAPER)

Đánh số tiếp chuỗi giả thuyết của [strategies-2026](./strategies-2026.md) (H01–H17). Tất cả `EXPERIMENTAL_UNVALIDATED`, không được tuyên bố lợi nhuận.

| ID | Setup | Khung | Vào | SL | TP | Vô hiệu | Dữ liệu |
| --- | --- | --- | --- | --- | --- | --- | --- |
| H18 | Quét + lấy lại mức phiên/PDH/PDL | M5, bối cảnh M15 | Mức đang armed; râu vượt và đóng quay lại; delta nến đảo dấu ngược hướng quét. Limit tại mức lấy lại hoặc giá mở nến sau | Cực trị râu + 0,1·ATR | TP1 giữa phiên/VWAP; TP2 cực trị đối diện của phiên | Đóng cửa vượt lại mức quét; không đạt 1R sau 12 nến | Kline 5m + taker buy, lịch phiên |
| H19 | Hồi về VWAP/POC trong vùng đi ngang | M1–M5 | Chế độ RANGE (tái dùng gate H06); lệch ≥ 2σ khỏi VWAP phiên hoặc ngoài Value Area ngày trước; mất cân bằng ở bin biên giảm dần | 1·ATR ngoài biên | POC hoặc VWAP | BOS M15 cùng hướng lệch; chế độ đổi sang TREND | Kline 1m, profile ngày |
| H20 | BOS + retest có mất cân bằng khối lượng | M5, xu hướng M15 cùng chiều | BOS bằng giá đóng với \|delta\| ≥ phân vị 80; bin quanh mức phá có E ≥ 2·Eref; limit tại vùng retest | Dưới/trên swing gần nhất | 2R hoặc mức thanh khoản kế tiếp | CHoCH ngược; retest đóng sâu > 0,5 ATR | Kline 5m + taker buy |

**Bộ lọc chung:** chỉ chạy trong London/New York; bỏ ±10 phút quanh mốc funding; stop ≥ 3× chi phí khứ hồi; mô phỏng limit fill bảo thủ (chỉ khớp khi giá đi xuyên qua mức ít nhất 1 tick, ghi giới hạn hàng đợi); báo base rate + n từng setup trước khi bật.

## 7. Chart miễn phí giống TradingView

| Lựa chọn | Phiên bản / ngày | Giấy phép | Kết luận |
| --- | --- | --- | --- |
| **TradingView Lightweight Charts** | 5.3.0 (npm 10/11/2026); repo đang tự host 5.2.1 | Apache-2.0, phải ghi TradingView là tác giả (NOTICE + link) | **Chọn.** ~62 KB gzip, canvas, IIFE standalone; v5 có multi-pane, `createSeriesMarkers`, price line, custom series, series/pane primitives; ví dụ chính thức Volume Profile, Rectangle, Trend Line (chỉ là ví dụ, chép kèm header Apache), gói `@tradingview/lwc-plugin-session-highlighting` 1.0.0 |
| TradingView Advanced Charts / widget | — | Chỉ cho web công khai, phải xin; widget chỉ hiện dữ liệu TradingView | Loại: admin nội bộ, không hiện được tín hiệu riêng, vi phạm "không bên thứ ba lúc chạy" |
| KLineChart | 10.1.0 (14/10/2026) | Apache-2.0 | Dự phòng: 27 chỉ báo có sẵn nhưng giao diện khác TradingView, v10 mới |
| Apache ECharts | 6.1.0 | Apache-2.0 | Loại: ~369 KB gzip, cảm giác dashboard |
| uPlot | 1.6.32 | MIT | Loại: không có nến |
| Highcharts Stock | 13.1.1 | Thương mại | Loại: tốn phí |

**Bẫy CSP đã kiểm trong bundle 5.3.0:** không có `eval`/`new Function`; style gán qua thuộc tính JS (CSP cho phép); logo attribution (`attributionLogo`, mặc định bật) chèn một thẻ `<style>` sẽ bị `default-src 'self'` chặn, và link logo gắn `utm_source=<hostname+path>` gửi tên miền admin cho TradingView. **Quyết định:** `layout.attributionLogo = false` + chân trang tĩnh "Biểu đồ: TradingView Lightweight Charts™ — © TradingView, Inc." kèm link NOTICE tự host (đã có `static/vendor/lightweight-charts-NOTICE.txt`). Không thêm hash vào `style-src` vì đổi theo mỗi phiên bản.

**Kiến trúc dữ liệu chart:** backend Python tính mọi lớp phủ bằng **cùng mã feature** mà chiến lược dùng (để chart hiện đúng thứ bot thấy), trả JSON: `bars`, `volume` (màu theo delta), `lines` (EMA/VWAP), `markers` (swing/BOS/CHoCH/sweep/tín hiệu), `price_lines` (entry/SL/TP1..3), `primitives.sessions`, `primitives.levels` (trạng thái armed/swept…), `primitives.trendlines`, `primitives.volume_profile` (bins mua/bán, POC, VAH, VAL). Trình duyệt chỉ vẽ. Cập nhật bằng polling `?since=` 5 giây như dashboard hiện tại.

## 8. Việc còn mở của nghiên cứu này

- Xác nhận bảng phí trực tiếp `binance.com/en/fee/futureFee` và header giấy phép mã nguồn từng script TradingView.
- Đọc bản đầy đủ Wen et al. 2022.
- Đo trượt giá thực tế trên PAPER trước khi chốt ngưỡng chi phí scalping.
- Kiểm tra quyền dữ liệu: dùng dữ liệu công khai Binance (klines 1m/5m, aggTrades) cho PAPER nội bộ vẫn là **quyết định owner #1** của khảo sát 14/11.

## 9. Nguồn đã kiểm (15/11/2026)

- Lightweight Charts: https://github.com/tradingview/lightweight-charts/releases · https://github.com/tradingview/lightweight-charts/blob/master/README.md · https://raw.githubusercontent.com/tradingview/lightweight-charts/master/NOTICE · https://tradingview.github.io/lightweight-charts/plugin-examples/ · https://github.com/tradingview/lightweight-charts/releases/tag/plugins-2026-11-13 · https://registry.npmjs.org/lightweight-charts
- TradingView licensing: https://www.tradingview.com/charting-library-docs/latest/getting_started/Frequently-Asked-Questions · https://www.tradingview.com/free-charting-libraries/ · https://www.tradingview.com/widget-docs/faq/data/
- KLineChart: https://github.com/klinecharts/KLineChart · Highcharts: https://shop.highcharts.com/
- Chỉ báo TradingView: https://www.tradingview.com/script/YhyU49QG-Auto-Trendlines-Market-Structure-MTF/ · https://www.tradingview.com/script/TVsSOe65/ · https://kr.tradingview.com/script/w7Biyqnb-Volume-Pressure-Atlas-MQLSoftware/ · https://www.tradingview.com/support/solutions/43000590599-script-publishing-rules/
- Binance: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data · https://www.binance.com/en/support/faq/360033544231 · https://www.binance.com/en/support/announcement/3243c81a35bd4f0c86a37315c3dc96cc
- Nghiên cứu: https://arxiv.org/abs/1011.6402 · https://arxiv.org/pdf/2602.00776 · https://arxiv.org/abs/2506.05764 · https://stoye.economics.cornell.edu/docs/Easley_ssrn-4814346.pdf · https://econpapers.repec.org/RePEc:eee:riibaf:v:49:y:2019:i:c:p:71-81 · https://ideas.repec.org/a/eee/ecofin/v62y2022ics1062940822000833.html
