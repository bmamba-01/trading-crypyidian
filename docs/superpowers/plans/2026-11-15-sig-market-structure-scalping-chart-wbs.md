# WBS SIG — Cấu trúc thị trường, thanh khoản phiên, áp lực mua/bán, scalping và chart kiểu TradingView

**Ngày:** 2026-11-15 · **Trạng thái:** SIG-0 (lưu trữ + collector), SIG-1, SIG-3 (lát hiển thị) và SIG-7 đã triển khai lát đầu (15/11/2026); các gói khác là kế hoạch · **Chế độ:** PAPER/replay; chỉ dữ liệu thị trường công khai; không route order, không bật SHADOW/PAPER_GATE/Demo/LIVE. <br>
**Nghiên cứu nền:** [đối chiếu tín hiệu, 3 chỉ báo, scalping, chart](../../research/signal-scalping-chart-research-2026-11-15.md). <br>
**Mở rộng từ:** [khảo sát 14/11](./2026-11-14-feature-survey-and-manager-insights.md) (DATA-1, IND-1, IND-2, IND-4, MI-1, MI-6, TG-1), [kế hoạch công thức + Strategy Card](./2026-11-14-strategy-formula-and-card-readiness.md) (BT-1, SC-1), [WBS Trader Workbench](./2026-11-03-wp07-trader-workbench-wbs.md) (WB07A chart, WB07B Telegram), [WP-SC](./2026-11-05-wp-sc-strategy-card-wbs.md), [WP06](./2026-11-01-wp06-learning-wbs.md) (hiệu chuẩn).

## Mục tiêu sản phẩm

Người quản lý mở một trang chart giống TradingView: nến + khối lượng tô theo áp lực mua/bán, phiên Á/Âu/Mỹ, mức thanh khoản và trạng thái quét, swing/BOS/CHoCH, trendline tự động, volume profile có POC/VAH/VAL. **Chính các lớp này** là feature mà chiến lược dùng để ra tín hiệu (một mã tính, hai nơi dùng). Tín hiệu hiện dạng thẻ: hướng, vùng vào, SL, TP1–TP3, R:R ròng, hạng tin cậy (hoặc % đã hiệu chuẩn), checklist điều kiện có giá trị thật, điều kiện mất hiệu lực. Có thêm họ chiến lược scalping (M1–M5) chạy PAPER sau khi qua backtest.

## Quy ước mã gói

Họ mã mới `SIG-n` (tránh va chạm nhãn WP-07: Plan 07 = OPS07, Trader Workbench = WB07 theo [gap analysis 04/11](./2026-11-04-next-wbs-gap-analysis.md)). Mỗi gói ghi rõ mã cũ được mở rộng. File plan con theo mẫu `YYYY-MM-DD-sigN-<slug>.md`.

## Phát hiện từ kiểm kê code (15/11/2026)

| # | Phát hiện | Vị trí | Ảnh hưởng |
| --- | --- | --- | --- |
| G1 | Kline parser bỏ `[8]` số giao dịch, `[9]` taker buy base, `[10]` taker buy quote | `adapters/binance_usdm/market_data.py:496-530`; `market_bar` không có cột | Không tính được delta/CVD dù dữ liệu sàn có sẵn |
| G2 | Không có collector nến trực tiếp; adapter public chưa được khởi tạo trong `src` | `app.py:459` chỉ đọc bar đã lưu | Mọi tín hiệu vẫn từ seed/replay (RT-1/DATA-1) |
| G3 | Runtime chỉ nhận 15m/1h/4h dù config nhận 1m–1d | `application/strategy_market_requirements.py:13-19,78-80` | Không chạy được M1/M5 |
| G4 | Card release thiếu trong `FEATURES_BY_RELEASE` → `feature_requirements_for_release` ném lỗi | `strategy_market_requirements.py:20-67`; gọi từ `decision_snapshot_builder.py:82`, `market_signal_composition.py:172` | Card `SC-…-VOL-BREAKOUT-001` đăng ký nhưng không lấy được snapshot qua runtime |
| G5 | `planner_bridge` bỏ `confidence`, `risk_reward_ratio`, zone của card | `strategy_pipeline/planner_bridge.py:30-112` | Mất thông tin cho thẻ tín hiệu |
| G6 | `TradePlan` chỉ có một target | `domain/risk/planner.py:266-305` | Chưa có TP1–TP3/chốt từng phần |
| G7 | `AntiConsensusAnalyzer` khởi tạo nhưng không gọi; `has_consensus_warning` gán cứng `False` | `admin/strategy_card_service.py:88,122-123` | Không phát hiện nguồn trùng lặp |
| G8 | `geometry.py` có pivot/swing đã xác nhận nhưng không được dùng ngoài test | `domain/strategy/geometry.py:43` | Tái dùng cho SIG-1 |
| G9 | Chart chưa có volume, chỉ báo phủ, phiên, profile; Lightweight Charts 5.2.1 | `static/trade-chart.js`; `static/vendor/` | Nâng cấp SIG-7 |
| G10 | Không có MACD, OBV, delta/CVD, BOS/CHoCH, trendline, phiên, sweep, volume profile | grep 0 kết quả trong `src` | SIG-1 |

## WBS

### SIG-0 — Dữ liệu mua/bán chủ động và nến khung ngắn (mở rộng DATA-1, WB07C)

1. Migration nullable `market_bar.trade_count`, `taker_buy_base_volume`, `taker_buy_quote_volume` (Decimal); downgrade bị chặn khi đã có giá trị; dữ liệu cũ giữ NULL, **không ước lượng**.
2. Parser REST/Vision giữ `[8]/[9]/[10]`; bar hash/manifest v4 đưa các trường này vào hash, reader v3 vẫn đọc được (theo mẫu manifest v2→v3 quote volume).
3. Collector nến đã đóng chỉ đọc dữ liệu công khai (REST phân trang hoặc WebSocket `/market`), interval 1m/5m/15m/1h/4h cho watchlist được owner duyệt; cursor/gap/quality; readiness báo `market_dispatch` thật thay `not_configured`. Phần dispatcher PAPER là RT-1.
4. (Tùy chọn, sau quyết định owner) aggTrades cuộn có retention giới hạn cho delta theo giá; REST chỉ giữ 48 giờ.

**Acceptance:** fixture REST có taker buy → DB → snapshot giữ nguyên Decimal; bar cũ NULL không làm feature delta "bịa" giá trị (feature trả thiếu dữ liệu); E2E collector fixture → bar → cursor → resume không trùng. **Phụ thuộc:** quyết định owner #1 (quyền dữ liệu PAPER nội bộ), #2 (symbol/khung).

**Đã triển khai (15/11/2026, mục 1–2 cho REST):**
- Migration `e3a5c7e9f1b4` (head mới, sau `d2e4f6a8b0c1`): `market_bar.trade_count BIGINT`, `taker_buy_base_volume`, `taker_buy_quote_volume NUMERIC(38,18)` nullable; CHECK không âm, taker base ≤ volume, taker quote ≤ quote volume; downgrade raise `SIG0_MARKET_BAR_TAKER_FLOW_DOWNGRADE_REQUIRES_EMPTY_COLUMNS` khi đã có giá trị rồi mới drop.
- `OHLCVBar` thêm 3 trường tùy chọn có kiểm biên; `_parse_kline` REST đọc `[8]/[9]/[10]` (lỗi định dạng → `MarketDataContractError`); REST backfill chép sang bar; `MarketDataRepository` ghi/đọc; checksum nội dung bar backfill chỉ thêm khóa khi có giá trị nên bar cũ giữ nguyên checksum.
- Chart (`load_recent_bars`) trả `taker_buy_volume` → lớp delta/CVD/profile mua-bán tự bật khi nến có dữ liệu.
- **Quyết định owner #1 (15/11/2026):** đồng ý lưu dữ liệu thị trường công khai Binance cho PAPER nội bộ.
- **Collector (mục 3) đã triển khai:** `application/market_bar_collector.py` + `workers/market_bar_collector_worker.py`, chạy trong `ingestion-worker` khi `TRADING_BOT_MARKET_COLLECTOR_ENABLED=true` (mặc định tắt; chỉ PAPER). Luồng mặc định `BTCUSDT:1m,5m,15m,1h,4h;ETHUSDT:1m,5m,15m,1h,4h` (quyết định #2 dùng mặc định đề xuất, đổi qua `TRADING_BOT_MARKET_COLLECTOR_STREAMS`), tick 30 giây, lần đầu lấy 1000 nến/luồng. Mỗi luồng: cursor riêng `BINANCE_USDM_REST_COLLECTOR`, tái dùng `BinanceRestBackfillRunner` (rights gate PAPER_DEMO **trước** request mạng, kiểm từng nến), ghi nến + cursor cùng transaction, khóa idempotency trùng với backfill REST; cursor chỉ tiến tới sau nến cuối thực nhận. Readiness `ingestion-worker` thêm `market_collector` (ok/degraded với bộ đếm; chỉ `not_ready` khi task dừng). Chart chỉ đọc một nguồn/khung, ưu tiên REST công khai → Binance → mẫu, và ghi rõ khi là dữ liệu mẫu.
- **Bật chạy thật (runbook):** (1) ở "Nguồn dữ liệu" tạo nguồn `BINANCE_USDM_REST`, gửi policy chờ duyệt với mục đích **PAPER demo**, phạm vi giấy phép (provider/product/venue/contract viết hoa, symbol/khung trùng cấu hình collector), tải bằng chứng; (2) **một người duyệt khác** người gửi duyệt quyền (self-approve bị 403); (3) đặt `TRADING_BOT_INGESTION_WORKSPACE_ID` = workspace đó, `TRADING_BOT_MARKET_COLLECTOR_ENABLED=true`, khởi động lại `ingestion-worker`; (4) kiểm `/health/ready` của worker và trang `/admin/chart`.
- **Đang chạy trên stack local (15/11/2026):** người duyệt local tạo bằng `scripts/seed_local_reviewer.py`; owner gửi policy `BINANCE_USDM_REST` (PAPER demo, phạm vi BINANCE/USDM_KLINES/PUBLIC_REST/PERPETUAL, BTCUSDT+ETHUSDT × 1m/5m/15m/1h/4h, hạn 15/11/2027) kèm bản ghi quyết định owner làm bằng chứng (ghi rõ không phải giấy phép Binance; điều khoản tham chiếu https://www.binance.com/en/terms), reviewer duyệt độc lập. `ingestion-worker` chạy collector cho workspace của owner: 10 luồng × 1000 nến lịch sử có taker buy, sau đó thu tăng dần mỗi 30 giây; readiness `market_collector: ok`.
- **Chưa làm:** parser Binance Vision, manifest v5 ghim trường taker, aggTrades, ghi `ingestion_gap` khi thiếu nến giữa khoảng (hiện chỉ đếm `missing`), xung đột khóa idempotency nếu một backfill REST chạy với `feed_delay > 0` trùng khoảng với collector (nội dung khác → conflict), WebSocket `/market` thay polling.

### SIG-1 — Thư viện feature cấu trúc/phiên/khối lượng không nhìn trước (mở rộng IND-1, IND-2, IND-4, FX-6)

Mọi feature có `formula_version`, chạy được tăng dần, và có `confirmed_at`/`available_at`; chỉ dùng từ thời điểm đó. Cài clean-room, không chép mã Pine.

1. **Cấu trúc:** pivot đã xác nhận (tái dùng `geometry.py`), nhãn HH/HL/LH/LL, máy trạng thái BOS/CHoCH (phá bằng giá đóng), trendline tự động từ 2 pivot (dung sai/vô hiệu theo ATR), 7 trạng thái cấu trúc theo dấu độ dốc, đa khung bằng nến khung lớn đã đóng.
2. **Phiên & thanh khoản:** lịch phiên Tokyo/London/New York bằng `zoneinfo` (DST), mức đỉnh/đáy phiên, PDH/PDL, PWH/PWL, đỉnh/đáy bằng nhau; máy trạng thái armed → swept → rejected/accepted/expired; độ rộng phiên theo phân vị; base rate kèm n.
3. **Áp lực mua/bán:** delta = 2·takerBuy − volume, CVD reset theo ngày UTC/phiên, delta chuẩn hóa theo phân vị; volume profile phiên/ngày đã đóng và bản developing tới t−1 (bins, POC, VAH/VAL 70%); phân bổ delta vào bin theo đường đi OHLC (ghi rõ là xấp xỉ); mất cân bằng E, Eref và phân loại hoạt động/mất cân bằng.
4. **Chỉ báo bổ sung còn thiếu:** MACD (12,26,9 với warm-up ≥ 3×), anchored VWAP neo theo quy tắc (đầu tuần/tháng/sự kiện đã xác nhận); OBV tùy chọn.
5. Golden dataset nhỏ tính tay cho từng feature; test property "streaming = batch"; test DST (tuần đổi giờ London/New York lệch nhau); test bar thiếu taker buy.

**Acceptance:** không feature nào đổi giá trị lịch sử khi thêm nến mới (trừ bản developing được đánh dấu); `feature_snapshot` lưu hash công thức; glossary mục AA có định nghĩa và nguồn.

**Đã triển khai (15/11/2026, lát 1):**
- `domain/market/structure.py` (`sig1-structure-v1`, `sig1-trendline-v1`): swing đã xác nhận tái dùng `geometry.find_confirmed_pivots`, nhãn HH/HL/LH/LL, máy trạng thái BOS/CHoCH bằng giá đóng (kiểm phá trước, nạp swing xác nhận sau trong cùng nến), trendline tự động (dung sai 0,3 ATR, vô hiệu 0,5 ATR, bị thay khi có line mới cùng phía), trạng thái cấu trúc theo dấu độ dốc.
- `domain/market/sessions.py` (`sig1-session-v1`, `sig1-liquidity-v1`): phiên Tokyo/London/New York theo `zoneinfo` (DST theo từng thị trường), đỉnh/đáy phiên, PDH/PDL theo ngày UTC đã đóng, máy trạng thái armed → rejected (quét)/accepted → expired; chỉ phiên/ngày `complete` mới sinh mức.
- `domain/market/volume_profile.py` (`sig1-volume-profile-v1`): delta/CVD từ taker buy (thiếu → `None`, không ước lượng), volume profile N bin, POC, Value Area 70%, mua/bán theo bin, E/Eref và phân loại mất cân bằng.
- `domain/market/features.py`: thêm `macd` (12/26/9).
- **Chưa làm trong lát này:** PWH/PWL, đỉnh/đáy bằng nhau, độ rộng phiên theo phân vị và base rate kèm n, lookback pivot thích ứng theo ATR, anchored VWAP, OBV, ghi feature mới vào `feature_snapshot` (chưa có chiến lược nào dùng — thuộc SIG-2/SIG-4), golden dataset từ dữ liệu thật.

### SIG-2 — Runtime đa khung ngắn và sửa đường snapshot cho card (mở rộng RT-1, SC-1)

1. Mở `strategy_market_requirements` cho 1m/3m/5m/30m theo release; giới hạn số khung/feature mỗi release để kiểm soát tải.
2. Sửa G4: card release có feature requirement được khai báo (sinh từ card hoặc bảng tường minh), kèm test card → snapshot → candidate qua runtime.
3. Chart admin thêm 1m/3m/30m (M10 không phải interval Binance; không resample để tạo M10).

**Acceptance:** H06 15m và một card chạy hết đường snapshot → decision trên fixture; khung không hỗ trợ báo lỗi tiếng Việt có mã lý do.

**Đã triển khai (15/11/2026):** G4 sửa — `CARD_VOL_BREAKOUT_RELEASE` (`sc-binance-usdm-vol-breakout-001-v1.0.0`) khai báo khung vào 1h + `atr_14` trong `strategy_market_requirements`, khớp release trong `strategy_registry()` (test). Khung 1m/3m/5m/30m cho runtime vẫn chưa mở.

**Đã triển khai (16/11/2026, chưa commit):** `strategy_market_requirements.TIMEFRAME_INTERVALS` mở 1m/3m/5m/30m (giữ 15m/1h/4h); giới hạn `MAX_TIMEFRAMES_PER_RELEASE = 3`, `MAX_FEATURES_PER_TIMEFRAME = 8` kiểm khi nạp module (`validate_requirements`); lỗi `MarketRequirementError(code, message)` (vẫn là `ValueError`, giữ câu cũ để không đổi hành vi) với mã `TIMEFRAME_UNSUPPORTED`, `RELEASE_UNSUPPORTED`, `TOO_MANY_TIMEFRAMES`, `TOO_MANY_FEATURES`; producer (`market_signal_composition`) báo mã khi bot yêu cầu khung chưa hỗ trợ. Chart admin đã có 1m/3m/30m từ SIG-7. **Không sửa** `_TIMEFRAME_INTERVALS` trong `domain/strategy/rules.py` vì `release_content_hash` băm mã nguồn module rules → H18–H20 (SIG-4) sẽ nằm ở module riêng.

### SIG-3 — Hợp đồng tín hiệu v2: vùng vào, thang chốt lời, điều kiện mất hiệu lực (mở rộng MI-1, MI-2, SC-1)

1. `StrategyCandidate` v2 (giữ v1 đọc được): `entry_zone_low/high`, `stop_price`, `targets[]` (giá + tỷ lệ chốt), `setup_id`, `invalidation[]` có cấu trúc, `expires_at`, `checklist[]` (`condition_key`, `actual`, `threshold`, `operator`, `passed` — chính là MI-1), `confidence_tier` + `calibrated_probability` (nullable).
2. Kiểm hợp lệ theo hướng: lệnh bán phải có SL > vùng vào > TP1 > TP2 > TP3 (ảnh tham khảo có TP3 sai phía — phải bị chặn).
3. Sửa G5: `planner_bridge` giữ zone/R:R/score của card.
4. Sửa G6: `TradePlan` nhiều target; R:R ròng tính cho từng TP sau phí; PAPER monitor chốt từng phần + dời SL về hòa vốn sau TP1 (tùy recipe), ghi ledger từng phần.

**Acceptance:** replay cùng dữ liệu cho cùng hash; partial exit reconcile với ledger; báo cáo và CSV dùng dữ liệu từng phần đúng.

**Đã triển khai (15/11/2026, lát 1 — chỉ hiển thị, không đổi planner/ledger/thực thi):**
- `domain/strategy/checklists.py` (`sig3-checklist-v1`): checklist có cấu trúc (key, nhãn, giá trị thực, phép so, ngưỡng, đạt/không/—) cho H01/H03/H06. Đặt **ngoài** `rules.py` vì `release_content_hash` băm mã nguồn module rules — sửa rules sẽ đổi danh tính mọi release đang ghim. Test parity: rule ra candidate ⇔ mọi điều kiện đạt (14 kịch bản).
- `BotDecision.checklist_json` + khóa `checklist` trong payload quyết định (cả CANDIDATE lẫn ABSTAIN), chỉ thêm khi có giá trị nên hash quyết định cũ không đổi; lỗi checklist không làm hỏng quyết định của rule.
- `domain/strategy/signal_card.py` (`sig3-card-v1`): vùng vào (tham chiếu → giá vào kế hoạch), SL, mục tiêu recipe + mốc 1R/2R/3R tham chiếu với R thô, mất hiệu lực từ recipe, độ tin cậy "Chưa hiệu chuẩn"; `validate_price_ladder` chặn SL/TP sai phía (ví dụ lệnh bán có TP3 trên vùng vào).
- `BotReportingService.get_latest_signal` (REPORT_READ, bot tồn tại) đọc 20 quyết định gần nhất, chỉ dùng quyết định qua kiểm hash, ghép kế hoạch đã duyệt theo `candidate_id`; thẻ hết hạn được đánh dấu và không vẽ lên chart.
- Trang `/admin/chart`: khối "Thẻ tín hiệu gần nhất" + "Quyết định mới nhất: không vào lệnh" kèm checklist; lớp "Thẻ tín hiệu (SL/TP)" vẽ đường giá SL/vào/TP khi tín hiệu cùng mã và còn hiệu lực.
- **Chưa làm (lát sau):** `TradePlan` nhiều target và chốt từng phần trong PAPER (G6), giữ confidence/R:R/zone của card qua `planner_bridge` (G5), net R:R từng mục tiêu (cần tách công thức planner thành hàm dùng chung), thẻ trong tin Telegram (TG-1), checklist cho Strategy Card L0–L6.

**Đã triển khai (16/11/2026, lát 2 — G6 lõi, chưa commit):**
- `domain/risk/take_profit.py` (`sig3-ladder-v1`): `TakeProfitLadder` (1–3 bậc, R tăng dần, tổng tỷ lệ = 1, tùy chọn dời stop về entry sau TP1), `build_targets` (giá theo R của khoảng entry→stop, làm tròn bảo thủ ra xa entry, chặn trùng sau làm tròn), `net_reward_to_risk` dùng chung công thức với planner, R:R bình quân theo tỷ lệ chốt.
- **Không sửa `ExitRecipe`** (giữ `release_content_hash`); thang là cấu trúc riêng truyền vào `TradePlanner.plan(..., ladder=)`. `TradePlan.targets` mặc định rỗng và **chỉ vào hash khi có** → plan/checkpoint/quyết định cũ giữ nguyên hash (test hash kiểu cũ). Cổng R:R ròng áp cho R:R bình quân của thang; mục tiêu cuối là `target_price`.
- PAPER: `PaperPosition` mang `targets`, `targets_hit`, `initial_quantity`, `quantity_step`; `position_monitor` chốt từng phần (stop vẫn ưu tiên cùng nến; nhiều mục tiêu cùng nến gộp và chốt ở giá mục tiêu đầu tiên chạm — bảo thủ; mục tiêu cuối đóng hết `TAKE_PROFIT`; làm tròn xuống theo bước khối lượng); gateway đóng một phần với khóa `exit:{pos}:{bar}:TP{i}`; dời stop về giá vào thực tế sau TP1 (chỉ khi tốt hơn stop hiện tại), hiệu lực từ nến sau.
- Ledger: `paper_lifecycle` ghi fill EXIT lý do `TAKE_PROFIT_{i}` (ledger tự ghi REDUCED) và `STOP_UPDATED` lý do `BREAKEVEN_AFTER_TP{i}`; trade vẫn OPEN tới khi đóng hết. Checkpoint chỉ ghi khóa thang khi có thang (checkpoint cũ khôi phục được — test).
- Recovery: chấp nhận EXIT trên trade OPEN **chỉ khi** vị thế có thang, đã chạm ≥ 1 mục tiêu, chưa đóng và số lần chốt ≤ số mục tiêu đã chạm; STOP_UPDATED kiểm khối lượng theo các mức vị thế từng có.
- **Còn mở:** G5 (`planner_bridge` giữ vùng vào/R:R/điểm của card dạng có cấu trúc); lý do thoát từng fill trong báo cáo/CSV; hiển thị thang trên thẻ tín hiệu; Compose E2E chốt từng phần (làm cùng acceptance SIG-4: fixture M5 → candidate → plan → khớp → TP1 → đóng).

### SIG-4 — Họ chiến lược scalping H18–H20 (mới; chỉ PAPER, `EXPERIMENTAL_UNVALIDATED`)

1. H18 quét + lấy lại mức phiên/PDH/PDL (M5/M15); H19 hồi về VWAP/POC trong RANGE (M1–M5); H20 BOS + retest có mất cân bằng (M5/M15). Định nghĩa trong [nghiên cứu §6](../../research/signal-scalping-chart-research-2026-11-15.md#6-bộ-setup-scalping-đề-xuất-giả-thuyết-chỉ-paper); bổ sung vào `strategies-2026.md`.
2. Hồ sơ thực thi "scalping": lệnh limit mô phỏng maker (khớp khi giá xuyên mức ≥ 1 tick, ghi giới hạn hàng đợi), hết hạn lệnh chờ sau N nến, phí maker/taker tách riêng.
3. Cổng chi phí cứng: stop ≥ 3× chi phí khứ hồi ước tính; R:R ròng ≥ ngưỡng; bộ lọc phiên London/New York; chặn ±10 phút quanh mốc funding; cooldown sau thua liên tiếp.
4. Tham số cố định trước backtest; mỗi lần đổi tham số tính là một lượt thử (BT-1).

**Acceptance:** unit cho từng điều kiện vào/vô hiệu; E2E PAPER từ fixture M5 → candidate → plan → fill limit → TP1 chốt một phần → đóng; không có order Binance.

**Đã triển khai lát 1 (16/11/2026, chưa commit):**

- `domain/strategy/scalping_rules.py`: `H18SweepReclaimRule` (`h18-v1`), `H19VwapRangeReversionRule` (`h19-v1`), `H20BreakRetestRule` (`h20-v1`); bộ kiểm đầu vào point-in-time riêng cho 5m/15m (nhân đôi `rules._inputs` có chủ đích để không đổi hash release cũ); bộ lọc chung phiên London/New York, ±10 phút funding 00/08/16 UTC, blackout vĩ mô, stop ≥ 3× chi phí khứ hồi taker, vị thế phẳng. Định nghĩa chi tiết trong `strategies-2026.md` mục H18–H20.
- Đăng ký vào registry sản phẩm (`application/strategy_catalog.scalping_rules`), `StrategyRegistry()` trần giữ nguyên 5 release; khai báo dữ liệu trong `strategy_market_requirements` (5m; H20 thêm 15m).
- Thang chốt lời theo release (`ladder_for_release`: TP1 1R 50% + hòa vốn, TP2 2R 50%), truyền vào planner ở `PaperDecisionCycle.prepare` — release khác nhận `None` (không đổi hành vi).
- Test: `tests/unit/test_sig4_scalping_rules.py` (điều kiện vào/vô hiệu từng setup, các cổng chung, biên cửa sổ funding, đăng ký + hash, kế hoạch có thang sau phí VIP0) và `tests/unit/test_sig4_acceptance.py` (chấp nhận trong tiến trình: nến M5 → H18 → `PaperDecisionCycle` đường settings đã publish → intent PAPER → khớp 1m → TP1 chốt một phần + hòa vốn → TP2 đóng; không kết nối sàn).
- **E2E (16/11/2026):** `tests/e2e/test_sig4_scalping_paper.py` + probe `tests/e2e/probes/sig4_scalping_paper.py` + fixture `tests/fixtures/sig4_runtime_replay.py`. "Đồng hồ có kiểm soát" = thời gian của dữ liệu + `clock=` tiêm vào `PaperLedgerRepository`/`RiskReservationRepository` (không sửa mã production). Pha `lifecycle` (role trading-runtime, chặn socket): snapshot từ `build_decision_snapshot_from_bars` thật → `PaperDecisionCycle` (registry sản phẩm) → `record_evaluation`/`reserve` → `PaperLifecycleCoordinator` với ledger + checkpoint + lease/fencing token thật: khớp 10:05 → TP1 (fill EXIT `TAKE_PROFIT_1`, `STOP_UPDATED` `BREAKEVEN_AFTER_TP1`) → TP2 đóng trade (net PnL ledger = gateway) → coordinator mới hydrate từ checkpoint khớp. Pha `replay` (db-provisioner): replay H18 qua `run_backtest_dataset_runtime_replay` và lưu outcome bằng role `crypidian_replay_writer` (report hash/net PnL khớp). Cũng đóng mục "Compose E2E chốt từng phần" của SIG-3.
- **Ràng buộc phát hiện khi làm E2E:** PostgreSQL có `CHECK (risk_reservation.expires_at > created_at)` với `created_at = now()` thật, nên kịch bản ledger chạy ở **cùng thứ/giờ của tuần tương lai** (`same_slot_after`); checkpoint từ revision 2 cần lease runtime + fencing token (MB-05); bộ tính đặc trưng chung đòi đủ warmup mọi đặc trưng (EMA200) → chiến lược 5m cần ≥ 200 nến M5 (~17 giờ) trước khi có tín hiệu; snapshot builder tính đủ `adx_14`/`efficiency_ratio_40` cho 5m (đã thấy giá trị READY trong replay).
- **Chưa làm:** E2E qua producer `market-signal-worker` + vòng runtime production (`create_app`) cho release scalping — bộ lọc phiên/funding đọc giờ thật nên chỉ chạy được khi đồng hồ có kiểm soát ở producer/runtime; mô phỏng limit maker, hết hạn lệnh chờ, cooldown sau chuỗi thua.

### SIG-5 — Kiểm định scalping (mở rộng BT-1, WB07C)

1. Bundle 1m/5m với taker buy; fill ở giá mở nến sau; phí + trượt giá theo hồ sơ; walk-forward; ghi số lượt thử.
2. Báo cáo theo setup: n, tỷ lệ thắng so với tỷ lệ hòa vốn, kỳ vọng sau phí, phân phối R, theo phiên/giờ; cờ "chưa đủ mẫu".
3. Kiểm độ nhạy chi phí (trượt giá ×2, chỉ taker) — setup chỉ được đề xuất bật PAPER khi còn kỳ vọng dương dưới kịch bản xấu.

**Acceptance:** cùng bundle → cùng hash báo cáo; tiêu chí bật PAPER được owner duyệt trước khi chạy (tránh chọn sau khi thấy kết quả).

**Đã triển khai lát 1 (16/11/2026, chưa commit) — bộ máy kiểm định; chưa có kết luận trên dữ liệu thật:**

- `domain/portfolio/scalping_validation.py` (`sig5-validation-v1`): `ScalpTrade` (R ròng = PnL ròng / rủi ro ban đầu), `walk_forward_folds` (khối liền nhau + embargo), `setup_report` (n, n ngoài mẫu, tỷ lệ thắng so với hòa vốn, kỳ vọng ròng và theo R, phân phối R, theo phiên/giờ UTC, theo fold, cờ chưa đủ mẫu, `report_hash` không phụ thuộc thứ tự), `paper_enable_verdict` (`sig5-paper-criterion-v1` = quyết định #5: ≥ 200 lệnh ngoài mẫu **và** kỳ vọng ròng > 0 ở kịch bản xấu; chỉ là đề xuất).
- `application/scalping_validation.py` (`sig5-costs-v1`): replay PAPER từng release qua `run_backtest_dataset_runtime_replay` ở kịch bản gốc (taker VIP0 0,05%, trượt giá cơ sở 0,02% — **giả định dự án**, chưa đo từ sổ lệnh) và xấu (trượt giá ×2, chỉ taker); planner nhận cùng chi phí; `validate_release` trả báo cáo + kết luận + `validation_hash`; `build_research_bundle` dựng bundle 5m/15m từ nến REST đã lưu.
- Replay dùng **cửa sổ 256 nến như producer** (`max_bars_per_timeframe`, mặc định `None` giữ hành vi BT-1): trước đây replay thấy toàn bộ lịch sử → đặc trưng lệch live và O(n²) theo độ dài dữ liệu. Đo trên dữ liệu thật: 20–45 ms/quan sát (chủ yếu tính feature snapshot); 1 tháng M5 ≈ 7 phút, 760 MB. Dựng observation cả 6 tháng một lần tốn > 3,8 GB/tiến trình → dùng `validate_release_chunks` (replay đoạn 30 ngày, mỗi đoạn tự mang warmup 72 h, gộp lệnh rồi mới chia fold) và CLI `--chunk-days`.
- `scripts/sig5_scalping_validation.py`: `fetch` (REST kline công khai, không khóa — **chỉ chạy khi owner đồng ý tải**) và `validate` (chỉ đọc file cục bộ, ghi JSON + bảng tóm tắt).
- Test: `tests/unit/test_sig5_scalping_validation.py` (fold/embargo, công thức báo cáo, hash, kết luận, runner trên bundle replay, cửa sổ observation, CLI offline).
**Lượt chạy dữ liệu thật đầu tiên (16/11/2026) — kết luận theo `sig5-paper-criterion-v1`: KHÔNG đề xuất bật PAPER cho setup nào.**

Dữ liệu: REST kline công khai Binance USDⓈ-M, 2026-05-01 → 2026-11-01 (tải 16/11/2026), 5m + 15m; đánh giá từ 2026-05-04 (warmup 72 h), 4 fold walk-forward, embargo 60 phút, đoạn replay 30 ngày, vốn 10 000 USDT, rủi ro 0,5%/lệnh. Kịch bản xấu = trượt giá 0,04%/lần khớp + taker 0,05%.

| Release × mã | n ngoài mẫu (xấu) | Thắng | Hòa vốn | Kỳ vọng R (gốc / xấu) | Kỳ vọng ròng xấu (USDT/lệnh) | Lý do | `validation_hash` (16 ký tự đầu) |
|---|---|---|---|---|---|---|---|
| h18-v1 BTCUSDT | 0 | — | — | — | — | thiếu mẫu, kỳ vọng không dương | d1b007724744e4e4 |
| h18-v1 ETHUSDT | 3 | 33,3% | 58,3% | −0,70 / −0,54 | −21,42 | thiếu mẫu, kỳ vọng âm | ea6059043729a69e |
| h19-v1 BTCUSDT | 0 | — | — | — | — | thiếu mẫu, kỳ vọng không dương | 947286559988080c |
| h19-v1 ETHUSDT | 0 | — | — | — | — | thiếu mẫu, kỳ vọng không dương | 7bd37f73e34e5348 |
| h20-v1 BTCUSDT | 116 | 25,0% | 48,1% | −0,25 / −0,25 | −10,51 | thiếu mẫu, kỳ vọng âm | c49a83d3c8e9e95e |
| h20-v1 ETHUSDT | 163 | 30,1% | 55,7% | −0,26 / −0,29 | −11,79 | thiếu mẫu, kỳ vọng âm | 172d518f03f8e095 |

- H20 âm ổn định ở **cả 4 fold** (BTC −0,11…−0,35R; ETH −0,18…−0,36R) và mọi phiên (New York tệ nhất ≈ −0,31/−0,35R); không lệnh nào đạt ≥ 2R (phân phối dồn ở −1R…0 — dừng hòa vốn/hết thời gian giữ). Phí ≈ 1/2 khoản lỗ ròng (BTC: phí 579 / lỗ 1 219 USDT) → âm cả trước phí.
- H18/H19 gần như không phát tín hiệu: 1 tháng BTC H18 chỉ ~14 lần quét-lấy lại trong phiên, 13 lần bị loại vì delta chưa đảo; H19 ETH 1 ứng viên/tháng (2 944 lần loại vì không đi ngang ADX/ER, 1 048 vì chưa lệch 2σ VWAP) nhưng 6 tháng vẫn 0 lệnh — planner loại `NET_REWARD_RISK_BELOW_MINIMUM` (R:R ròng thang 1R/2R sau phí 0,96/0,91 < 1,0; stop hẹp).
- Cửa sổ 256 nến 5m (~21 h, giống producer) không chứa trọn ngày UTC trước → PDH/PDL của H18 là đỉnh/đáy **một phần** ngày. Replay ↔ live vẫn khớp, nhưng ngữ nghĩa mức sai; sửa (cửa sổ ≥ 2 ngày hoặc nguồn mức ngày riêng) = đổi producer + release H18 mới → cần owner quyết.
- Đổi tham số rule sau khi xem kết quả này là **lượt thử mới** (trial mới), không phải sửa lượt này.

- **Lệch so với mô tả:** khớp trên nến 5m (chưa dùng 1m làm execution); "chỉ taker" trùng kịch bản gốc vì lát này chưa có limit maker; chưa ghi báo cáo vào DB/admin; chưa chạy trên dữ liệu thật (cần owner cho phép tải và xác nhận quyền dùng dữ liệu REST cho nghiên cứu).

### SIG-6 — Độ tin cậy có hiệu chuẩn và đồng thuận không trùng lặp (mở rộng WP06 calibration, MI-5)

1. Nhãn: P(chạm TP1 trước SL trong H nến) từ outcome đã lưu, theo setup.
2. Mô hình baseline logistic (hạ tầng trainer WP06), hiệu chuẩn isotonic/temperature, đánh giá walk-forward bằng Brier score, log-loss, reliability diagram 10 bin + n từng bin.
3. Hiển thị: chưa đạt ngưỡng hiệu chuẩn → chỉ hạng cao/trung bình/thấp; đạt → % kèm khoảng tin cậy và n.
4. Sửa G7: gọi `AntiConsensusAnalyzer` trên chuỗi điểm các điều kiện; checklist hiện "x/y điều kiện độc lập" với số nguồn độc lập hiệu dụng, cảnh báo khi nhiều điều kiện cùng đo một thứ.
5. Không bật SHADOW/PAPER_GATE; đây chỉ là hiển thị/xếp hạng.

**Acceptance:** không có con số % nào hiển thị khi chưa có báo cáo hiệu chuẩn; test xác nhận hai điều kiện tương quan cao bị gộp.

**Đã triển khai lát 1 (16/11/2026, commit `cd123a7`) — mục 4 (sửa G7) + kiểm mục 3:**

- `StrategyPipelineEngine.numeric_node_series`: chuỗi đầu ra L1–L3 theo từng nến, cùng công thức `execute` (tách `_l2_from_bars` dùng chung, không đổi `PIPELINE_ENGINE_VERSION` vì ngữ nghĩa giữ nguyên), không nhìn trước, bỏ nến vi phạm L0 ở mọi node.
- `application/strategy_pipeline/consensus.py` (`sig6-consensus-v1`): `card_consensus` gọi `AntiConsensusAnalyzer` (|r Pearson| ≥ 0,85), số **điều kiện độc lập** = số nhóm liên thông của đồ thị tương quan cao; < 50 nến hợp lệ → `insufficient_data` (không coi là trực giao).
- `StrategyCardAdminService(bars_provider=…)`: bỏ `has_consensus_warning = False` gán cứng; đo trên mã/khung đầu tiên thẻ khai báo, tối đa 300 nến đã đóng. `PostgresCardBarsProvider` đọc `market_bar` của workspace (phiên chỉ đọc, RLS, một nguồn như chart/bộ quét; không gọi mạng sàn), nối trong `create_app`.
- Giao diện: huy hiệu "Đã kiểm định (Orthogonal)" chỉ khi đo được và không có cặp tương quan cao; thiếu nến → "Chưa đủ dữ liệu tương quan"; KPI chỉ đếm thẻ đo được trực giao; hộp chi tiết hiện "x/y điều kiện độc lập · n nến · mã · khung".
- Thẻ mẫu `MEAN-REVERT-002`: L3 `zscore_normalizer` (trung bình các node, giá đóng cửa áp đảo) gần trùng L2 (trả giá đóng cửa) → cảnh báo, 3/4 điều kiện độc lập — đúng loại "đồng thuận giả" G7 cần bắt.
- Mục 3 đã đạt từ trước: thẻ tín hiệu chỉ hiện "Chưa hiệu chuẩn", Telegram không gửi độ tin cậy.
- **Chưa làm:** nhãn hiệu chuẩn (mục 1–2) — chưa có setup scalping đạt SIG-5 nên chưa có outcome để hiệu chuẩn; checklist H01/H03/H06 chưa có "x/y điều kiện độc lập" (cần chuỗi điểm điều kiện theo lịch sử); ma trận tương quan trực quan.

### SIG-7 — Chart kiểu TradingView (mở rộng WB07A)

1. Nâng Lightweight Charts 5.2.1 → 5.3.0: cập nhật `static/vendor/`, `ASSET-SOURCES.md` (SHA-256, URL, ngày), dependency inventory; `attributionLogo: false` + chân trang attribution tĩnh và link NOTICE (tránh `<style>` bị CSP chặn và tránh rò hostname qua `utm_source`).
2. Primitive tự viết dựa trên ví dụ chính thức (giữ header Apache-2.0): volume profile mua/bán + POC/VA, hộp phiên, mức thanh khoản theo trạng thái, trendline; marker swing/BOS/CHoCH/sweep/tín hiệu; price line entry zone/SL/TP1–3.
3. Pane: nến + volume histogram tô theo dấu delta; pane phụ CVD/RSI/MACD bật tắt.
4. Trang `/admin/chart` (symbol, khung 1m–1d, bật/tắt từng lớp, nhớ lựa chọn trong `localStorage` bọc try/catch); tái dùng trên bot detail/report.
5. API `GET /admin/api/chart` (xác thực, `no-store`, giới hạn bar) trả JSON tính bằng **cùng mã SIG-1**; polling `since` 5 giây khi tab hiện.
6. Mobile 360px: ẩn volume profile hoặc thu hẹp, vùng chạm ≥ 44px, danh sách sự kiện thay hover; Playwright `assert_responsive` 360–1280.

**Acceptance:** không inline script/style; marker/mức khớp feature snapshot; chart lỗi/thiếu dữ liệu taker buy hiện ghi chú rõ thay vì vẽ số 0.

**Đã triển khai (15/11/2026, lát 1):**
- Trang `/admin/chart` (sidebar "Biểu đồ thị trường") + API `GET /admin/api/chart` (polling 5 giây, `fp` → `unchanged`, `no-store`). Chọn bot (quyền `bot:report:read` trên bot đó, bot phải tồn tại) rồi chọn **mã có nến đã lưu** trong workspace; khung chưa có nến hiện mờ. Bot hiện chưa có trường symbol riêng nên không suy mã từ bot.
- `application/market_chart.py` dựng JSON bằng đúng mã SIG-1: nến, volume tô theo delta (hoặc theo chiều nến khi thiếu taker), EMA20/50, VWAP ngày UTC, RSI14, MACD, CVD (khi có taker), swing/BOS/CHoCH, trendline, phiên, mức thanh khoản, volume profile ngày UTC trước và ngày đang hình thành; phiên/mức/profile ẩn ở khung ≥ 4h kèm ghi chú.
- `static/market-chart.js`: Lightweight Charts **5.2.1** (giữ bản đang tự host; API primitive/marker/pane đủ dùng, chưa cần tải bản 5.3.0), 4 primitive tự vẽ (hộp phiên, mức thanh khoản có dấu ✕ khi bị quét, trendline, volume profile mua/bán + POC/VA), marker swing/BOS/CHoCH/"Quét", pane phụ RSI/MACD đồng bộ vùng nhìn, bật/tắt lớp nhớ trong `localStorage` (bọc try/catch), danh sách sự kiện thay hover, đổi theme sáng/tối.
- `attributionLogo: false` + chân trang ghi công TradingView và link NOTICE tự host; không script/style nội tuyến.
- **Chưa làm:** nâng 5.3.0, entry zone/SL/TP1–3 trên chart (cần SIG-3), tái dùng trên bot detail/report, cập nhật nến cuối bằng `update()` thay vì `setData()` toàn bộ, profile neo theo phiên thay vì ngày UTC.

### SIG-8 — Thẻ tín hiệu và Telegram (mở rộng TG-1, TG-2, MI-6, WB07B)

1. Thẻ tín hiệu trên Admin: nhãn **PAPER**, mã tín hiệu, setup + release, hướng, vùng vào, SL, TP1–3, R:R ròng từng TP, rủi ro % vốn, hạng/xác suất (SIG-6), checklist ✓/✗ với giá trị thật, "mất hiệu lực khi …", thời điểm hết hạn, câu cảnh báo rủi ro. Không dùng chữ "AI ASTRO"/"quantum"/"độ chính xác".
2. Tin Telegram cùng nội dung rút gọn (TG-1), cập nhật reply vào tin gốc; chống spam scalping: gộp tín hiệu trùng, cooldown theo symbol/setup, giới hạn số tin/giờ (TG-2).
3. Tường thuật bằng template từ sự kiện (MI-6), không để LLM sinh số.

**Acceptance:** tin và thẻ truy ngược được về decision/feature snapshot; filter Telegram hiện có (symbol/khung/hướng/strategy/setup) áp được cho setup mới.

- **Đã làm 15/11/2026 — TG-1 lát 1:** tin Telegram thêm khối "THẺ TÍN HIỆU (PAPER)" dựng bằng cùng `build_signal_card` với Admin, từ candidate + checklist của quyết định **đã xác minh hash**: vùng vào, SL, mục tiêu recipe và mốc 1R/2R/3R tham chiếu (R thô, chưa trừ phí), checklist ✓/✗/— có giá trị thật và ngưỡng (tối đa 8 dòng), "Mất hiệu lực khi", "Độ tin cậy: Chưa hiệu chuẩn", dòng cuối "PAPER · mô phỏng nội bộ, không phải khuyến nghị đầu tư." Thẻ không dựng được (dữ liệu cũ, thang giá sai hướng) thì chỉ gửi phần cơ bản.
- **Đã làm 15/11/2026 — TG-2 lát 1:** chống spam theo subscription (một bot → một chat) trong transaction claim, tuần tự hóa bằng advisory lock theo subscription: gộp tín hiệu trùng (cùng mã + hướng + chiến lược + release) trong cooldown (mặc định 90 phút, `TRADING_BOT_SIGNAL_WORKER_COOLDOWN_MINUTES`), trần tin/60 phút trượt (mặc định 6, `TRADING_BOT_SIGNAL_WORKER_MAX_MESSAGES_PER_HOUR`). Delivery bị chặn ghi `SUPPRESSED` + mã lý do (migration `c3e5a7b9d1f4`).
- **Chưa làm (TG-1/TG-2):** reply cập nhật vào tin gốc, R:R ròng từng TP, rủi ro % vốn; trần tin theo **chat** xuyên nhiều bot (RLS worker hiện theo bot), giờ yên lặng, hiển thị số tin bị chặn trên Admin.

### SIG-9 — Dải biến động dự kiến (tùy chọn, ưu tiên thấp)

Bootstrap lợi suất lịch sử theo chế độ để hiện "khoảng giá có thể đi trong 4 giờ" (dải phân vị), ghi rõ **không phải dự báo hướng**. Chỉ làm sau SIG-5; không dùng làm điều kiện vào lệnh.

## Thứ tự triển khai

```text
SIG-0 (taker buy + collector; chờ quyết định owner #1/#2)
  ├─ SIG-1 (feature) ── SIG-7 (chart, hiển thị/gắn nhãn trước — giá trị sớm cho người quản lý)
  └─ SIG-2 (khung ngắn + sửa card)
SIG-3 (contract v2, partial TP) ── SIG-4 (H18–H20) ── SIG-5 (kiểm định)
SIG-5 ── SIG-6 (hiệu chuẩn) ── SIG-8 (thẻ + Telegram) ── SIG-9 (tùy chọn)
```

SIG-1 + SIG-7 có thể làm trên dữ liệu seed/fixture trước khi SIG-0 có dữ liệu thật (taker buy NULL → lớp áp lực ẩn kèm ghi chú). SIG-3 độc lập với SIG-1 và làm song song được. Migration xếp tuần tự để giữ một Alembic head.

## Bảng gói và mã cũ được mở rộng

| Gói | Mở rộng | Cỡ | Rủi ro chính |
| --- | --- | --- | --- |
| SIG-0 | DATA-1, WB07C | M | Quyền dữ liệu; lỗ dữ liệu khi mất kết nối |
| SIG-1 | IND-1, IND-2, IND-4, FX-6 | L | Nhìn trước/vẽ lại; định nghĩa SMC không thống nhất |
| SIG-2 | RT-1, SC-1 | S–M | Tải tính toán khung 1m |
| SIG-3 | MI-1, MI-2, SC-1 | M | Đổi contract ảnh hưởng ledger/báo cáo |
| SIG-4 | strategies-2026 (H18–H20) | M | Chi phí ăn hết lợi thế |
| SIG-5 | BT-1 | M | Overfit, chọn tham số sau khi xem kết quả |
| SIG-6 | WP06 calibration, MI-5 | M | Ít mẫu → hiệu chuẩn không ổn định |
| SIG-7 | WB07A | M | CSP, hiệu năng mobile |
| SIG-8 | TG-1, TG-2, MI-6, WB07B | S–M | Spam, nội dung bị hiểu là khuyến nghị |
| SIG-9 | MT-1 Monte Carlo | S | Bị hiểu nhầm là dự báo |

## Quyết định cần owner

| # | Quyết định | Ảnh hưởng |
| --- | --- | --- |
| 1 | (Giữ từ 14/11) Dùng dữ liệu công khai Binance cho PAPER nội bộ; thêm: lưu taker buy và (tùy chọn) aggTrades cuộn, thời gian giữ | SIG-0, mọi gói sau |
| 2 | Symbol và khung chạy scalping đầu tiên (đề xuất BTCUSDT, ETHUSDT; M5 + M15) | SIG-0, SIG-2, SIG-4 |
| 3 | SMC: chỉ hiển thị/gắn nhãn trước (đề xuất) rồi mới thành chiến lược H18/H20 sau SIG-5 | SIG-1, SIG-4 |
| 4 | Giả định phí: VIP0 taker 0,05%/maker 0,02% hay mức khác; có tính giảm BNB không | SIG-4, SIG-5 |
| 5 | Tiêu chí bật PAPER cho một setup scalping (đề xuất: ≥ 200 lệnh ngoài mẫu, kỳ vọng dương sau phí ở kịch bản trượt giá ×2) | SIG-5 |
| 6 | Có làm SIG-9 không | SIG-9 |

### Quyết định owner đã chốt (16/11/2026, trước khi chạy bất kỳ kiểm định nào)

- **#2 Mã/khung scalping đầu tiên:** BTCUSDT, ETHUSDT; khung M5 + M15.
- **#4 Phí:** VIP0 — taker 0,05%, maker 0,02%; **không** tính giảm phí BNB.
- **#5 Tiêu chí đề xuất bật PAPER cho một setup scalping:** ≥ 200 lệnh **ngoài mẫu** (walk-forward) **và** kỳ vọng sau phí vẫn dương ở kịch bản xấu (trượt giá ×2 + chỉ khớp taker). Tiêu chí khóa trước khi chạy SIG-5; đổi tiêu chí sau khi thấy kết quả phải ghi là lượt thử mới.
- **Thứ tự:** SIG-2 → SIG-3 → SIG-4 → SIG-5 (đúng chuỗi phụ thuộc).

## Rủi ro và giới hạn

- OHLCV + taker buy không tái hiện hàng đợi limit, spread hay thứ tự trong nến; kết quả scalping trên nến 1m/5m là **cận trên lạc quan**.
- Các chỉ báo kiểu SMC/phiên chưa có bằng chứng độc lập về lợi thế sau phí; giai đoạn đầu chỉ để mô tả và gắn nhãn.
- Lightweight Charts không tự cung cấp dữ liệu hay semantics; Crypidian chịu trách nhiệm về mọi số trên chart và phải giữ attribution TradingView.
- Không gói nào trong WBS này cho phép gửi lệnh Binance hoặc bật SHADOW/PAPER_GATE/Demo/LIVE.
