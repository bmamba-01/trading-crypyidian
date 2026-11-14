# Khảo sát tính năng còn thiếu và đề xuất "dễ hiểu cho người quản lý"

**Ngày lập:** 2026-11-14 (Asia/Ho_Chi_Minh) · **Nhánh:** `claude/project-progress-review-wp07-d70431` (PR `bmamba-01/trading-crypyidian#1`, HEAD `45ce6a5`)
**Trạng thái:** đề xuất, chờ owner chọn thứ tự. Chưa có dòng code nào của các gói mới (MI-*, DATA-*, TG-*) được viết.
**Đầu vào:** kế hoạch [`2026-11-14-strategy-formula-and-card-readiness.md`](2026-11-14-strategy-formula-and-card-readiness.md), [`project-status.md`](../../implementation/project-status.md), memory triển khai, mã nguồn hiện tại và một lượt nghiên cứu nguồn ngoài (mọi URL truy cập 2026-11-14).

## 1. Tóm tắt cho người quản lý

1. **Bot chưa tự chạy trên thị trường thật.** Mọi lệnh PAPER và tín hiệu hiện tại đến từ dữ liệu seed hoặc replay. Chưa có worker nào đọc nến Binance liên tục; `ingestion-worker` chỉ bảo trì quyền dữ liệu. Đây là nút thắt lớn nhất, chặn cả "tín hiệu Telegram thật" (gói RT-1 + DATA-1 bên dưới).
2. **Công thức cốt lõi đã đúng hơn sau P0** (exit H03/H06 v2, cổng R:R ròng 1,0, lỗ ngày theo UTC, công thức funding), nhưng các chiến lược vẫn ở trạng thái `EXPERIMENTAL_UNVALIDATED`: chưa có backtest ngoài mẫu đủ số lệnh với cùng chi phí như PAPER.
3. **Thứ giúp người quản lý nhiều nhất không cần toán mới:** một checklist bằng câu thường cho mỗi quyết định ("điều kiện nào đạt, điều kiện nào còn thiếu bao nhiêu"), đèn giao thông "thị trường hôm nay có hợp với chiến lược không", và cảnh báo khi thị trường khác hẳn giai đoạn đã kiểm thử. Ba thứ này làm được từ dữ liệu sẵn có.
4. **SMC (Smart Money Concepts)** đáng dùng như *ngôn ngữ mô tả* cấu trúc giá (quét thanh khoản, phá cấu trúc, khoảng trống giá), chưa có bằng chứng độc lập là nguồn lợi nhuận. Phải tự viết bản không nhìn trước tương lai.
5. **"Quantum trading":** chưa có lợi thế lượng tử nào được chứng minh trong giao dịch; sản phẩm bán lẻ mang nhãn "Quantum AI" bị FCA, ASIC, CFTC, IOSCO cảnh báo là lừa đảo. Nên lấy *ý tưởng trình bày* (nhiều kịch bản cùng tồn tại với xác suất) bằng mô hình cổ điển, và **không dùng chữ "quantum"** trong sản phẩm.

## 2. Tính năng chưa phát triển (kiểm kê tới HEAD `45ce6a5`)

Ký hiệu: **[K]** đã kiểm trong mã nguồn ở vòng này; **[T]** lấy từ tài liệu dự án, chưa kiểm lại trong mã.

### 2.1 Chặn việc chạy thật

| # | Thiếu gì | Hệ quả | Gói |
| --- | --- | --- | --- |
| 1 | Worker nến trực tiếp từ Binance (REST/WebSocket công khai) ghi `market_bar` [K] | Không có tín hiệu thật, runtime không tự khớp/đóng lệnh | DATA-1, RT-1 |
| 2 | Dispatcher thị trường cho runtime PAPER: snapshot → quyết định → monitor (`market_dispatch: not_configured`) [K] | Lệnh mở chỉ đóng khi có probe đẩy nến | RT-1 |
| 3 | Bên cấp bằng chứng thoát lệnh cho runtime (kênh Donchian, z-score) [K] | `h03-v2`/`h06-v2` mới chạy đúng trong replay | RT-1 |
| 4 | Runtime nạp lỗ đã chốt từ ledger; circuit breaker chưa nối vào cổng vào lệnh; health truyền `events=[]` [K] | Giới hạn lỗ ngày chưa chặn được lệnh thật | FX-2 phần còn lại |
| 5 | Lịch tự ghi funding vào ledger/replay; hồ sơ PAPER vẫn dùng funding tuyệt đối 0,05/đơn vị [K] | PnL lệnh giữ qua mốc funding chưa trừ funding thật | FX-3 phần còn lại |
| 6 | Luồng admin công bố `strategy_release` (kể cả H0x v2 và Strategy Card) [K] | Release mới chỉ dùng được qua seed | SC-3 |
| 7 | Quyết định quyền dữ liệu cho PAPER nội bộ (WP05/WP06) [T] | Chưa thể bật DATA-1 đúng chính sách | Owner |

### 2.2 Độ đúng và đo lường (kế hoạch cũ, chưa làm)

| Gói | Nội dung còn mở |
| --- | --- |
| FX-4 phần còn lại | Time stop / thoát theo tín hiệu ngược khớp ở giá mở nến sau (cần trạng thái pending-exit bền) |
| FX-6 | Warm-up EMA/RMA ≥ 3×period, seed ATR theo Wilder, định nghĩa z-score, bản bearish cho tỷ lệ râu nến |
| FX-7 | Maintenance margin, giá thanh lý, biên an toàn stop so với thanh lý |
| BT-1 | Backtest cùng chi phí PAPER, ngưỡng số lệnh tối thiểu, walk-forward, ghi số lượt thử |
| FX-8 | Cải tiến rule H01/H03/H06 (sau BT-1) |
| RISK-1 | Trần rủi ro theo nhóm tương quan |
| SC-1, SC-2, SC-4 | Engine Strategy Card tính đúng thứ thẻ khai báo; thẻ mẫu trung thực; một nguồn registry |
| MT-1 | Đường vốn mark-to-market, drawdown %, phân phối R, MFE/MAE, Sharpe/Sortino/Calmar, chi phí so với lãi gộp |
| OPS-1 | Seed kiểm thử sạch và E2E tự động cho seed |

### 2.3 Sản phẩm và vận hành [T trừ khi ghi khác]

| Mảng | Còn thiếu |
| --- | --- |
| Binance Demo/LIVE (WP-03) | HTTP transport ký, handshake tài khoản Demo, reconcile lúc khởi động. Ngoài phạm vi cho tới khi owner mở |
| Người dùng (US-04) | Mời/quản lý thành viên, khôi phục tài khoản, UI phân quyền |
| Cấu hình (US-06, US-08) | Quy trình nháp → duyệt → công bố cấu hình qua UI; watchlist/điều kiện symbol |
| Nghiên cứu (WP-05) | Connector lấy tin/macro thật, điểm chú ý (trending), ý kiến trader |
| AI (WP-06) | Mô hình thật chấm điểm runtime, theo dõi drift, quy trình promote/rollback |
| Telegram | Tin tín hiệu đã có nhưng thiếu nội dung "vì sao / khi nào mất hiệu lực", reply theo luồng, tóm tắt ngày, giờ yên lặng [K: mã gửi hiện có]; chưa có webhook HTTPS công khai — local dùng `scripts/telegram_local_relay.py` |
| Tooltip (UI-1 còn mở) | Vùng tổng do `dashboard-live.js` dựng; kiểm trình duyệt cho biểu đồ dashboard [K] |
| Vận hành (OPS07C–E) | Backup/restore, security review, runbook sự cố |
| Tài liệu | Bảng User Story trong `project-status.md` đã cũ (ví dụ ghi US-05/US-21 Telegram "chưa bắt đầu" trong khi đã có link/revoke và tín hiệu) [K] |

## 3. Đề xuất mới

Mỗi gói: giá trị cho người quản lý / công sức (S/M/L) / rủi ro chính. Thứ tự đề xuất ở mục 6.

### 3.1 Diễn giải quyết định (MI — manager insight)

**MI-1 · Checklist quyết định bằng câu thường (S–M, giá trị rất cao).** Mỗi candidate và mỗi `NoTrade` hiện thành danh sách dòng ✓/✗/— theo tầng L0–L6 (hoặc theo điều kiện của H0x): giá trị thực, ngưỡng, khoảng cách tới ngưỡng. Ví dụ: "ADX14 = 23,4 · cần ≥ 20 · ✓"; "Giá đóng 1h 68.120 · cần > 68.450 (đỉnh 55 nến) · còn +0,48% · ✗". Dữ liệu đã có trong `gate_details`, `evidence_feature_ids` và `detail_vi`; cần chuẩn hóa thành cấu trúc (`condition_key`, `actual`, `threshold`, `operator`, `passed`). Dùng chung cho bot detail, Microscope, báo cáo và tin Telegram. *Rủi ro:* thấp.

**MI-2 · "Còn thiếu gì để vào lệnh" (counterfactual) (S, rất cao).** Với luật ngưỡng, khoảng cách tới điều kiện là phép tính chính xác, không cần ML: hiển thị điều kiện gần đạt nhất và mức giá/giá trị cần có. Trả lời câu hỏi người quản lý hay hỏi nhất: "vì sao bot không vào lệnh?". *Nguồn ý tưởng:* Wachter và cộng sự 2017, https://arxiv.org/abs/1711.00399 (chưa mở lại bản gốc trong vòng này).

**MI-3 · Đèn chế độ thị trường theo từng chiến lược (S–M, rất cao).** Xanh/vàng/đỏ cho biết điều kiện bối cảnh của từng chiến lược đang thỏa bao nhiêu (H01: xu hướng ADX ≥ 20 và giá cùng phía EMA200 4h; H06: ADX < 18 và ER40 < 0,25), kèm "đã ở trạng thái này bao lâu". Không phải dự báo, chỉ là đọc điều kiện đã khai báo. *Rủi ro:* thấp.

**MI-4 · Cảnh báo "ngoài vùng đã kiểm thử" (OOD) (M, cao).** So biến động (ATR%), ADX, funding hiện tại với phân phối trong giai đoạn backtest/PAPER của chiến lược theo phân vị; ví dụ "Biến động 30 ngày ở phân vị 99 so với dữ liệu kiểm thử — kết quả kiểm thử có thể không còn đại diện". *Phụ thuộc:* BT-1 để có phân phối tham chiếu. *Rủi ro:* chọn cửa sổ so sánh.

**MI-5 · Kịch bản có xác suất, trình bày kiểu "nhiều trạng thái cùng tồn tại" (L, trung bình–cao).** Đây là phần nên lấy từ trào lưu "quantum": hiển thị phân bố xác suất chế độ thị trường (ví dụ "xu hướng 62% · đi ngang 28% · biến động mạnh 10%") từ mô hình cổ điển (HMM/Markov-switching). Bắt buộc:
- dùng xác suất lọc $P(s_t \mid x_{1..t})$, **không** dùng xác suất làm mượt hay Viterbi trên toàn chuỗi (nhìn trước tương lai);
- fit theo cửa sổ trượt, ghim phiên bản mô hình như feature;
- chỉ hiện con số khi đã có biểu đồ hiệu chuẩn (reliability diagram); chưa hiệu chuẩn thì hiện hạng "cao/trung bình/thấp" (Guo và cộng sự 2017, https://arxiv.org/abs/1706.04599);
- tên hiển thị "Xác suất chế độ thị trường", không dùng chữ "quantum".
*Bằng chứng:* Bayesian HMM 4 trạng thái tốt nhất ngoài mẫu cho BTC trong một nghiên cứu (preprint https://ar5iv.labs.arxiv.org/html/2011.03741; bài Springer https://link.springer.com/article/10.1007/s42521-023-00085-x). *Rủi ro:* cao (lookahead, overfit số trạng thái) — làm sau BT-1.

**MI-6 · Tường thuật tự động từ sự kiện (S, cao).** Câu sinh từ template theo sự kiện đã xác định ("Giá quét đỉnh 3 ngày rồi đóng cửa quay lại dưới đỉnh; cổng xu hướng chưa đạt nên bot đứng ngoài"). **Không dùng LLM để sinh con số.** Dùng chung cho Telegram và bot detail.

**MI-7 · Hậu kiểm theo nhóm (M, cao, gộp với MT-1).** Bảng R-multiple theo chiến lược × lý do vào × lý do thoát × chế độ thị trường lúc vào; trả lời "kiểu lệnh nào kiếm tiền, kiểu nào mất tiền". Tham khảo cách Freqtrade phân tích theo entry/exit tag (mô tả, không sao chép).

### 3.2 Chỉ báo và dữ liệu (DATA, IND)

**DATA-1 · Thu thập dữ liệu thị trường liên tục (M, mở khóa mọi thứ khác).** Worker chỉ đọc dữ liệu công khai (không dùng API giao dịch, không cần API key): nến đã đóng (klines), funding, open interest, long/short ratio, basis; tùy chọn aggTrades và lệnh thanh lý. Lý do làm sớm: Binance chỉ giữ **30 ngày** lịch sử cho open interest, long/short và basis, **48 giờ** cho aggTrades qua REST — không thu từ bây giờ thì sau này không backtest được (đã kiểm tài liệu, xem mục 7). Dùng WebSocket đường dẫn `/market` (URL cũ ngừng 23/04/2026). *Cần owner:* quyền dùng dữ liệu cho PAPER nội bộ (quyết định 7 của kế hoạch cũ). *Rủi ro:* lỗ dữ liệu khi mất kết nối → ghi khoảng trống và đánh dấu chất lượng.

**IND-1 · CVD xấp xỉ từ klines (S).** Klines USDⓈ-M có trường khối lượng mua chủ động (taker buy base volume): delta ≈ 2 × taker_buy − volume. Trả lời "bên mua hay bên bán đang chủ động". *Rủi ro:* thấp; mất chi tiết trong nến.

**IND-2 · Anchored VWAP neo theo quy tắc (S).** VWAP từ đầu tuần/tháng hoặc từ một sự kiện cấu trúc đã xác nhận. Trả lời "người mua từ mốc X đang lãi hay lỗ". *Rủi ro:* nhìn trước nếu neo vào đỉnh/đáy chọn bằng mắt — chỉ cho neo theo quy tắc.

**IND-3 · Funding và open interest làm cổng bối cảnh L4 (M).** "Giá tăng nhờ đòn bẩy mới (OI tăng) hay nhờ đóng vị thế bán; đám đông có lệch quá một phía (funding cực trị)". Chỉ dùng làm bộ lọc/cảnh báo, **không làm ngòi nổ vào lệnh**: chưa tìm thấy bằng chứng peer-reviewed là chúng dự báo lợi suất. *Phụ thuộc:* DATA-1.

**IND-4 · Cấu trúc giá kiểu SMC, bản không nhìn trước (M).** FVG, quét thanh khoản (sweep), BOS/CHoCH, order block, premium/discount. Yêu cầu:
- mọi khái niệm dựa trên swing có trường `confirmed_at` = thời điểm nến thứ N bên phải đóng; tín hiệu chỉ được dùng từ `confirmed_at`;
- test so khớp chạy tăng dần (streaming) với chạy cả lô;
- **không dùng thẳng** thư viện `smartmoneyconcepts` 0.0.27 cho backtest: `swing_highs_lows` so với nến trước và sau nên nhìn trước tương lai nếu dùng theo index gốc (https://github.com/joshyattridge/smart-money-concepts);
- giai đoạn đầu chỉ dùng để **mô tả và gắn nhãn** sự kiện trên biểu đồ/hậu kiểm; muốn thành chiến lược phải qua tiêu chí mục 5 của kế hoạch cũ.
*Bằng chứng:* chưa có nghiên cứu độc lập chứng minh lợi thế sau phí; định nghĩa không thống nhất giữa các tác giả. FVG, premium/discount tính được tức thời; swing/BOS/OB có độ trễ cố định N nến. *Rủi ro:* cao.

**IND-5 · Hurst (DFA) bổ sung bộ lọc ER40 của H06 (S–M).** Đo thị trường đang bám xu hướng hay hồi quy. Dùng DFA thay R/S vì R/S chệch khi mẫu nhỏ (https://arxiv.org/pdf/cond-mat/0103510). *Rủi ro:* trung bình.

**IND-6 · Supertrend / Ichimoku để hiển thị (S, tùy chọn).** Chỉ là lớp vẽ trên biểu đồ cho người quen TradingView. Ichimoku: đường Chikou vẽ giá hiện tại lùi 26 nến, không được dùng như tín hiệu tại nến quá khứ.

**Không đề xuất lúc này:** order book imbalance (không backtest được nếu chưa lưu sổ lệnh), liquidation heatmap (là mô hình ước lượng, luồng thanh lý của Binance chỉ đẩy lệnh lớn nhất mỗi giây nên dễ gây hiểu nhầm), mọi thứ mang nhãn "quantum".

### 3.3 Telegram (TG)

**TG-1 · Tin tín hiệu chuẩn (S, cao).** Dòng đầu: nhãn **PAPER** và mã tín hiệu đối chiếu được với Admin. Sau đó: cặp, chiều, khung, chiến lược + phiên bản release; vùng vào, stop, mục tiêu, R:R ròng sau chi phí, rủi ro % vốn; ba dòng "vì sao" lấy từ MI-1; một dòng "tín hiệu mất hiệu lực khi …"; đèn chế độ (MI-3); câu cảnh báo rủi ro ngắn; link xem chi tiết. Cập nhật (khớp, dời stop, đóng) **reply vào tin gốc**; tin không khẩn gửi im lặng (`disable_notification`).

**TG-2 · Chống spam và giới hạn tốc độ (S).** Hàng đợi theo chat ≤ 1 tin/giây, nhóm ≤ 20 tin/phút, tôn trọng `retry_after` khi bị 429; gộp tín hiệu trùng; giờ yên lặng; bản tóm tắt ngày. Nguồn: https://core.telegram.org/bots/faq (Bot API 10.4).

**TG-3 · Webhook HTTPS cho môi trường triển khai (M).** Local đã có relay `getUpdates`; môi trường thật cần reverse proxy HTTPS và `setWebhook` với `secret_token`. *Cần owner:* nơi chạy.

*Pháp lý:* nếu kênh mở cho người ngoài, các nội dung tín hiệu có thể là quảng bá tài chính (ví dụ FCA FG24/1 áp dụng cho nhóm Telegram) — cần tư vấn theo khu vực trước khi mở.

### 3.4 Danh mục và rủi ro

**PF-1 · Phân bổ rủi ro giữa các chiến lược (M).** Risk parity hoặc trần CVaR giữa H01/H03/H06 bằng tối ưu cổ điển; đây là phần "tối ưu danh mục" mà các thử nghiệm lượng tử nhắm tới, làm bằng máy thường là đủ ở quy mô này. *Phụ thuộc:* BT-1, RISK-1.

## 4. Đánh giá "Quantum trading"

| Loại | Thực tế | Dùng gì cho Crypidian |
| --- | --- | --- |
| Thuật toán lượng tử thật (QAOA, annealing, quantum Monte Carlo, QML) | Nghiên cứu/PoC trên phần cứng nhiễu. HSBC–IBM (09/2025) báo "tới 34%" cải thiện dự báo khớp lệnh trái phiếu nhưng bị chuyên gia phản bác và hiệu ứng có thể do nhiễu phần cứng. Ước lượng lợi thế định giá phái sinh cần hàng nghìn qubit logic chịu lỗi (Goldman–IBM, https://quantum-journal.org/papers/q-2021-06-01-463/) | Không dùng |
| "Quantum-inspired" chạy máy cổ điển (Toshiba SBM, tensor network) | Có sản phẩm, thiếu benchmark độc lập | Không cần ở quy mô hiện tại |
| Bot bán lẻ "Quantum AI" | Bị cảnh báo lừa đảo: FCA (https://www.fca.org.uk/news/warnings/quantum-ai), ASIC Moneysmart, CFTC (https://www.cftc.gov/LearnandProtect/AdvisoriesAndArticles/AITradingBots.html), IOSCO I-SCAN, Finanstilsynet | Tránh hoàn toàn, kể cả chữ "quantum" trong UI/tin nhắn |
| Ý tưởng trình bày nhiều kịch bản có xác suất | Làm được bằng HMM/Markov-switching và Monte Carlo cổ điển | Lấy: MI-5 và mô phỏng drawdown Monte Carlo trong MT-1 |

## 5. Quyết định cần owner

| # | Quyết định | Ảnh hưởng |
| --- | --- | --- |
| 1 | Nguồn dữ liệu được phép cho PAPER nội bộ (dữ liệu công khai Binance: klines, funding, OI, long/short) và thời gian lưu | DATA-1, RT-1, tín hiệu Telegram thật |
| 2 | Danh sách symbol và khung thời gian chạy thật đầu tiên | DATA-1 |
| 3 | Thứ tự mục 6 | Toàn bộ |
| 4 | SMC: chỉ dùng mô tả/gắn nhãn (đề xuất) hay phát triển thành chiến lược thử nghiệm | IND-4 |
| 5 | Kênh Telegram chỉ cho nội bộ hay mở rộng (pháp lý) | TG-1..3 |

## 6. Thứ tự đề xuất

```text
DATA-1 (thu dữ liệu, làm ngay vì lịch sử OI/long-short chỉ 30 ngày)
  └─ RT-1 (dispatcher PAPER) ── TG-1/TG-2 (tín hiệu thật có diễn giải)
MI-1 ── MI-2 ── MI-6        (làm song song, chỉ cần dữ liệu sẵn có)
MI-3, IND-1, IND-2          (S, sau DATA-1 cho dữ liệu thật)
BT-1 ── MI-4 ── MI-5 ── PF-1
IND-3 (sau DATA-1) · IND-4 (sau BT-1, ban đầu chỉ gắn nhãn) · IND-5
```

Lý do: không có dữ liệu thật thì không có tín hiệu thật và không đo được gì; phần diễn giải (MI-1/2/6) tận dụng logic đã có và cho người quản lý giá trị ngay; mô hình xác suất (MI-5) và SMC (IND-4) chỉ nên vào sau khi có backtest đáng tin.

## 7. Nguồn đã kiểm (2026-11-14)

Binance USDⓈ-M (tài liệu chính thức):
- Klines (giới hạn 1500, trường taker buy volume): https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data
- aggTrades (chỉ 48 giờ gần nhất từ 13/08/2026): https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Compressed-Aggregate-Trades-List
- Open interest history (30 ngày): https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics
- Long/short ratio: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Top-Trader-Long-Short-Ratio
- Luồng lệnh thanh lý (chỉ lệnh lớn nhất mỗi giây từ 10/04/2026): https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams
- Change log (URL WebSocket `/public`, `/market`): https://developers.binance.com/docs/derivatives/change-log

Các nguồn khác ghi ngay tại từng mục. Một số chi tiết chưa kiểm được trong vòng này: đường dẫn/giới hạn của top-trader và taker-volume endpoint, weight của depth, định nghĩa `retry_after` trong Bot API; phải kiểm lại trước khi code DATA-1/TG-2.
