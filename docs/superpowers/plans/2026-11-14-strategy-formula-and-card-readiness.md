# Kế hoạch: độ đúng công thức, chiến lược và Strategy Card

**Ngày lập:** 2026-11-14 (Asia/Ho_Chi_Minh) · **Nhánh nền:** `dev@e1d845c` + PR `bmamba-01/trading-crypyidian#1`
**Trạng thái:** đề xuất, chờ owner duyệt thứ tự và các quyết định ở mục 6. Chưa có dòng code nào của kế hoạch này được viết.

## 1. Mục tiêu và phạm vi

Owner yêu cầu ba việc:

1. Sửa và cải thiện năm giới hạn còn lại sau khi Strategy Card vào catalog sản phẩm (mục 2).
2. Đánh giá chiến lược và công thức từ góc nhìn trader chuyên nghiệp, vì các công thức còn thiếu sót.
3. Mọi chỉ số trên biểu đồ và trang giao dịch có tooltip tiếng Việt để Admin dễ hiểu.

Nguyên tắc giữ nguyên trong suốt kế hoạch:

- Chỉ PAPER/replay. Không bật `SHADOW`, `PAPER_GATE`, Demo hay LIVE; không gọi API giao dịch của sàn.
- Không tuyên bố khả năng sinh lời. Một chiến lược chỉ "đủ điều kiện PAPER" khi qua tiêu chí bằng chứng ở mục 5.
- Mỗi gói làm theo RED → GREEN → REFACTOR, có unit test và E2E qua ranh giới bị đổi, cập nhật `implementation-memory.md`.
- Mọi con số phụ thuộc quy tắc Binance USDⓈ-M (chu kỳ và công thức funding, bậc maintenance margin, phí maker/taker, filter của lệnh, kiểu giá kích hoạt stop) phải được kiểm tra với tài liệu chính thức **tại thời điểm triển khai** và ghi URL + ngày vào memory. Kế hoạch này không khẳng định con số nào; vòng lập kế hoạch chưa tra tài liệu Binance.

## 2. Năm giới hạn cần đóng

| # | Giới hạn | Gói xử lý |
| --- | --- | --- |
| 1 | Runtime thật giữ lease và khôi phục vị thế nhưng không tự mở/đóng lệnh (`market_dispatch: not_configured`). | RT-1 |
| 2 | Chưa có luồng admin tạo hàng `strategy_release` trong workspace; kể cả H0x cũng chỉ do seed tạo. | SC-3 |
| 3 | Proof WP06 cho release của thẻ bị từ chối vì `candidate_universe_proofs.py` (tầng adapter) dùng registry trần. | SC-4 |
| 4 | Thẻ demo: tên không khớp logic; EMA và z-score của engine là placeholder. | SC-1, SC-2 |
| 5 | Workspace gieo bằng bản seed cũ ghim release/hash mà runtime thật không nhận diện. | OPS-1 |

## 3. Kết quả rà từ góc nhìn trader

Rà đọc code ngày 2026-11-14 (không chạy Docker/E2E). **[F]** là sự thật đọc từ code, **[J]** là nhận định nghề nghiệp. Đường dẫn tương đối theo `src/trading_bot/`.

### 3.1 Ba kết luận nặng nhất

1. **Exit khai báo nhưng không chạy.** [F] `exit_channel_period=20` của H03 (`domain/strategy/rules.py:367`) và `exit_feature_name="zscore_20"` của H06 (`rules.py:573`) không có nơi tiêu thụ nào ngoài `recipes.py`. H03 không thoát theo kênh, H06 không thoát khi z-score hồi về. Release mô tả một chiến lược khác với thứ đang chạy.
2. **Funding không bao giờ được ghi trong PAPER.** [F] `apply_funding_event` (`domain/execution/paper.py:461`) không có nơi gọi; `funding_cost` luôn bằng 0. Lệnh giữ dài (H01/H03 tới 120 nến) được định giá lạc quan.
3. **Chi phí nuốt gần hết lợi thế thiết kế.** [F] Hồ sơ PAPER mặc định (phí 0,10% mỗi chiều, spread + trượt giá 0,15% mỗi chiều) cho chi phí khứ hồi khoảng 0,5% giá. [J, tỷ lệ ATR/giá là giả định minh họa] Với H06 trên 15 phút, ATR/giá 0,2% thì chạm chốt lời vẫn lỗ; ATR/giá 0,4% thì cần tỷ lệ thắng khoảng 79% để hòa vốn. Thẻ VOL-BREAKOUT với ATR/giá 0,5% có R:R ròng khoảng 1,0 thay vì 2,0.

### 3.2 Nhận định theo chiến lược

| Chiến lược | Có phải giả thuyết giao dịch được như đang code? | Bằng chứng cần trước khi nhận vốn PAPER | Ba cải tiến giá trị nhất |
| --- | --- | --- | --- |
| H01 EMA trend | Mạch lạc, kiểm thử được. Tín hiệu rất thưa vì chỉ vào đúng nến giao cắt kèm ADX ≥ 20. | ≥ 100 lệnh ngoài mẫu trên nhiều symbol/năm; expectancy sau chi phí > 0 với khoảng tin cậy không chứa 0; độ nhạy ngưỡng ADX/EMA phẳng. | Cửa sổ vào N nến sau giao cắt; warm-up 3×period cho EMA200 4h; funding thật. |
| H03 Donchian breakout | Kinh điển, tốt nhất trong ba; nhưng exit kênh 20 không chạy. | Như H01, tách theo regime. | Hiện thực exit kênh; biên phá vỡ theo ATR và volume ≥ 1,5× median; lọc regime. |
| H06 mean reversion | Hợp lý về ý tưởng; gần như không thể có lãi trên 15 phút với chi phí mặc định; exit z-score không chạy; không backtest được vì replay thiếu macro. | Chưa nên chạy cho tới khi có cổng R:R ròng và replay có macro. | Cổng "TP ròng > 0, R:R ròng ≥ 1"; TP = trung bình + exit z-score; macro lịch sử cho replay, cân nhắc khung 1h. |
| Thẻ VOL-BREAKOUT-001 | Không. [F] Đồ thị chỉ là "râu dưới ≥ 50% biên độ nến thì LONG, stop 2 ATR, mục tiêu 4 ATR, 12 nến"; không có Donchian, trend hay breakout. | Không cấp vốn PAPER cho tới khi tên và logic khớp nhau. | Thêm node Donchian/trend thật hoặc đổi tên; chuẩn hóa nến theo ATR và điều kiện vị trí; guard dữ liệu như H0x. |
| Thẻ MEAN-REVERT-002 | Không chạy được: ngòi nổ không được nhận diện, EMA = giá đóng, z-score giả, ATR một nến. | — | Giữ ngoài catalog cho tới khi SC-1 xong. |

### 3.3 Phát hiện chi tiết theo nhóm

**Chỉ báo**

- [F] Warm-up đạt khi số nến ≥ `period` (`domain/market/feature_snapshot.py:279-302`); EMA seed bằng SMA. Ở đúng 200 nến, EMA200 chính là SMA200 và trọng số seed vẫn còn khoảng 37% ở 300 nến. [J] Bộ lọc 4h của H01 phụ thuộc độ dài lịch sử được nạp, nên replay và runtime có thể lệch nhau.
- [F] ATR seed gồm TR[0] = H − L (`domain/market/features.py:76-77`), lệch nhẹ so với Wilder giai đoạn đầu; ADX không bị ảnh hưởng.
- [F] `zscore` chuẩn hóa theo 20 mẫu trước, không gồm nến hiện tại (`features.py:262-280`): khác z kiểu Bollinger, ngưỡng ±2 chạm thường xuyên hơn.
- [F] Regime đã được tính (`feature_snapshot.py:409-489`) nhưng không rule nào đọc.
- [F] `wick_rejection_strength` chỉ có bản tăng giá; tỷ lệ râu theo biên độ nến nên doji 2 tick vẫn có râu 0,9 (`domain/strategy_card/anatomy.py:63-74`).
- Đạt: [F] H01/H03/H06 chỉ dùng nến đã đóng và `available_at ≤ decision_time`; không thấy look-ahead.

**Sizing và rủi ro**

- Đạt: [F] công thức khối lượng theo rủi ro mỗi lệnh có tính phí, funding và trượt giá, làm tròn xuống theo step (`domain/risk/sizing.py:129-165`).
- [F] `adverse_funding_per_unit=0.05` là số tuyệt đối theo quote mỗi đơn vị (`application/paper_execution_profile.py:114`): gần 0 với BTC, khổng lồ với coin giá nhỏ.
- [F] Chỉ có initial margin; không có maintenance margin hay giá thanh lý trong `src`. [J] Với đòn bẩy cao, vị thế có thể bị thanh lý trước khi chạm stop.
- [F] Stop/TP neo vào giá đóng nến tín hiệu, khối lượng tính theo giá vào kế hoạch; fill ở open nến sau không tính lại. R:R gộp thực tế thấp hơn recipe (đã quan sát 1,79 so với 2,0).
- [F] Không có khoảng cách stop tối thiểu so với chi phí khứ hồi.
- [F] Trần rủi ro theo symbol, không gom theo tương quan (BTC/ETH/SOL cùng hướng gần như một vị thế).
- [F] Trong replay, `realized_loss_today` và `realized_loss_rolling` là tổng lỗ cộng dồn cả lượt chạy, không reset, không bù lệnh thắng (`application/runtime_replay_evaluator.py:213-227`). Backtest dài sẽ chạm trần lỗ ngày vĩnh viễn và âm thầm ngừng vào lệnh.
- [F] `calculate_loss_windows` và `CircuitBreakerEngine` chưa nối vào `RiskState`; endpoint health nạp `events=[]`.

**PAPER execution**

- Bảo thủ: fill ở open nến kế có chi phí bất lợi; nến chạm cả stop lẫn TP thì chọn stop; gap qua stop khớp tại open.
- Bi quan: TP chịu cùng spread/trượt giá/phí như lệnh thị trường; chưa tách maker/taker.
- Lạc quan: time stop và tín hiệu ngược khớp tại `bar.close` của chính nến ra quyết định; khớp đủ khối lượng không giới hạn theo volume nến; không mô phỏng thanh lý.
- [F] `bars_held` tăng theo mỗi nến monitor trong khi replay cho phép interval 1m–1d: `max_hold_bars=120` có thể là 120 phút thay vì 120 giờ.

**Chỉ số và hậu kiểm**

- [F] Drawdown tính trên PnL đã đóng, theo tiền tuyệt đối, không mark-to-market, không theo % vốn (`domain/portfolio/reporting.py:808-826`).
- [F] Bội số R hiện chia cho `risk_budget`, không phải rủi ro ban đầu thực (`estimated_loss`): khi size bị trần notional/margin, −1R không còn nghĩa là chạm stop.
- [F] `FALSE_BREAKOUT` = cắt lỗ dưới 900 giây cho mọi chiến lược; với nến 1h gần như không bao giờ đúng.
- [F] Mọi lệnh lãi là `OPTIMAL` và được `REINFORCE`: chấm theo kết quả, không theo quy trình.
- [F] Ba ranh giới ngày khác nhau: báo cáo theo giờ Việt Nam, loss window mặc định UTC, admin theo giờ Việt Nam.

**Replay/backtest**

- Đạt: chống rò rỉ tốt (sắp theo `available_at`, bắt look-ahead, dùng chung evaluator với runtime).
- [F] Chi phí replay kém thực tế hơn PAPER (spread 0, funding 0, đòn bẩy 1x). Bootstrap chạy từ n ≥ 2, `trial_count=1` viết cứng, walk-forward chỉ có cho nhánh learning.

**Strategy Card pipeline**

- [F] Node L2 không phải ATR trả `bar.close`; L3 "zscore" là trung bình mọi giá trị đã có; cổng `min_atr=0.005` so với ATR theo đơn vị giá; cổng trend luôn qua; ngưỡng mặc định 0,5 và hướng mặc định LONG khi thẻ không khai; nhánh "crossover" luôn kích hoạt; L6 đọc cứng khóa `l2_atr`.
- [F] Engine dùng `snapshot.series[0]` bất kể timeframe của thẻ, không có guard nến đóng/stale/gap/`position_is_flat` như H0x; `max_hold_bars=12` và hiệu lực 15 phút viết cứng trong bridge.
- [F] Release của thẻ không có trong `FEATURES_BY_RELEASE` và `ENTRY_TIMEFRAME_BY_RELEASE` (`application/strategy_market_requirements.py:20-45`), nên builder snapshot thật và replay từ chối release này. Hiện chỉ bộ seed cấp snapshot cho thẻ.

## 4. Các gói công việc

Cỡ: S ≤ 1 ngày, M 2–4 ngày, L ≥ 1 tuần (ước lượng thô, chưa có estimate được duyệt).

### Giai đoạn P0 — đúng trước, rồi mới đo

**FX-0 · Thuật ngữ và nguồn (S).** Thêm vào `docs/research/glossary-vi.md` các thuật ngữ kế hoạch dùng mà glossary chưa có, mỗi mục có EN→VI, định nghĩa, giới hạn và nguồn: MFE, MAE, Chandelier exit, time stop, tick size, notional, break-even win rate, cost drag, implementation shortfall, equity curve, bid-ask spread, relative volume, ATR%/NATR, volatility targeting, hammer/pin bar, doji, risk of ruin, Kelly fraction; rà lại "correlation" và "Wilder smoothing". Lập danh sách tài liệu Binance cần tra cho FX-3, FX-6, FX-7. *Nghiệm thu:* không thuật ngữ nào trong các gói sau thiếu mục glossary.

**FX-1 · Exit chạy đúng như release khai báo (M).** Hiện thực thoát theo kênh Donchian cho H03 và thoát theo feature (z-score về ngưỡng) cho H06 trong position monitor và nguồn evidence của monitor; hoặc gỡ khỏi recipe nếu owner chọn không dùng. Đổi recipe là đổi hash, nên phát hành `h03-v2`/`h06-v2`, không sửa tại chỗ `v1`. *Nghiệm thu:* test cho từng lý do thoát; E2E PAPER có lệnh đóng bằng exit mới; lệnh đang mở ghim `v1` vẫn khôi phục được.

**FX-2 · Cửa sổ lỗ và ngắt mạch dùng chung một định nghĩa (M).** Replay dùng `calculate_loss_windows` thay cho tổng cộng dồn; runtime nạp `RiskState.realized_loss_*` từ ledger qua cùng hàm; chốt một múi giờ/giờ reset cho báo cáo, loss window và admin; nối `CircuitBreakerEngine` vào cổng entry với ledger bền (tiếp OPS07B). Điều kiện chặn tính cả rủi ro đang mở và rủi ro lệnh mới. *Nghiệm thu:* backtest nhiều ngày không còn tự khóa vĩnh viễn; test ranh giới ngày; E2E breaker chặn entry nhưng vẫn cho thoát bảo vệ. *Cần owner:* múi giờ, giờ reset, ngưỡng.

**FX-3 · Funding (M).** Ghi funding theo lịch vào ledger PAPER và replay; đổi `adverse_funding_per_unit` thành tỷ lệ × notional × số kỳ dự kiến trong thời gian giữ. *Nguồn phải kiểm:* chu kỳ và công thức funding theo symbol. *Nghiệm thu:* lệnh giữ qua mốc funding có `trade_cost` loại FUNDING; khối lượng không còn bị bóp vô lý với coin giá nhỏ.

**FX-4 · Đơn vị thời gian giữ lệnh (S).** `max_hold_bars` và ATR của trailing gắn với timeframe chiến lược, không với interval của nến monitor; time stop và thoát theo tín hiệu ngược khớp ở open nến sau. *Nghiệm thu:* cùng một lệnh cho cùng thời điểm time stop khi monitor chạy nến 1m hay 1h.

**FX-5 · Kế hoạch lệnh trung thực về R (M).** Thêm cổng trong planner: từ chối khi TP ròng ≤ 0 hoặc R:R ròng dưới ngưỡng cấu hình; neo lại stop/TP theo giá fill thực hoặc lưu 1R thực tại fill; bội số R = PnL ròng / rủi ro ban đầu thực. *Nghiệm thu:* H06 trên dữ liệu ATR thấp bị từ chối với mã lý do rõ; hậu kiểm hiển thị R đúng khi size bị trần. *Cần owner:* ngưỡng R:R ròng tối thiểu.

### Giai đoạn P1 — chỉ báo, ký quỹ và backtest đáng tin

**FX-6 · Chỉ báo (S–M).** Warm-up ≥ 3×period cho EMA/RMA và ghi vào `formula_hash`; seed ATR theo Wilder; ghi rõ định nghĩa z-score và thêm biến thể gồm nến hiện tại để so sánh; thêm bản bearish cho các tỷ lệ râu nến và chuẩn hóa biên độ nến theo ATR. Đổi công thức là đổi `formula_hash`, snapshot cũ giữ nguyên. *Nghiệm thu:* bộ dữ liệu vàng đối chiếu với cài đặt tham chiếu độc lập.

**FX-7 · Maintenance margin và giá thanh lý (M).** Tính giá thanh lý theo bậc maintenance margin; từ chối kế hoạch khi stop không nằm trước giá thanh lý với biên an toàn; mô phỏng thanh lý trong PAPER. *Nguồn phải kiểm:* bậc MMR, công thức giá thanh lý, kiểu giá kích hoạt stop (mark hay last). *Cần owner:* biên an toàn.

**BT-1 · Backtest thực tế (M).** Replay dùng cùng hồ sơ chi phí với PAPER (spread, funding, đòn bẩy); ngưỡng số lệnh tối thiểu trước khi báo khoảng tin cậy; ghi số lượt thử để chiết khấu; walk-forward cho rule H0x; macro lịch sử để H06 backtest được. *Nghiệm thu:* báo cáo backtest ghi rõ "chưa đủ mẫu" thay vì đưa số.

**FX-8 · Cải tiến rule (M mỗi rule, sau BT-1).** H01: cửa sổ vào sau giao cắt, khoảng cách EMA tối thiểu, cooldown sau stop. H03: biên phá vỡ theo ATR, volume theo bội số median, lọc regime. H06: TP = trung bình, xác nhận đảo chiều, cân nhắc khung 1h. Mỗi thay đổi là một release mới và phải qua tiêu chí mục 5 trước khi thay bản cũ. *Cần owner:* duyệt từng giả thuyết trước khi code.

**RISK-1 · Rủi ro theo nhóm tương quan (M).** Nhóm symbol theo tương quan/beta BTC và trần rủi ro theo nhóm. *Cần owner:* cách định nghĩa nhóm.

### Giai đoạn P2 — Strategy Card thành công cụ thật

**SC-1 · Engine tính đúng thứ thẻ khai báo (L).** Bảng đăng ký operation tường minh thay cho dò chuỗi con; EMA và các chỉ báo lấy từ feature đã quản trị; z-score thật đọc đầu vào theo cạnh đồ thị; ATR cho L6 tra theo cạnh, thiếu thì `NoTrade`; cổng biến động theo ATR/giá hoặc phân vị; ngưỡng và hướng bắt buộc khai; hiện thực crossover; `max_hold_bars` và hiệu lực candidate lấy từ thẻ; dùng timeframe của thẻ và các guard dữ liệu của H0x; khai `FEATURES_BY_RELEASE`/`ENTRY_TIMEFRAME_BY_RELEASE` cho release của thẻ. Tăng `PIPELINE_ENGINE_VERSION`. *Nghiệm thu:* mỗi operation có test tham chiếu; thẻ dùng operation chưa đăng ký bị từ chối khi kiểm định, không âm thầm bỏ qua.

**SC-2 · Thẻ mẫu trung thực (M, sau SC-1).** VOL-BREAKOUT-001: hoặc thêm node Donchian/trend để đúng tên, hoặc đổi tên thành thẻ từ chối râu dưới; phát hành phiên bản mới, không sửa `1.0.0`. MEAN-REVERT-002: EMA50, z-score, ngòi nổ đảo chiều râu nến có bản gương, vùng giá với TP = trung bình. Thẻ chỉ vào `PRODUCT_CARD_IDS` khi qua tiêu chí mục 5. *Cần owner:* chọn hướng cho VOL-BREAKOUT.

**SC-3 · Công bố release trong workspace từ admin (M).** Trang quản trị liệt kê release của catalog, cho owner công bố/thu hồi hàng `strategy_release` (MFA, lý do, audit, hash lấy từ catalog), và cho cấu hình bot chọn release đã công bố. Đóng giới hạn 2. *Nghiệm thu:* Playwright + Compose E2E; hash lệch catalog bị từ chối.

**SC-4 · Một nguồn registry cho mọi tầng (S–M).** Tầng adapter nhận registry qua tham số/port thay vì tự dựng, để proof WP06 thấy release của thẻ mà không import ngược tầng. Đóng giới hạn 3. *Nghiệm thu:* E2E proof với release của thẻ; test cấm `StrategyRegistry()` trần trong `src` ngoài catalog.

### Giai đoạn P3 — runtime tự chạy và đo lường

**RT-1 · Nguồn nến và dispatcher cho runtime PAPER (L).** Collector nến đã đóng → feature snapshot → evidence; `DecisionSnapshotProvider` thật thay cho provider tổng hợp của probe; cấp nến cho position monitor để tự khớp và tự đóng lệnh; `/health/ready` báo `market_dispatch` theo trạng thái thật. Đóng giới hạn 1. *Phụ thuộc:* quyền nguồn dữ liệu (WP05/WP06), FX-1..FX-5. *Cần owner:* nguồn dữ liệu được phép dùng cho PAPER nội bộ.

**MT-1 · Chỉ số chuyên nghiệp (M–L).** Đường vốn mark-to-market theo nến và drawdown %; phân phối bội số R và chuỗi thua theo R; MFE/MAE; Sharpe/Sortino trên lợi suất ngày, MAR/Calmar; thời gian có vị thế; chi phí so với lãi gộp; trượt giá so với kế hoạch; tách theo chiến lược, hướng, lý do thoát. Hậu kiểm chấm theo quy trình (MFE/MAE, hiệu suất exit) thay vì theo kết quả; `FALSE_BREAKOUT` định nghĩa theo số nến của timeframe chiến lược. *Nghiệm thu:* mỗi chỉ số có công thức trong glossary và test với ledger tất định; thiếu dữ liệu thì hiện "chưa đủ dữ liệu".

**RR-1..RR-3 · Phân tích đợt chạy và tổng kết cho AI research engine.** Chi tiết ở [`2026-11-14-feature-survey-and-manager-insights.md` §3.5](2026-11-14-feature-survey-and-manager-insights.md): báo cáo đợt chạy tất định → research brief có cấu trúc → vòng đề xuất có người duyệt. Phụ thuộc RT-1, MT-1, BT-1.

**OPS-1 · Dữ liệu kiểm thử sạch (S).** Bộ seed thêm lệnh kiểm tra workspace có release/hash không còn trong catalog và báo rõ; hướng dẫn làm lại stack bằng cách xóa volume; E2E tự động cho bộ seed. Đóng giới hạn 5.

### Giai đoạn song song — giao diện

**UI-1 · Tooltip tiếng Việt cho mọi chỉ số (M).** Hiện trạng [F]: chỉ trang báo cáo (14 chỗ) và bot detail (5 chỗ) có `tooltip-term`; biểu đồ, 15 chỉ số trong `metric_cards.html` và KPI dashboard chưa có; tooltip là `::after` khi hover/focus trên `<span>` không nhận focus, nên không dùng được bằng bàn phím hay màn hình chạm.

Việc cần làm:

1. Một từ điển chú giải duy nhất phía máy chủ (khóa chỉ số → tên, công thức, cách đọc, giới hạn, liên kết glossary), dùng chung cho template, CSV và biểu đồ; test bắt buộc mọi khóa trong `METRICS` và mọi nhãn chỉ số có chú giải.
2. Thành phần tooltip truy cập được: nút có `aria-describedby`, mở bằng hover, focus và chạm, đóng bằng Escape, không tràn ở 360px, tuân thủ CSP (không script/style nội tuyến).
3. Biểu đồ nến: chú giải khi rê chuột hiển thị Mở/Cao/Thấp/Đóng, khối lượng, % thay đổi bằng tiếng Việt; chú thích cho mũi tên vào/đóng, đường stop, đường mục tiêu, đường giá vào; chú thích khung thời gian và nguồn nến; nhãn trục và định dạng số tiếng Việt.
4. Biểu đồ hiệu suất dashboard: chú giải đường vốn, mốc vào/đóng, cách tính PnL ròng.
5. Phủ các trang: dashboard, bot detail, báo cáo (thẻ chỉ số, lịch lãi/lỗ, panel hậu kiểm), backtest, Strategy Card/microscope (giải thích từng tầng L0–L6), hiệu năng PAPER.
6. Playwright trên desktop và mobile: mở tooltip bằng bàn phím và chạm, kiểm nội dung, kiểm không vi phạm CSP.

*Nghiệm thu:* không còn chỉ số nào hiển thị mà thiếu chú giải (có test kiểm kê); checklist trợ năng cập nhật. *Phụ thuộc:* nội dung chú giải của các chỉ số mới chờ MT-1; phần còn lại làm ngay được.

## 5. Tiêu chí để một chiến lược hoặc thẻ đủ điều kiện PAPER

Đề xuất, cần owner duyệt:

1. Release tính đúng mọi thứ nó khai báo (không placeholder), có test tham chiếu.
2. Backtest ngoài mẫu với cùng mô hình chi phí của PAPER, có funding, đủ số lệnh tối thiểu, có khoảng tin cậy và ghi số lượt thử.
3. Expectancy sau chi phí > 0 với khoảng tin cậy không chứa 0; kết quả không sụp khi đổi tham số chính ±20%.
4. Cổng R:R ròng của planner không từ chối phần lớn tín hiệu.
5. Hậu kiểm theo quy trình (MFE/MAE) không cho thấy lãi chỉ đến từ vài lệnh ngoại lệ.

Chưa đạt thì release ở trạng thái `EXPERIMENTAL_UNVALIDATED` và không được chọn làm mặc định.

## 6. Quyết định cần owner

| # | Quyết định | Ảnh hưởng tới |
| --- | --- | --- |
| 1 | Duyệt thứ tự P0 → P3 và việc làm UI-1 song song. | Toàn bộ |
| 2 | H03/H06: hiện thực exit đã khai báo hay gỡ khỏi recipe. | FX-1 |
| 3 | Múi giờ, giờ reset, ngưỡng lỗ ngày/trượt và ngưỡng breaker. | FX-2 |
| 4 | Ngưỡng R:R ròng tối thiểu; biên an toàn stop so với thanh lý. | FX-5, FX-7 |
| 5 | VOL-BREAKOUT-001: sửa đồ thị cho đúng tên hay đổi tên theo logic thật. | SC-2 |
| 6 | Tiêu chí mục 5 và ngưỡng số lệnh tối thiểu. | BT-1, FX-8, SC-2 |
| 7 | Nguồn dữ liệu được phép cho runtime PAPER tự chạy. | RT-1 |
| 8 | Cách nhóm symbol theo tương quan. | RISK-1 |

## 7. Thứ tự và phụ thuộc

```text
FX-0 ─┬─ FX-1 ─┐
      ├─ FX-2 ─┤
      ├─ FX-3 ─┼─ FX-5 ─┬─ BT-1 ── FX-8
      └─ FX-4 ─┘        ├─ FX-7
FX-6 ───────────────────┘
SC-1 ── SC-2          SC-3, SC-4 (độc lập, sau PR #1)
FX-1..FX-5 + quyền dữ liệu ── RT-1
FX-5 ── MT-1          UI-1 (song song; phần chỉ số mới chờ MT-1)
OPS-1 (bất kỳ lúc nào)
```

Lý do thứ tự: sửa chỗ sai làm méo mọi phép đo (exit, cửa sổ lỗ, funding, đơn vị thời gian, R) trước khi backtest hay cải tiến rule; nếu không, mọi kết quả đo về sau đều phải làm lại.

## 8. Ngoài phạm vi

Kết nối Binance Demo/LIVE (WP-03), quyền dữ liệu non-QA và scorer WP06, backup/restore và security review (OPS07C–E). Các mục này giữ nguyên ở kế hoạch riêng.
