# WBS CK — Buồng lái biểu đồ (chart cockpit) theo hai app tham chiếu

**Ngày:** 2026-11-15 · **Trạng thái:** CK-0 đã làm; CK-1…CK-5 đã làm phần lõi (15/11/2026, xem cuối từng mục, còn mở ghi rõ); CK-6…CK-13 là kế hoạch · **Chế độ:** PAPER/replay; chỉ dữ liệu thị trường công khai; không route order, không bật SHADOW/PAPER_GATE/Demo/LIVE. <br>
**Đầu vào:** hai ảnh owner gửi ngày 15/11/2026 — (1) "XAUUSD Research Desk 3.3.0" (MT5, ICT + VSA); (2) một app quét/auto crypto trên Bybit (WLFI/USDT, Green Flag, MA34×89). <br>
**Xây trên:** [WBS SIG](./2026-11-15-sig-market-structure-scalping-chart-wbs.md) (đang ở nhánh `claude/project-progress-review-wp07-d70431`, **chưa vào `dev`**), [UI kit v1.1](../../design/admin-ui-kit.md), ingest live Binance (`docs/research/binance-usdm-api-audit-2026-11-14.md`), [khảo sát 14/11](./2026-11-14-feature-survey-and-manager-insights.md).

> Ảnh là sản phẩm của bên thứ ba. Plan này lấy **ý tưởng trình bày và tổ chức thông tin**, không chép mã, logo, tên sản phẩm hay bộ quy tắc độc quyền. Các con số trên ảnh ("TĂNG +7/8", "PREMIUM 96%", "68%", "75%") không có định nghĩa công bố; Crypidian chỉ hiển thị chỉ số tự định nghĩa, có công thức và nguồn.

## 1. Mục tiêu

Một trang biểu đồ duy nhất mà người quản lý nhìn vào là biết ngay:

1. **Giá đang ở đâu:** bid/ask, spread, nến hiện tại và thời gian còn lại, biên ngày, mức PDH/PDL, mark price, funding và thời điểm thanh toán funding kế tiếp.
2. **Thị trường đang thế nào:** xu hướng từng khung (H4/H1/M15/M5), mức làm xu hướng mất hiệu lực, giá đang ở vùng đắt hay rẻ của biên dao động, phiên/killzone đang chạy.
3. **Bot đang chờ gì:** từng setup đạt bao nhiêu điều kiện, còn thiếu điều kiện nào, điều kiện đó cần giá/thời điểm nào.
4. **Đã xảy ra gì:** tín hiệu, lệnh PAPER, quét thanh khoản, phá cấu trúc — trên chart và trong nhật ký.
5. **Dữ liệu có đáng tin không:** nguồn, số nến, độ trễ, kết nối stream, khoảng trống dữ liệu.

Mọi lớp vẽ trên chart và mọi điều kiện trong checklist phải tính bằng **cùng một mã feature** mà chiến lược dùng (nguyên tắc của SIG).

## 2. Phân tích ảnh 1 — XAUUSD Research Desk (MT5, ICT/VSA)

| Vùng | Thành phần quan sát được | Ý nghĩa / cách dùng |
| --- | --- | --- |
| Thanh trên | Tab thị trường (Vàng/Chứng khoán/Crypto) | Đổi nhóm tài sản |
| | Chip phiên **Asia · London · NY AM · NY PM · Silver Bullet · Macro** (chip đang chạy được viền) | Killzone ICT theo giờ New York, có cửa sổ Silver Bullet và "macro" ngắn |
| | "MT5 · TRỰC TIẾP", "AUTO TRADE BẬT 3 mã · 0 lệnh", "GIAO DỊCH BẬT · REAL", đồng hồ "08:28:01 giờ máy" | Trạng thái nguồn dữ liệu, bộ tự động và chế độ giao dịch; đồng hồ hệ thống |
| Dải KPI | **Bid/Ask** 4.110,248 · Ask · spread 0,240 | Giá mua/bán tốt nhất và chênh lệch |
| | **Nến hiện tại** 4.110,248 · −0,795 (−0,02%) · "đóng sau 1:58" | Giá, thay đổi so với giá mở, **đếm ngược đóng nến** |
| | **Xu hướng (điểm) TĂNG +7/8** · "H4▲ H1▲ M15▲ M5▼ · PREMIUM 96%" | Điểm đồng thuận đa khung + vị trí giá trong biên (vùng đắt/rẻ) |
| | **Killzone (giờ NY 20:28)** Asia KZ · "Tích lũy (Asia)" | Phiên hiện tại và tính chất kỳ vọng của phiên |
| | **Biên ngày** 4.054,107 – 4.112,395 · PDH/PDL | Biên độ ngày và đỉnh/đáy ngày trước |
| Thanh công cụ chart | Chọn mã, khung M1…W1, `fx (1)` chỉ báo, `Vùng (4)`, `Lớp 5/27`, nhảy về hiện tại, tải lại, **Nhiều chart**, Nạp CSV, Demo | Quản lý chỉ báo/vùng/lớp có đếm số; bố cục nhiều chart |
| Chart | Nến + đường EMA; **mũi tên tín hiệu** mua/bán trên từng nến | Tín hiệu lịch sử của mô hình |
| | Hộp **OB M5 / OB M1** (order block nhiều khung) | Vùng cung/cầu ICT |
| | **PDH 4.106,990 √quét**, **PDL 4.026,719** | Mức thanh khoản và trạng thái đã bị quét |
| | **nPOC 4.126,690**, **POC TPO 4.072,307** + histogram profile bên phải | POC chưa bị chạm lại (naked) và POC theo thời gian (TPO) |
| | Đường Bid/Ask trên trục giá; nhãn **1:58** cạnh giá | Giá trực tiếp và đếm ngược ngay trên chart |
| | Đỉnh 4.146,902 nét đứt | Mức tham chiếu (đỉnh gần) |
| | Volume tô màu (vàng/tím/xanh) | Phân loại nến theo **VSA** |
| | Chú giải OHLC + Vol; "Chỉ báo mới 20 4088.586" | Legend giá trị chỉ báo tại con trỏ |
| | Thanh vẽ bên trái: con trỏ, trendline, đường ngang, hình chữ nhật, văn bản, nam châm, khóa, ẩn, hoàn tác, xóa | **Công cụ vẽ** của người dùng |
| | Hộp Bán/Mua 1 chạm + lot + SL/TP | Đặt lệnh thật từ chart — **không làm** (xem §6) |
| Cột phải | Tab Entry · Nhận định · Lệnh · Thống kê · AI · Sự kiện (22) · Mức giá · Tin & Lịch | Bảng điều khiển theo ngữ cảnh mã đang xem |
| | Ma trận **H4/H1/M15/M5**: TĂNG/GIẢM + vai trò (Xu hướng chính / Động lượng / Thời điểm) | Vai trò của từng khung trong quyết định |
| | "Đồng thuận TĂNG (mạnh) — H4/H1 và M15 cùng chiều"; "H4 giữ TĂNG khi chưa đóng cửa dưới 3.869,928" | Câu kết luận + **mức vô hiệu** từng khung |
| | "Entry theo xu hướng chính" · **Chọn kịch bản 2/5** · **CHƯA ĐỦ ĐIỀU KIỆN** · setup "Tiếp diễn (BOS + hồi) 3/4", "Spike & Ledge 2/6" | Nhiều setup chạy song song, mỗi setup có tiến độ |
| | **MUA (Long)** · "Còn thiếu: BOS M5" | Hướng dự kiến + điều kiện còn thiếu |
| | Checklist **3/4 đạt**: Xu hướng ✓, Khung lớn ✓, BOS M5 ✗ ("chờ M5 đóng cửa trên đỉnh trung hạn ITH/ITL"), Thời điểm (cộng điểm) ✗ ("ngoài London/NY AM"), Tin tức ✓ ("không có tin đỏ USD ±30 phút") | Điều kiện bắt buộc + điều kiện **cộng điểm**; lọc tin |
| Dock dưới | Giao dịch · Lịch sử · Nhật ký vào lệnh; lọc Vị thế/Giao dịch/Lệnh; "Tháng qua"; Xuất CSV; bảng thời gian, ticket, mã, loại, khối lượng, giá, SL, TP, đóng lúc, giá đóng, phí, swap, lợi nhuận | Lịch sử ngay dưới chart |
| Thanh trạng thái | "MetaTrader 5 EXNESS · … · XAUUSDc M5 · 500 nến đã đóng · cập nhật 08:27:40"; "Tick mới"; chú thích "ICT neo giờ New York + FVG/IFVG/BPR + HoanLe VSA màu nến" | Nguồn dữ liệu, số nến, thời điểm cập nhật, danh sách lớp đang bật |

## 3. Phân tích ảnh 2 — app quét/auto crypto (Bybit)

| Vùng | Thành phần quan sát được | Ý nghĩa / cách dùng |
| --- | --- | --- |
| Góc trái trên | Unified · Available · số dư từng tài sản · IP · Account center | Tài khoản sàn — **không làm** (dùng số dư PAPER) |
| Bảng thao tác | Manual spot WLFI/USDT, "Fixed USDT 10", Buy/Sell | Lệnh thủ công — **không làm** với sàn |
| Bot tự động | **AUTO GREEN FLAG 1H 3/5 · ACTIVE** · Log/Start/Stop/Next | Một chiến lược tự động, tiến độ 3/5 |
| | Kiểm tra realtime: Green flag ✓ (nến 05/11 19:00), Test đáy equal low ✓, MA20 đi ngang ✓ (3 nến), MA20 đảo chiều ○, nến đóng gần nhất | Checklist **có thời điểm** của từng điều kiện |
| | Điều kiện BUY ✓/○ (Green flag H1 bắt buộc; nến sau green flag đã đóng; 1D giữ đáy; 1D MA20 tăng; 1D giá ≥ MA20; 1D cấu trúc tăng; không mua đuổi) · **"CHỜ BUY 2/5 (cần ≥3/5 + Green Flag + nến sau)"** | Quy tắc: điều kiện bắt buộc + **ngưỡng tối thiểu k/n** |
| | Điều kiện SELL: red flag sau BUY (bắt buộc), phá cấu trúc, đóng < MA20, phá đáy 3 nến · "cần red flag + ≥1/3" | Quy tắc thoát cũng có checklist |
| | **AUTO MA34×89 REVERSAL 4H · OFF** (MA34↑ 2 nến → close > MA34 + breakout → retest → buy) | Chiến lược thứ hai, tắt |
| | Capital protection auto/flex · **Multi auto slots 1/5 · queue 0** | Bảo vệ vốn; số chỗ chạy đồng thời |
| Hàng trên | **HOT WEEKLY top 10 volatile liquid USDT** (biên %, thay đổi %, volume) · **21D RANGE −5%…+10% top 10** · **TOP MARKET CAP top 10** | Bộ quét thị trường |
| Đầu chart | WLFI/USDT · Change pair · Watchlist 15m · Paper history 1H · **Validate data** · Auto scanner log · Exchange Bybit · Mkt cap/FDV/Rank | Đổi mã, watchlist, kiểm dữ liệu, nhật ký quét |
| | 1m…1W · Display · **ZigZag 3/200** · MTF 4H NO / 1D NO / 1W NO · **HTF BLOCK** | ZigZag; bộ lọc khung lớn có chế độ chặn |
| | Dải chỉ báo: SMA20, SMA50, RSI14, ATR14, VOL vs MA20 ×0,05, V20 range, POS −3%, **LOCAL REGIME UNCLEAR** | Đọc nhanh chỉ báo + nhãn chế độ thị trường |
| | "6 nến đã đóng" · chip **4H GIẢM −1,4% 68% · 1D GIẢM −9,5% 75% · 1W GIẢM −16,1% 89%** · "HTF: xu hướng giảm đồng thuận · chỉ cảnh báo" | Xu hướng khung lớn + thay đổi % + độ mạnh; chế độ "chỉ cảnh báo" |
| Panel chart | S/M/L đổi kích thước; "Real auto session active 0/1 completed"; **LIVE MIRROR holding … entry … now … UPnL … wait sell** | Trạng thái phiên tự động và vị thế đang giữ |
| | Tab Real auto history / Paper / Backtest; bảng Pair, Action, Entry, P/L% | Lịch sử theo nguồn |
| Chart | MA20, MA50, **Stop loss** (đường), **ZigZag**; hộp **"AUTO BUY FILLED Entry 0.046580"** chỉ vào nến; đường entry nét đứt; nhãn giá hiện tại | Lớp vị thế đang mở trên chart |
| | Pane volume: "NOW 51,28K · MA20 1,07M · RANGE · POS −3%" | Volume hiện tại so với trung bình |
| | Nút **→ LIVE**, Hide paper, Reset view, Go live; gợi ý "Middle-drag pan · +/− zoom · Home: live · Double-click reset · **Right-click signal: Analyze**" | Điều hướng bằng phím/chuột; **phân tích tín hiệu tại nến** |
| | "RT events 1 · Closed 7 · Resync 2" | Sức khỏe kết nối thời gian thực |

## 4. Đối chiếu với Crypidian

Ký hiệu: **D** = đã có trên `dev`; **S** = đã làm lát đầu trên nhánh SIG (chưa vào `dev`); **—** = chưa có.

| Tính năng | Trạng thái | Bằng chứng / ghi chú | Gói |
| --- | --- | --- | --- |
| Chart nến Lightweight Charts, theme sáng/tối, marker vào/đóng lệnh | D | `static/trade-chart.js`, bot detail, dashboard | — |
| Trang `/admin/chart` có volume theo delta, EMA20/50, VWAP, RSI, MACD, CVD, phiên Tokyo/London/NY, PDH/PDL + trạng thái quét, swing/BOS/CHoCH, trendline, volume profile POC/VA, bật tắt lớp | S | `application/market_chart.py`, `static/market-chart.js` | CK-0 (merge) |
| Checklist có cấu trúc + thẻ tín hiệu (vào/SL/TP, mất hiệu lực) | S | `domain/strategy/checklists.py`, `signal_card.py` | CK-6 dùng lại |
| Collector nến + taker buy | S | `market_bar_collector.py` | CK-0 |
| Nến live qua WebSocket `/market`, mark price, thanh lý, dòng lệnh taker | D | `market_stream.py`, `live_market_ingestion_worker.py` (phiên này) | CK-1 dùng lại |
| REST bookTicker, premiumIndex (mark/index/funding kế tiếp), 24h ticker, long/short, basis, depth | D (wrapper) | `adapters/binance_usdm/market_data.py` | CK-1, CK-11 |
| Bid/Ask trực tiếp (stream `@bookTicker`) | S | `LiveQuoteWorker` route `/public` → bảng `market_live_quote` | CK-1 |
| Đếm ngược đóng nến, đếm ngược funding | S | `domain/market/overview.py` `candle_clock`; `nextFundingTime` của mẫu mark | CK-1 |
| Điểm xu hướng đa khung + mức vô hiệu + vai trò khung | S (vai trò cố định) | `timeframe_trend`, `mtf_consensus`; ma trận trên `/admin/chart` | CK-2 |
| Vùng đắt/rẻ (premium/discount) | S | `position_pct`/`zone` từng khung | CK-2 |
| Killzone ICT, Silver Bullet, macro | — | S chỉ có phiên Tokyo/London/NY | CK-3 |
| FVG, IFVG, BPR, Order Block | — | grep 0 kết quả | CK-3 |
| Đỉnh/đáy bằng nhau, ITH/ITL | — (backlog SIG-1) | — | CK-3 |
| TPO profile, nPOC | — | S có volume profile, chưa có TPO và POC "trần" | CK-4 |
| Phân loại nến VSA, tô màu nến/volume | — | — | CK-5 |
| ZigZag | — | Pivot đã xác nhận có ở `geometry.py` | CK-5 |
| Nhiều setup song song, tiến độ k/n, điều kiện bắt buộc vs cộng điểm, "còn thiếu" | — (một phần ở S) | Checklist S chỉ cho H01/H03/H06, chưa có ngưỡng k/n | CK-6 |
| Lọc tin tức ±30 phút | — | WP05 macro/news chưa có nguồn | CK-6, CK-10 |
| Đường vị thế PAPER (entry/SL/TP/UPnL) trên chart, hộp "PAPER FILLED" | Một phần | Bot detail có marker fill; chưa có UPnL sống trên chart | CK-7 |
| Chuột phải nến → phân tích quyết định tại nến | Một phần | Microscope (Strategy Card) có, chưa gắn với nến thật | CK-7 |
| Phím tắt điều hướng, → Live, Reset view | Một phần | Lightweight Charts có cuộn/zoom; chưa có phím | CK-7 |
| Công cụ vẽ của người dùng + lưu | — | — | CK-8 |
| Quản lý lớp/chỉ báo có đếm, preset, nhiều chart | Một phần (S có bật tắt lớp) | — | CK-8 |
| Dock dưới chart: vị thế/lịch sử/nhật ký quyết định + CSV | Một phần | Trang báo cáo + CSV có sẵn | CK-9 |
| Thanh trạng thái nguồn dữ liệu, độ trễ, resync | Một phần | Readiness `live_market_ingestion`, `market_collector` | CK-9 |
| Tab Nhận định / Mức giá / Sự kiện / Tin & Lịch | — | MI-6 (tường thuật) là kế hoạch | CK-10 |
| Bộ quét biến động/biên/volume | — | Có REST 24h ticker | CK-11 |
| Market cap / FDV / xếp hạng | — | Cần nguồn ngoài Binance (quyền dữ liệu WP05) | CK-11 (tùy chọn) |
| Watchlist | — | Multi-select coin có sẵn (UI kit v1.1) | CK-11 |
| Bảng giám sát bot trên mã đang xem (checklist realtime, log) | Một phần | Bot detail, decision log | CK-12 |
| Bảo vệ vốn, số chỗ chạy | Một phần | Circuit breaker, loss windows, `max_positions` | CK-12 |
| Chiến lược Green Flag, MA34×89, Tiếp diễn BOS + hồi | — | — | CK-13 (giả thuyết) |
| Đặt lệnh thật, AUTO TRADE REAL, số dư sàn | **Không làm** | LIVE khóa | §6 |

## 5. Bố cục đích

```text
┌ Thanh trên ──────────────────────────────────────────────────────────────────────────┐
│ Mã ▾ · chip killzone (Asia | London | NY AM | NY PM | Silver Bullet | Macro)          │
│ Nguồn: Binance USDⓈ-M · stream OK · PAPER · đồng hồ UTC / giờ NY                       │
├ Dải KPI (CK-1, CK-2) ─────────────────────────────────────────────────────────────────┤
│ Bid/Ask · spread │ Nến hiện tại · đóng sau mm:ss │ Đồng thuận x/y · ▲▲▲▼ · vị trí % │  │
│ Killzone + giờ NY │ Biên ngày · PDH/PDL │ Mark · funding · còn hh:mm               │
├───┬───────────────────────────────────────────────────────────────┬──────────────────┤
│ V │ Thanh công cụ: khung · fx(n) · Vùng(n) · Lớp a/b · → Live ·    │ Tab: Entry ·     │
│ ẽ │ Reset · Nhiều chart                                            │ Nhận định ·      │
│   │ Chart: nến (VSA tùy chọn) · EMA/ZigZag · FVG/OB/BPR · phiên ·  │ Lệnh · Thống kê ·│
│ ( │ killzone · PDH/PDL √quét · nPOC · VP/TPO · bid/ask · đếm ngược │ Sự kiện · Mức giá│
│ C │ · đường vị thế PAPER + UPnL · tín hiệu                         │ · Tin & Lịch ·   │
│ K │ Pane volume (delta/VSA) · pane RSI/MACD/CVD                    │ AI (chỉ đọc)     │
│ 8 │                                                                │ (CK-2, CK-6,     │
│ ) │                                                                │ CK-10, CK-12)    │
├───┴───────────────────────────────────────────────────────────────┴──────────────────┤
│ Dock: Vị thế PAPER · Lịch sử · Nhật ký quyết định · Sự kiện · lọc kỳ · Xuất CSV (CK-9)│
├ Thanh trạng thái ─────────────────────────────────────────────────────────────────────┤
│ Nguồn · N nến đã đóng · cập nhật · độ trễ · stream: sự kiện/đóng/resync · lớp đang bật │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

**Mobile (≤ 767px):** dải KPI thành 2 cột cuộn ngang được; thanh vẽ ẩn (chỉ xem bản vẽ); cột phải thành tab dưới chart; dock thành danh sách; vùng chạm ≥ 44px; volume profile thu hẹp hoặc ẩn.

## 6. Ngoài phạm vi

- Hộp Bán/Mua 1 chạm, lot, "GIAO DỊCH BẬT · REAL", "AUTO TRADE BẬT", số dư và IP tài khoản sàn: Crypidian không gửi lệnh sàn; LIVE khóa.
- Nút START/STOP 1 chạm cho bot: tạm dừng/tiếp tục vẫn đi qua form có lý do và kiểm toán (UI kit v1.1).
- Số "độ tin cậy %" chưa hiệu chuẩn, "điểm +7/8" cộng các chỉ báo tương quan: thay bằng chỉ số có định nghĩa (CK-2) và hạng tin cậy theo SIG-6.
- Nạp CSV dữ liệu giá ngoài và đổi sàn: dữ liệu ngoài phải qua đăng ký nguồn và quyền dữ liệu (WP05).

## 7. WBS

### CK-0 — Hợp nhất nền SIG vào `dev` (bắt buộc trước)

1. Merge nhánh `claude/project-progress-review-wp07-d70431` (8 commit: collector, `/admin/chart`, cấu trúc/phiên/profile, checklist, thẻ tín hiệu) vào `dev`; nối migration `e3a5c7e9f1b4` sau head hiện tại; xử lý trùng mục glossary "AA" (nhánh SIG và `dev` cùng dùng nhãn AA).
2. Chuyển các ô cấu hình của `/admin/chart` sang UI kit v1.1 (multi-select coin, công tắc lớp).

**Acceptance:** một Alembic head; unit + Playwright + Compose E2E admin và chart đạt. **Phụ thuộc:** owner duyệt merge.

**Đã làm (15/11/2026):** merge `claude/project-progress-review-wp07-d70431` @ `261c2ea` vào nhánh làm việc. Xung đột 8 tệp: `compose.yaml`, `app.py`, `settings.py` giữ **cả hai** (ingest live WebSocket của `dev` và collector REST SIG-0 chạy song song, nguồn khác nhau); probe/test backfill WP07 lấy bản SIG (cùng sửa một lỗi, bản SIG thêm PAPER_DEMO cho collector); tài liệu giữ cả hai. Glossary: mục SIG giữ nhãn **AA** (tài liệu SIG tham chiếu), mục điều khiển biểu mẫu đổi thành **AB**, mục CK là **AC**. Alembic một head `e3a5c7e9f1b4` (migration SIG nối sau `d2e4f6a8b0c1` sẵn có). `/admin/chart` theo UI kit v1.1: 10 lớp hiển thị là công tắc kiểu iOS (nhãn trái, công tắc phải), ô chọn mã là Tom Select có biểu tượng coin (`data-searchable="coins"`), ô chọn một của Tom Select giữ trên một hàng. Ô chọn bot giữ native vì test vùng chạm đo trực tiếp `#mc-bot`.

### CK-1 — Dải thông số thị trường trực tiếp

1. Bid/Ask/spread: worker đăng ký stream `<symbol>@bookTicker` (route `/public`, kiểm tài liệu Binance hiện hành trước khi code), giữ giá mới nhất trong bộ nhớ của worker và ghi mẫu theo chu kỳ (không ghi từng tick); khi chưa có stream thì dùng REST `ticker/bookTicker` kèm nhãn "REST, trễ ≤ n giây".
2. Nến hiện tại: giá, thay đổi so với giá mở, **đếm ngược đóng nến** (client tính từ `open_time + interval` và đồng hồ máy chủ trong JSON, không tin đồng hồ trình duyệt).
3. Biên ngày UTC + PDH/PDL (dùng mức từ SIG-1), mark/index price, funding dự kiến và **đếm ngược tới `nextFundingTime`** (đã có ở `premiumIndex` và stream mark price).
4. Nhãn đếm ngược cạnh giá trên trục và đường Bid/Ask trên chart (price line).
5. API: thêm khối `header` vào `GET /admin/api/chart`; polling 1–2 giây cho khối header, 5 giây cho phần còn lại.

**Acceptance:** số trên dải khớp dữ liệu nguồn trong fixture; thiếu stream thì hiện rõ nguồn dự phòng; đếm ngược đúng ở khung 1m/5m/1h/4h kể cả qua mốc ngày UTC.

**Đã làm (15/11/2026, commit `72a3b58`):**
- Stream `<symbol>@bookTicker` trên route `/public` (kiểm tài liệu Binance 15/11/2026); một kết nối chỉ một route. `LiveQuoteWorker` giữ giá mới nhất trong bộ nhớ, ghi mỗi 2 giây (`TRADING_BOT_INGESTION_LIVE_QUOTE_INTERVAL_SECONDS`, 0,5–60) và ghi nốt khi rớt kết nối; bật bằng `book_ticker` trong `TRADING_BOT_INGESTION_LIVE_EVENTS`. Readiness `live_market_quotes`.
- Migration `a7c9e1b3d5f6`: bảng `market_live_quote` một dòng/(workspace, nguồn, instrument), upsert chỉ nhận `update_id` lớn hơn, RLS bắt buộc; ingestion SELECT/INSERT/UPDATE (không DELETE), admin-web/runtime/market-signal chỉ đọc; admin-web thêm quyền đọc `market_mark_price`.
- `GET /admin/api/chart/overview` (no-store, polling 2 giây, kiểm `REPORT_READ` như chart, một snapshot đọc). Dải thông số: giá hiện tại + nguồn, bid/ask/spread (bps) + tuổi giá, đếm ngược nến, đồng thuận đa khung, phiên đang chạy, biên ngày UTC + PDH/PDL, mark + funding + đếm ngược. Chart vẽ đường Bid/Ask (lớp "Bid/Ask trực tiếp") và nhãn đếm ngược trên trục giá.
- **Lệch so với kế hoạch:** API riêng `/api/chart/overview` thay vì khối `header` trong `/api/chart` (polling nhanh không kéo theo 600 nến). Không dùng REST `ticker/bookTicker` làm dự phòng: thiếu/cũ stream thì giá hiện tại lùi về mark (≤ 180 giây) rồi giá đóng nến, kèm nhãn nguồn và ghi chú. Đếm ngược chạy bằng đồng hồ trình duyệt giữa hai lần polling và đồng bộ lại theo `seconds_left` của máy chủ mỗi 2 giây.
- **Còn mở:** % thay đổi so với giá mở nến hiện tại (đang so với giá đóng nến đã đóng gần nhất); stream chưa bật trên stack local `crypidian-review`.

### CK-2 — Ma trận xu hướng đa khung, mức vô hiệu và vùng đắt/rẻ

1. Trạng thái xu hướng mỗi khung (H4/H1/M15/M5, cấu hình được): TĂNG/GIẢM/ĐI NGANG từ chuỗi swing HH/HL/LH/LL và BOS/CHoCH gần nhất của SIG-1, chỉ dùng nến khung lớn đã đóng.
2. **Mức vô hiệu**: TĂNG giữ khi chưa đóng cửa dưới đáy swing xác nhận gần nhất (ngược lại cho GIẢM); hiện câu "H4 giữ TĂNG khi chưa đóng cửa dưới X".
3. **Vai trò khung** theo setup: xu hướng chính / động lượng / thời điểm (khai báo trong setup, không cứng trong UI).
4. **Đồng thuận** = số khung cùng chiều với khung chính trên số khung xét (ví dụ "3/4 khung cùng TĂNG"); không cộng các chỉ báo đo cùng một thứ (tham khảo cảnh báo trùng lặp ở SIG-6).
5. **Vị trí trong biên** = (giá − đáy biên) / (đỉnh biên − đáy biên) × 100%, biên = cặp swing xác nhận gần nhất của khung chính; > 50% là vùng đắt (premium), < 50% là vùng rẻ (discount); hiện kèm khung và mốc biên.
6. Chip khung lớn kiểu ảnh 2: hướng + % thay đổi từ giá mở nến khung lớn; **không** hiện "độ mạnh %" chưa định nghĩa.
7. **Bộ lọc khung lớn** cho setup: ô chọn Chặn / Chỉ cảnh báo / Tắt. Cần quyết định owner vì overlay bot v1 cấm trường lạ (đổi lược đồ v2 hoặc để trong tham số chiến lược).

**Acceptance:** golden dataset tay cho 3 kịch bản (đồng thuận, lệch, đi ngang); mức vô hiệu đổi đúng khi có swing mới; test "streaming = batch".

**Đã làm (15/11/2026, commit `72a3b58`):** `timeframe_trend` (xu hướng theo BOS cuối của swing xác nhận 3 nến mỗi bên, tối thiểu 20 nến đã đóng; mức vô hiệu; biên = cặp swing gần nhất; vị trí %/vùng đắt-rẻ; phá vỡ gần nhất) và `mtf_consensus` (`ck2-mtf-v1`). Ma trận 4 vai trò trên `/admin/chart` (4H xu hướng chính, 1H xác nhận, 15M động lượng, 5M thời điểm) kèm câu "1H giữ TĂNG khi chưa đóng cửa dưới X"; khung thiếu nến ghi rõ, khung chính = khung đầu tiên có dữ liệu. Golden test tay: tăng, giảm (đối xứng), thiếu dữ liệu, đồng thuận 1/2.
**Còn mở:** vai trò khung đang cố định (chưa khai báo theo setup); chưa có golden "đi ngang" và test "streaming = batch"; chip % thay đổi theo nến khung lớn (mục 6); bộ lọc khung lớn (mục 7) chờ owner quyết định lược đồ.

### CK-3 — Lớp ICT/SMC: killzone, FVG/IFVG/BPR, order block, đỉnh/đáy bằng nhau

Định nghĩa ICT không thống nhất giữa các nguồn (xem §12). Dự án chốt **quy ước cố định**, ghi `formula_version`, tham số khóa trước backtest, chỉ dùng làm **mô tả và gắn nhãn** cho tới khi qua SIG-5.

1. **Killzone theo giờ New York** (`zoneinfo` America/New_York, tự đổi DST): Asia 20:00–00:00, London 02:00–05:00, NY AM 07:00–10:00, NY PM 13:30–16:00; Silver Bullet 03:00–04:00, 10:00–11:00, 14:00–15:00; macro 09:50–10:10 và các cửa sổ xx:50–xx:10 được owner chọn. Bảng giờ là cấu hình có version, không cứng trong mã. Vẽ dải nền trên chart; chip trên thanh trên sáng khi đang trong cửa sổ.
2. **FVG**: ba nến liên tiếp, `low[t] > high[t−2]` (tăng) hoặc `high[t] < low[t−2]` (giảm); xác nhận khi nến t đóng; theo dõi lấp một phần/toàn bộ; ngưỡng tối thiểu theo ATR để bỏ khe nhỏ.
3. **IFVG**: FVG bị **giá đóng cửa** xuyên qua và đổi vai trò (quy ước: đóng cửa, không dùng râu).
4. **BPR**: phần giao của hai FVG ngược chiều gần nhau (trong N nến); hướng theo FVG xuất hiện sau.
5. **Order block**: nến ngược chiều cuối cùng trước nhịp đi tạo BOS (cần BOS của SIG-1 đã xác nhận); vô hiệu khi đóng cửa xuyên vùng. Đa khung (M1/M5/M15) chỉ hiện OB của khung đang xem và một khung lớn hơn để tránh rối.
6. **Đỉnh/đáy bằng nhau** (≥ 2 pivot chênh ≤ k·ATR) và **ITH/ITL** (đỉnh/đáy trung hạn: swing được hai swing ngắn hạn hai bên xác nhận) — đóng backlog SIG-1.
7. Mỗi vùng có `created_at`, `confirmed_at`, `invalidated_at`, khung, trạng thái; panel "Vùng (n)" đếm vùng còn hiệu lực.

**Acceptance:** golden dataset tay cho mỗi loại; vùng không xuất hiện trước `confirmed_at`; test DST tuần London/New York đổi giờ lệch nhau; giới hạn số vùng vẽ để chart mượt trên mobile.

**Đã làm (15/11/2026, commit `a310357`):** `domain/market/ict.py` (`ck3-ict-v1`, bảng giờ `ck3-killzone-v1`): `killzone_windows`/`active_killzones` theo `America/New_York` (cửa sổ Á qua nửa đêm), `fair_value_gaps` (lọc khe < 0,1×ATR; chạm/lấp/đảo vai IFVG bằng giá đóng cửa/mất hiệu lực), `balanced_price_ranges`, `order_blocks` (nến ngược chiều cuối tại/trước cực trị nhịp hồi giữa swing bị phá và nến phá; dùng cả BOS và CHoCH), `equal_levels` (≤ 0,1×ATR, không nến nào vượt mức giữa hai swing, ghi nến quét), `intermediate_swings`. Mỗi vùng có `confirmed_index` và `state_at(i)`. Payload chart khối `ict` (killzone chỉ ở khung ≤ 15m, 3 ngày gần nhất; tối đa 12 FVG, 8 OB, 4 BPR, ưu tiên vùng còn hiệu lực); chart có 3 công tắc mới (FVG·OB·BPR, EQH/EQL·ITH/ITL, Killzone mặc định tắt), ô "Vùng ICT còn hiệu lực", sự kiện xác nhận vùng/quét EQH-EQL; dải thông số hiện chip killzone đang mở.
**Quyết định owner #2 (§11) chưa chốt:** đang dùng quy ước ghi ở mục này làm mặc định (chỉ mô tả/gắn nhãn, không vào lệnh); đổi quy ước thì đổi `ICT_FORMULA_VERSION`/`KILLZONE_TABLE_VERSION`.
**Còn mở:** OB đa khung (khung đang xem + một khung lớn hơn); cửa sổ macro xx:50–xx:10 do owner chọn (mới có 09:50–10:10); bảng giờ killzone đang là hằng số có version, chưa là cấu hình sửa được trên Admin; panel "Vùng (n)" dạng danh sách (mới có số đếm + danh sách sự kiện).

### CK-4 — Profile mở rộng: TPO và POC "trần" (nPOC)

1. **TPO profile** theo phiên/ngày đã đóng: mỗi khối 30 phút ghi nhận các mức giá đã chạm; POC TPO, Value Area 70% TPO; hiện cạnh volume profile (hai cột) hoặc chuyển đổi.
2. **nPOC**: POC (volume và/hoặc TPO) của phiên đã đóng mà giá chưa quay lại; tự xóa khi giá chạm; giới hạn N phiên gần nhất.
3. VAH/VAL của phiên trước dưới dạng đường; danh sách vào tab "Mức giá".

**Acceptance:** TPO tính tay trên fixture 2 phiên; nPOC biến mất đúng nến chạm; ghi rõ TPO POC và volume POC có thể khác nhau.

**Đã làm (15/11/2026, commit `cdb634d`):** `domain/market/tpo.py` (`ck4-tpo-v1`): `tpo_profile` (khối 30 phút UTC, 24 hàng hoặc `row_size`, POC/VA 70%), `naked_levels`. Payload: mỗi volume profile ngày có thêm `tpo`; `profile_levels` = nPOC khối lượng + nPOC TPO của tối đa 5 ngày UTC đã đóng đủ nến (bỏ ngay khi bị chạm) và VAH/VAL khối lượng của ngày đã đóng gần nhất (kéo tới nến cuối). Chart: lớp "TPO profile" (mặc định tắt, cột lam bám mép phải ngày, POC TPO nét chấm) và "nPOC · VAH/VAL ngày trước" (mặc định bật); chú giải ghi TPO POC có thể khác POC khối lượng. Golden test tay: 4 khối → POC 11,5, VA 11–13; gộp nến 5m theo khối; nPOC biến mất đúng nến chạm.
**Lệch so với kế hoạch:** dùng ngày UTC (giống volume profile hiện có) thay vì từng phiên; danh sách mức trong tab "Mức giá" để CK-10.

### CK-5 — Phân loại nến VSA và ZigZag

1. **VSA** (theo quy tắc phổ biến của Tom Williams, cài lại sạch): độ rộng nến và volume so với phân vị N nến; vị trí giá đóng trong nến; nhãn: không có cầu, không có cung, cao trào mua, cao trào bán, khối lượng chặn (stopping volume), upthrust, kiểm tra cung (test). Ngưỡng cố định trước backtest. "HoanLe VSA" trên ảnh là bộ màu riêng chưa có tài liệu — **không** sao chép.
2. Tô màu nến/volume theo VSA là **tùy chọn** (mặc định vẫn tô theo delta); có chú giải màu và nhãn chữ khi rê/chạm để không chỉ dựa vào màu.
3. **ZigZag** dựng từ pivot đã xác nhận (không vẽ lại quá khứ); đoạn cuối chưa xác nhận vẽ nét đứt và ghi rõ "chưa chốt"; tham số % hoặc theo ATR.

**Acceptance:** golden dataset cho từng nhãn VSA; ZigZag "streaming = batch" cho phần đã xác nhận.

**Đã làm (15/11/2026, chưa commit):** `domain/market/vsa.py`: `vsa_labels` (`ck5-vsa-v1`, cửa sổ 20 nến trước, 7 nhãn theo thứ tự ưu tiên ghi trong docstring) và `zigzag` (`ck5-zigzag-v1`, ngưỡng 2×ATR14 hoặc %, điểm chốt theo `final_index`). Payload `vsa` (nến có nhãn + nhóm sức mạnh/yếu) và `zigzag` (điểm chốt, pivot chưa chốt, giá cuối). Chart: lớp "ZigZag (pivot đã chốt)" bật mặc định (nét đứt + chữ "chưa chốt" cho đoạn cuối), lớp "Tô volume theo VSA" tắt mặc định (lam = sức mạnh, cam = yếu, nến khác mờ); chú giải OHLC khi rê/chạm ghi tên nhãn VSA. Golden test tay cho 7 nhãn + nến không nhãn; test streaming = batch cho ZigZag (domain và payload).
**Còn mở:** chưa tô màu thân nến theo VSA (chỉ volume); ngưỡng ZigZag chưa chỉnh được trên giao diện.

### CK-6 — Bảng Entry: nhiều setup, tiến độ k/n, điều kiện còn thiếu

1. Mở rộng checklist SIG-3: mỗi điều kiện có loại **bắt buộc** / **cộng điểm**, setup có quy tắc "đủ khi mọi bắt buộc đạt và ≥ k/n cộng điểm" (kiểu ảnh 2: "cần ≥3/5 + Green flag + nến sau").
2. Hiện nhiều setup song song cho mã đang xem (từ các bot có quyền xem), tiến độ "3/4", trạng thái **Chưa đủ / Đủ điều kiện / Mất hiệu lực**, hướng dự kiến, dòng **"Còn thiếu: …"** kèm giá/thời điểm cần (MI-2).
3. Mỗi điều kiện ghi **thời điểm đạt** (ví dụ "Green flag ✓ nến 05/11 19:00") và giá trị thật so với ngưỡng.
4. Điều kiện mới: **Thời điểm** (đang trong killzone đã chọn — CK-3), **Tin tức** (không có sự kiện tác động cao trong ±N phút theo lịch nội bộ — CK-10), **Funding** (không trong ±10 phút quanh mốc funding — SIG-4).
5. Ô **Chọn kịch bản** (select) để ghim một setup lên chart (vẽ vùng vào/SL/TP của setup đó).
6. Không có nút đặt lệnh; khi đủ điều kiện, quyết định vẫn đi qua planner/risk gate như hiện tại.

**Acceptance:** setup với quy tắc k/n chuyển trạng thái đúng trên fixture; "còn thiếu" khớp điều kiện chưa đạt đầu tiên theo thứ tự ưu tiên; hash quyết định cũ không đổi.

### CK-7 — Lớp vị thế và tương tác chart

1. Vị thế PAPER đang mở: đường entry, SL, TP (TP1–3 khi SIG-3 có), nhãn **UPnL** theo mark price, hộp chú thích "PAPER đã khớp · giá" chỉ vào nến khớp; công tắc **Ẩn PAPER**.
2. **Chuột phải / nhấn giữ trên nến** → mở "Phân tích tại nến": quyết định của bot tại nến đó, checklist, lý do NoTrade, link Microscope. Không dữ liệu thì ghi rõ.
3. Phím tắt và nút: **→ Live** (về nến mới nhất), **Reset view** (double-click), +/− zoom, Home, kéo chuột giữa để cuộn; hộp gợi ý phím tắt.
4. Kích thước chart S/M/L (lưu `localStorage` bọc try/catch).

**Acceptance:** UPnL khớp ledger + mark price trong fixture; phím tắt có trên desktop, không chặn thao tác trợ năng; mobile dùng nhấn giữ.

### CK-8 — Công cụ vẽ, quản lý lớp và nhiều chart

1. Thanh vẽ bên trái: con trỏ, trendline, đường ngang, tia, hình chữ nhật, văn bản, Fibonacci thoái lui, **nam châm** (bắt OHLC), khóa, ẩn tất cả, hoàn tác/làm lại, xóa. Tự viết bằng primitive của Lightweight Charts dựa trên ví dụ chính thức (giữ header Apache-2.0); không dùng gói vẽ bên thứ ba (các gói tìm được còn sớm, v0.x, ít người dùng).
2. Lưu bản vẽ theo người dùng + mã + khung: giai đoạn 1 `localStorage`; giai đoạn 2 bảng `chart_drawing` (RLS theo workspace + thành viên, giới hạn số đối tượng, không chứa script). Cần quyết định owner.
3. Panel **Lớp a/b** và **fx (n)** liệt kê mọi lớp/chỉ báo với công tắc kiểu iOS, nhóm theo loại, preset ("ICT", "Volume", "Tối giản"); legend giá trị chỉ báo tại con trỏ.
4. **Nhiều chart**: lưới 2 hoặc 4 khung cùng mã, đồng bộ con trỏ và thời gian.

**Acceptance:** vẽ/lưu/tải lại đúng tọa độ (thời gian + giá) khi đổi khung; không script/style nội tuyến; nhiều chart vẫn mượt ở 1440px; mobile chỉ xem bản vẽ.

### CK-9 — Dock dưới chart và thanh trạng thái

1. Dock: **Vị thế PAPER**, **Lịch sử** (lọc kỳ: hôm nay/7 ngày/tháng qua/tùy chọn), **Nhật ký quyết định** (CANDIDATE/ABSTAIN/NoTrade kèm lý do), **Sự kiện**; Xuất CSV dùng lại `/admin/reports/export.csv`.
2. Thanh trạng thái: nguồn (Binance USDⓈ-M, REST/WS), số nến đã đóng đang hiện, cập nhật lần cuối, độ trễ ước tính, stream (số sự kiện, số lần ngắt/kết nối lại), khoảng trống dữ liệu (`ingestion_gap`), danh sách lớp đang bật.
3. Nút **Kiểm dữ liệu** (kiểu "Validate data" ảnh 2): chạy kiểm chất lượng nến trong khung đang xem (thiếu nến, trùng, OHLC sai, lệch với REST mẫu) và báo kết quả.

**Acceptance:** số trên thanh trạng thái khớp readiness worker; kiểm dữ liệu phát hiện được lỗ nến trong fixture.

### CK-10 — Cột phải: Nhận định, Mức giá, Sự kiện, Tin & Lịch, Thống kê

1. **Nhận định**: câu tường thuật sinh từ template theo sự kiện đã xác định (MI-6), không để LLM sinh số.
2. **Mức giá**: PDH/PDL, mức phiên, nPOC, VAH/VAL, OB/FVG còn hiệu lực, đỉnh/đáy bằng nhau; khoảng cách % tới giá hiện tại; bấm để cuộn chart tới mức.
3. **Sự kiện**: dòng thời gian BOS/CHoCH/quét/tín hiệu/khớp PAPER; đếm trên tab ("Sự kiện 22").
4. **Tin & Lịch**: giai đoạn 1 là **lịch sự kiện do owner nhập** (FOMC, CPI…; mức tác động; giờ) và mốc funding từ Binance — không cần nguồn ngoài; giai đoạn 2 nối nguồn tin/lịch kinh tế khi WP05 có quyền.
5. **Thống kê**: theo setup trên mã đang xem — n, tỷ lệ thắng so với tỷ lệ hòa vốn, kỳ vọng sau phí, phân phối R (từ báo cáo hiện có).
6. **AI**: chỉ đọc trạng thái scorer (ABSTAIN/khóa), không có thao tác.

**Acceptance:** câu nhận định truy ngược được về sự kiện nguồn; lịch nội bộ có audit khi thêm/sửa/xóa; điều kiện "Tin tức" của CK-6 dùng đúng lịch này.

### CK-11 — Bộ quét thị trường và watchlist

1. Bộ quét từ dữ liệu công khai Binance (24h ticker, kline 1d): top biến động tuần **có thanh khoản tối thiểu** (lọc quote volume), top theo biên 21 ngày (lọc theo khoảng thay đổi), top volume. Cập nhật theo chu kỳ, ghi thời điểm và nguồn.
2. Watchlist theo người dùng (multi-select coin UI kit v1.1), bấm một mã để mở chart.
3. **Market cap / FDV / xếp hạng**: cần nhà cung cấp ngoài Binance → đăng ký nguồn và quyền dữ liệu (WP05); tùy chọn, làm sau.
4. Nhật ký quét: mỗi lượt ghi tiêu chí, số mã đạt, thời điểm.

**Acceptance:** cùng dữ liệu đầu vào cho cùng danh sách; mã thiếu dữ liệu bị loại kèm lý do; không gọi API ký.

### CK-12 — Bảng giám sát bot trên mã đang xem

1. Mỗi bot có quyền xem đang theo dõi mã này: trạng thái, setup, tiến độ checklist realtime (✓/○ kèm thời điểm), kết luận "Chờ MUA 2/5 (cần ≥3/5 + …)", lần đánh giá gần nhất, link nhật ký.
2. Bảo vệ vốn: trạng thái cầu dao rủi ro, lỗ ngày/24 giờ so với giới hạn (khi FX-2 nối ledger), **chỗ chạy = vị thế mở / `max_positions`**.
3. Tạm dừng/tiếp tục dẫn tới form có lý do ở trang bot (không nút 1 chạm).

**Acceptance:** chỉ hiện bot người dùng có quyền; số chỗ chạy khớp ledger.

### CK-13 — Giả thuyết chiến lược lấy cảm hứng từ hai app (chỉ PAPER, sau SIG-5)

Đánh số tiếp sau H18–H20 của SIG. Tất cả `EXPERIMENTAL_UNVALIDATED`, tham số khóa trước backtest, phải qua SIG-5 trước khi chạy PAPER.

| ID | Setup | Khung | Ý tưởng (định nghĩa lại theo quy ước dự án) |
| --- | --- | --- | --- |
| H21 | Tiếp diễn: BOS + hồi về FVG/OB trong killzone | M5, khung chính H1/H4 | Ma trận CK-2 đồng thuận; BOS M5 cùng chiều; giá hồi vào FVG/OB chưa bị vô hiệu; trong London/NY AM; SL ngoài vùng; TP mức thanh khoản kế tiếp |
| H22 | Đảo chiều EMA34×89 khung 4H | 4H | EMA34 cắt EMA89 và dốc lên ≥ 2 nến; đóng cửa trên EMA34 và phá đỉnh gần; vào khi retest; thoát khi đóng dưới EMA89 |
| H23 | "Cờ xanh" đảo chiều đáy | 1H, bối cảnh 1D | Nến đảo chiều tăng đã xác nhận + kiểm tra đáy bằng nhau + MA20 đi ngang/đảo lên; cần ≥ 3/5 điều kiện khung ngày; thoát theo "cờ đỏ" + phá cấu trúc |
| H24 | Spike & Ledge | M5/M15 | Tên trên ảnh 1, **chưa có định nghĩa kiểm chứng** — cần owner cung cấp mô tả trước khi đưa vào danh sách |

## 8. Dữ liệu và API

- Mở rộng `GET /admin/api/chart` thành các khối: `header` (CK-1), `mtf` (CK-2), `zones` (CK-3), `profiles` (CK-4), `vsa`/`zigzag` (CK-5), `setups` (CK-6), `positions` (CK-7), `levels`/`events`/`narrative` (CK-10), `status` (CK-9). Mỗi khối có `formula_version`, `as_of`, nguồn; thiếu dữ liệu trả lý do, không trả 0.
- Fingerprint theo khối để polling chỉ tải khối đổi; header 1–2 giây, phần còn lại 5 giây; cân nhắc SSE ở giai đoạn sau.
- Feature nặng (TPO, VSA, vùng ICT) tính tăng dần và lưu cache theo `(mã, khung, nến đóng cuối)`; không tính lại toàn bộ mỗi lần polling.
- Không script/style nội tuyến; mọi số hiển thị làm tròn theo UI kit, số đầy đủ ở `title`.

## 9. Thứ tự triển khai

```text
CK-0 (merge SIG + UI kit cho /admin/chart)
  ├─ CK-1 (dải thông số) ── CK-9 (dock + trạng thái)
  ├─ CK-2 (ma trận đa khung) ── CK-6 (bảng Entry) ── CK-12 (giám sát bot)
  ├─ CK-3 (ICT) ─┬─ CK-4 (TPO/nPOC)
  │             └─ CK-5 (VSA/ZigZag)
  ├─ CK-7 (vị thế + tương tác) ── CK-8 (vẽ + lớp + nhiều chart)
  └─ CK-10 (cột phải) · CK-11 (quét + watchlist)
CK-3/CK-5 + SIG-5 (kiểm định) ── CK-13 (giả thuyết H21–H24)
```

Giá trị sớm nhất cho người quản lý: CK-0 → CK-1 → CK-2 → CK-6 (biết thị trường đang thế nào và bot còn thiếu gì). Migration xếp tuần tự để giữ một Alembic head.

## 10. Kiểm thử cho mọi gói

- Unit: golden dataset tính tay; test "streaming = batch"; test DST; test thiếu dữ liệu (taker buy NULL, stream ngắt).
- Playwright in-process (CSP thật): 360/390/768/1024/1280/1440px, sáng và tối; không tràn ngang; vùng chạm ≥ 44px; mỗi lớp bật/tắt được; chú giải không chỉ dựa vào màu.
- Compose E2E: luồng nến → feature → chart JSON → trình duyệt với role DB thật; quyền xem bot; không request ký.
- Rà ảnh trên stack local cho từng gói trước khi commit (như vòng UI kit v1.1).

## 11. Quyết định cần owner

| # | Quyết định | Ảnh hưởng |
| --- | --- | --- |
| 1 | Merge nhánh SIG (`claude/project-progress-review-wp07-d70431`) vào `dev` trước khi bắt đầu CK | CK-0, mọi gói |
| 2 | Chấp nhận quy ước ICT của dự án (FVG/IFVG/BPR/OB theo §7 CK-3) và bảng giờ killzone/Silver Bullet/macro | CK-3, CK-6, H21 |
| 3 | Bộ lọc khung lớn (Chặn/Cảnh báo/Tắt) nằm ở overlay bot v2 hay tham số chiến lược | CK-2 |
| 4 | Lưu bản vẽ: chỉ trình duyệt hay vào DB (bảng mới, RLS) | CK-8 |
| 5 | Lịch sự kiện kinh tế: owner tự nhập (đề xuất) hay mua/đăng ký nguồn ngoài | CK-6, CK-10 |
| 6 | Có cần market cap/FDV/xếp hạng (nguồn ngoài Binance) không | CK-11 |
| 7 | Mô tả setup "Spike & Ledge" và các quy tắc "Green flag" mong muốn | CK-13 |
| 8 | Nâng Lightweight Charts 5.2.1 → 5.3.0 khi làm CK-8 (ví dụ primitive mới) | CK-8 |

## 12. Rủi ro và giới hạn

- **Định nghĩa ICT/SMC/VSA không thống nhất** giữa các nguồn (IFVG kích hoạt bằng đóng cửa hay râu; BPR có cần IFVG; giờ killzone NY AM 7–10 hay 8–11). Dự án chốt một quy ước, ghi version và không tuyên bố đây là "chuẩn ICT".
- **Chưa có bằng chứng độc lập** rằng các khái niệm này cho lợi thế sau phí; giai đoạn đầu chỉ để mô tả, gắn nhãn và làm điều kiện hiển thị.
- **Nhiều lớp vẽ làm chart chậm và rối**, nhất là mobile: giới hạn số đối tượng, preset lớp, mặc định tối giản.
- **Đếm ngược và bid/ask** phụ thuộc đồng hồ máy chủ và stream; khi lệch hoặc ngắt phải hiện rõ (bài học lệch đồng hồ Docker 41 giây ngày 15/11).
- **Bộ quét** dễ bị hiểu là khuyến nghị mua: ghi rõ là thống kê biến động, không phải tín hiệu.
- Không gói nào cho phép gửi lệnh Binance hoặc bật SHADOW/PAPER_GATE/Demo/LIVE.

## 13. Nguồn đã tham khảo (15/11/2026)

Các nguồn dưới đây chủ yếu là nhà cung cấp chỉ báo và trang đào tạo, **không** phải tài liệu gốc của ICT hay sách gốc của Tom Williams; dùng để đối chiếu cách hiểu phổ biến, không phải định nghĩa có thẩm quyền.

- FVG/IFVG/BPR/OB: https://www.luxalgo.com/library/concept/balanced-price-range/ · https://www.tradezella.com/learning-items/key-ict-concepts · https://www.tradingview.com/script/UBIzzw2R-ICT-Balanced-Price-Range-TradingFinder-BPR-FVG-IFVG · https://in.tradingview.com/script/ib4uqBJx-ICT-Concepts-LuxAlgo/
- Killzone, Silver Bullet, macro: https://www.luxalgo.com/library/concept/killzones/ · https://www.luxalgo.com/library/concept/silver-bullet.md · https://backtrex.com/en/blog/ict-killzone-session-times-complete-schedule · https://tradingfinder.com/education/forex/ict-kill-zones
- TPO, POC, Value Area, nPOC: https://in.tradingview.com/support/solutions/43000713306-time-price-opportunity-tpo-indicator/ · https://help.cqg.com/cqgic/25/Documents/marketprofilevalueareasmpva.htm · https://www.luxalgo.com/library/indicator/e6y9zmfW-tpo-levels-with-poor-h-l-single-prints-npocs/ · https://info.tradovate.com/tpo-profile-charts
- VSA: https://www.luxalgo.com/library/concept/volume-spread-analysis.md · https://www.luxalgo.com/library/concept/no-demand-no-supply-bars/ · https://www.luxalgo.com/library/concept/climactic-action.md · https://atas.net/volume-analysis/basics-of-volume-analysis/vsa-and-cluster-analysis-buying-and-selling-climax-patterns/
- Lightweight Charts plugin/primitive: https://tradingview.github.io/lightweight-charts/docs/plugins/intro · gói vẽ bên thứ ba đã xem và **không chọn**: https://cdn.jsdelivr.net/npm/lightweight-charts-drawing@0.1.1/README.md · https://www.pkgstats.com/pkg:lwc-plugin-drawing-tools
- Binance (đã kiểm ở các vòng trước, kiểm lại trước khi code CK-1): stream route `/public` `/market` — https://developers.binance.com/docs/derivatives/change-log ; book ticker và premium index — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api
