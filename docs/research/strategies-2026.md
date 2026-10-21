# Nghiên cứu chiến lược cho bot Binance USDT-M Futures

**Ngày chốt nghiên cứu: 07/10/2026, múi giờ Asia/Saigon.** Đây là danh mục giả thuyết để đặc tả và kiểm định, chưa phải kết quả backtest của dự án. Các tham số bên dưới là cấu hình khởi đầu do tài liệu này đề xuất; không phải tham số tối ưu hay cam kết sinh lời của tác giả nguồn. Phạm vi chính là perpetual tuyến tính ký quỹ USDT; chiến lược cần spot hoặc sàn thứ hai được đánh dấu mở rộng.

**Thuật ngữ EN→VI:** lần đầu dùng tên phương pháp tiếng Anh để giữ khả năng tra cứu; bản dịch ưu tiên, định nghĩa ngắn, cách dùng trong bot và nguồn tham khảo nằm trong [Từ điển thuật ngữ giao dịch và AI](./glossary-vi.md). Quy tắc tên code tiếng Anh/comment/hướng dẫn tiếng Việt nằm tại [language policy](../standards/language-policy.md).

## Bảng dịch nhanh các chiến lược và thuật toán trong tài liệu

| ID / thuật ngữ gốc | Bản dịch tiếng Việt dùng trong dự án | Vai trò/giới hạn |
| --- | --- | --- |
| H01 EMA trend + volatility sizing | Theo xu hướng bằng EMA, khối lượng điều chỉnh theo biến động | Theo xu hướng; sizing theo ATR/RV không loại bỏ gap risk. |
| H02 Time-series momentum | Động lượng chuỗi thời gian đa chân trời | Đo lợi suất cùng tài sản trên nhiều lookback; có thể gặp momentum crash. |
| H03 Donchian breakout | Phá vỡ kênh Donchian | Phá ngưỡng cao/thấp quá khứ; phải loại nến đang đánh giá khỏi ngưỡng. |
| H04 Squeeze then volatility expansion | Co hẹp rồi mở rộng biến động | Lọc giai đoạn nén; chỉ hướng breakout mới cho hướng candidate. |
| H05 Trend pullback | Nhịp điều chỉnh trong xu hướng | Tìm điểm tiếp diễn sau hồi ngược xu hướng; tránh nhầm đảo chiều. |
| H06 RSI + z-score mean reversion | Hồi quy về trung bình bằng RSI và z-score | Mua/bán khi độ lệch lớn trong range đã xác định; rủi ro khi range chuyển thành trend. |
| H07 Session VWAP reversion | Hồi quy về VWAP theo phiên | Intraday; session reset và nguồn giá/volume phải được pin. |
| H08 Cointegration pairs, locked hedge ratio | Giao dịch cặp đồng liên kết với tỷ lệ phòng hộ khóa | Hai chân giao dịch; rủi ro quan hệ vỡ và một chân không khớp. |
| H09 Cross-sectional momentum with BTC hedge | Động lượng cắt ngang có phòng hộ BTC | Xếp hạng nhiều coin rồi hedge beta; cần kiểm soát survivorship và universe. |
| H10 Funding crowding reversal | Đảo chiều theo trạng thái funding cực đoan | Funding là proxy chi phí/nghiêng vị thế, không phải phép đếm long/short trực tiếp. |
| H11 Hedged funding/basis carry | Carry từ funding/basis có phòng hộ | Mở rộng ngoài futures đơn lẻ; gồm rủi ro funding, hedge break và collateral. |
| H12 OI + taker-flow breakout confirmation | Xác nhận phá vỡ bằng hợp đồng mở và dòng lệnh chủ động | Cần futures archive/flow data có timestamp; OI không tự chỉ hướng. |
| H13 CVD divergence exhaustion | Phân kỳ CVD để nhận diện khả năng cạn lực | Cần dữ liệu giao dịch có phân loại aggressor; không suy ra từ OHLCV. |
| H14 Book imbalance + signed flow | Mất cân bằng sổ lệnh kết hợp dòng lệnh có dấu | Chiến lược microstructure; nhạy với sequence, latency, spoof/cancel và fill model. |
| H15 Post-macro event breakout | Phá vỡ sau công bố dữ liệu vĩ mô | Dùng lịch, mốc phát hành/nhận dữ liệu và blackout; phản ứng không cố định. |
| H16 Cost-aware return forecast | Dự báo lợi suất có ngưỡng bù chi phí | Chỉ giao dịch khi dự báo ròng vượt chi phí và biên bất định. |
| H17 Meta-labeling + regime routing | Gán nhãn meta để lọc tín hiệu và định tuyến theo trạng thái thị trường | Primary strategy chọn hướng; model phụ lọc tín hiệu; không tự tạo lệnh. |

Các tên mô hình/phương pháp được dịch và giải thích chi tiết tại [Filter-then-Rank, XGBoost, LSTM, iTransformer, PPO](./glossary-vi.md#f-tên-phương-pháp-chỉ-báo-và-viết-tắt-trong-nghiên-cứu-2026), [triple-barrier/meta-labeling](./glossary-vi.md#e-dữ-liệu-và-chiến-lược-huấn-luyện-ai), [walk-forward/purged/embargo](./glossary-vi.md#e-dữ-liệu-và-chiến-lược-huấn-luyện-ai) và [PBO/DSR](./glossary-vi.md#e-dữ-liệu-và-chiến-lược-huấn-luyện-ai). Các chiến lược này là giả thuyết nghiên cứu, không phải kết quả đã xác minh của Crypidian.

## 1. Nguồn mới thực sự trong năm 2026

Ngày của nghiên cứu lấy từ trang xuất bản hoặc lịch sử phiên bản, không lấy năm trên tiêu đề SEO. arXiv là bản thảo công khai; DOI arXiv không đồng nghĩa phản biện học thuật. Bảng này tóm tắt đóng góp và giới hạn, không chuyển lợi nhuận trong bài thành kỳ vọng của bot.

| Nguồn gốc, ngày | Đóng góp có thể áp dụng | Chất lượng bằng chứng và giới hạn |
|---|---|---|
| Bysik & Ślepaczuk, [Machine Learning-Based Bitcoin Trading Under Transaction Costs: Evidence From Walk-Forward Forecasting](https://arxiv.org/abs/2606.00060), nộp 19/05/2026 | So sánh XGBoost/LSTM/iTransformer, 27 cửa sổ walk-forward; bộ lọc biên dự báo so với chi phí | Preprint, một tài sản BTC-USDT; [toàn văn](https://arxiv.org/html/2606.00060v1) nói không có vượt trội Sharpe so với buy-and-hold có ý nghĩa sau Holm, cần mở rộng mô hình spread/funding. Tránh kết luận ML tốt hơn mọi baseline. |
| Lebiedź & Ślepaczuk, [Dynamic Multi-Pair Trading Strategy in Cryptocurrency Markets with Deep Reinforcement Learning](https://arxiv.org/abs/2606.04574v2), nộp 03/06, sửa 25/06/2026 | Chọn cặp Filter-then-Rank; PPO/LSTM điều khiển execution trong ranh giới rủi ro xác định; dữ liệu Binance USD-M 1h | Preprint, có [repo tái lập của tác giả](https://github.com/damianlebiedz/pair-trading-with-rl); bootstrap báo mức ý nghĩa 10%, không đạt 5%. Không sao chép mức đòn bẩy nghiên cứu thành cấu hình production. |
| Kurban và cộng sự, [WebCryptoAgent: Agentic Crypto Trading with Web Informatics](https://arxiv.org/abs/2601.04687v2), nộp 08/01, sửa 22/06/2026 | Tổng hợp nguồn web và tín hiệu cấu trúc; tách suy luận theo giờ khỏi kiểm soát rủi ro theo giây | Preprint; kết quả thực nghiệm do tác giả báo cáo. Bài học kiến trúc hữu ích; không chứng minh prompt LLM có alpha phổ quát. |
| Yu và cộng sự, [Can AI Make Money in Crypto? Measuring the Gap from Backtests to Real Markets](https://arxiv.org/abs/2609.34510), 28/09/2026 | Protocol ba bước: lịch sử, paper tương lai trên sàn, tiền thật; khảo sát khoảng cách do thời gian và execution | Preprint mới; dùng làm khung đánh giá, không làm bằng chứng bot cụ thể đã thắng thị trường. [Hệ thống tác giả](https://github.com/Starlien95/Awesome-TradingAI). |
| Barton và cộng sự, [What LLM Trading Agents Actually Do in Production: A Six-Month, Population-Scale Record from Two Fleets](https://arxiv.org/abs/2609.05663), 04/09/2026 | Quan sát production trên Base và Hyperliquid; vai trò sizing, leverage và exit trong hành vi agent | Preprint quan sát hệ thống do tác giả vận hành, khó tách selection/market exposure; tác giả báo hai fleet không có directional edge. Không dùng win rate làm thước đo đủ cho alpha. |
| Nam Anh Le, [Funding-Aware Optimal Market Making for Perpetual DEXs](https://arxiv.org/abs/2605.06405), 07/05/2026 | Funding là biến trạng thái trong bài toán inventory/HJB | Preprint thiên về mô hình và holdout simulation, hiệu chỉnh trên Hyperliquid; proxy fill và tail funding còn giới hạn. Hữu ích cho inventory control, chưa phải chiến lược Binance đã kiểm chứng. |
| Mallory, [Implied ETF Carry Rates and the Limits of Arbitrage in Segmented Bitcoin Markets](https://arxiv.org/abs/2605.29309), 28/05/2026 | So carry suy từ IBIT options với CME futures; ma sát collateral/margin | Preprint định giá trên mẫu cụ thể, không phải bot perpetual. Hỗ trợ giả thuyết phân mảnh thị trường, không coi chênh lệch carry là lợi nhuận không rủi ro. |
| Cerutti, Firat, Hengge & Sagawa, [Stablecoin Shocks, IMF WP 2026/044](https://www.imf.org/en/publications/wp/issues/2026/03/06/stablecoin-shocks-574528), 06/03/2026 | Event study và SVAR-IV về cầu stablecoin, lợi suất Treasury, USD, lan truyền tài sản | Working paper định lượng của các tác giả IMF, không phải chính sách chính thức của IMF. Cầu stablecoin và thay đổi supply thô không phải cùng một biến nhân quả. |

**Phân biệt mới và cũ:** những nghiên cứu trên được công bố/sửa trong 2026. EMA, Donchian, RSI, Bollinger, VWAP, momentum, pairs trading, carry và meta-labeling đã tồn tại từ trước; phối hợp lại chúng trong 2026 không tạo bằng chứng về phương pháp mới. Nền tảng kinh tế của trend tham khảo [Moskowitz, Ooi & Pedersen, Time Series Momentum (2012)](https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum); crypto momentum tham khảo [Liu & Tsyvinski, Risks and Returns of Cryptocurrency (2018)](https://www.nber.org/papers/w24877). Kết quả trên tập tài sản/kỳ quan sát của hai bài không chứng minh tham số intraday bên dưới có lợi thế.

## 2. Khảo sát ngoài Freqtrade

| Nền tảng/nguồn | Ý tưởng đáng học cho bot độc lập | Cách dùng bằng chứng |
|---|---|---|
| [QuantConnect/LEAN](https://github.com/QuantConnect/Lean) | Engine event-driven, các model fee/fill/slippage thay được; research/backtest/live chung mô hình | Học hợp đồng engine và mô hình execution; open source không bảo đảm alpha. |
| [Hummingbot V2 controllers](https://hummingbot.org/strategies/v2-strategies/examples/) và [funding script chính chủ](https://github.com/hummingbot/hummingbot/blob/master/scripts/v2_funding_rate_arb.py) | Controller tạo intent; executor xử lý vòng đời vị thế, funding, ranh giới exit | Funding arbitrage đã có trong [release 29/04/2024](https://hummingbot.org/release-notes/1.27.0/), không gắn nhãn phát minh 2026. Đặc tả riêng unit funding và hedge. |
| [NautilusTrader depth replay](https://nautilustrader.io/docs/latest/tutorials/backtest_orderbook_binance/) | Replay L2, event/receive time, thử imbalance và fill semantics | [Bản nightly](https://nautilustrader.io/docs/nightly/tutorials/backtest_orderbook_binance/) ghi ví dụ imbalance không có edge; pin release/commit trước khi so sánh API. |
| [Jesse backtest](https://docs.jesse.trade/docs/backtest/) và [strategy API](https://docs.jesse.trade/docs/strategies/api) | Route nhiều timeframe, xác định should_long/short/cancel, chart order lifecycle | Đọc khả năng và hạn chế đúng phiên bản; tuyên bố marketing độ chính xác không thay cho replay của dự án. |
| [TradingView Pine strategies](https://www.tradingview.com/pine-script-docs/concepts/strategies/) | Prototype quy tắc, chart marker và broker emulator | Fill mặc định ở tick kế tiếp; OHLC mô phỏng đường intrabar. [Repainting](https://www.tradingview.com/pine-script-docs/concepts/repainting/) phải kiểm soát khi dùng HTF và dữ liệu chưa đóng. |
| [QuantResearch của López de Prado](https://www.quantresearch.org/Innovations.htm) | Triple barrier, meta-labeling, uniqueness weighting | Nguồn tác giả cho phương pháp; meta-label chỉ lọc cược của primary signal, không tự tạo hướng tốt. |
| [BIS Crypto carry, 2023; PDF sửa 10/2025](https://www.bis.org/publ/work1087.pdf) | Carry, hạn chế vốn arbitrage, margin và crash risk | Cơ sở kinh tế của funding/basis; dated futures basis và perpetual funding phải được mô hình riêng. |

Link master/latest có thể thay đổi sau ngày chốt. Khi tái lập, manifest phải lưu URL, phiên bản/commit, thời gian tải, hash dữ liệu và license. Tài liệu này không đã cài/chạy các nền tảng trên.

## 3. Hợp đồng thời gian và công thức chung

`t` là nến đã đóng và đã nhận; quyết định tại `decision_time >= available_at`. Lệnh vào được mô phỏng tại tick tiếp theo cộng latency. Chỉ báo khung 4h dùng nến 4h cuối đã đóng, kể cả khi nến 1h mới đóng ở giữa nến 4h. `HH_n(t-1)=max(H[t-n:t-1])`, `LL_n(t-1)=min(L[t-n:t-1])`; mức breakout không chứa nến đang đánh giá. Mọi rolling quantile/z-score dùng lịch sử trước thời điểm quyết định. Missing, stale hoặc warm-up chưa đủ: trả `NO_TRADE`, không fill giá trị tương lai.

Ký hiệu và công thức cố định:

- `r_t=ln(C_t/C_{t-1})`; `SMA_n=mean(C[t-n+1:t])`; `EMA_n(t)=αC_t+(1-α)EMA_n(t-1)`, `α=2/(n+1)`, seed SMA của n nến đầu.
- `RMA_n(x)_t=((n-1)RMA_{t-1}+x_t)/n`, seed trung bình n mẫu. `TR=max(H-L,abs(H-C_prev),abs(L-C_prev))`; `ATR_n=RMA_n(TR)`.
- `gain=max(ΔC,0)`, `loss=max(-ΔC,0)`; `RSI_n=100-100/(1+RMA(gain)/RMA(loss))`. Loss bằng 0 và gain dương: 100; cả hai bằng 0: quy ước 50.
- `up=H_t-H_prev`, `down=L_prev-L_t`; `+DM=up` nếu `up>down,up>0`, còn lại 0; `-DM` đối xứng. `DIplus=100 RMA(+DM)/ATR`, `DIminus=100 RMA(-DM)/ATR`; `DX=100 abs(DIplus-DIminus)/(DIplus+DIminus)`; `ADX=RMA(DX)`, mẫu có mẫu số 0 quy ước 0.
- Bollinger: `mid=SMA_20`, `sd=sqrt(mean((C-mid)^2))` dùng ddof=0; `upper/lower=mid±2sd`; `BW=(upper-lower)/mid`.
- `RV_n=sqrt(sum(r^2)/n)` là volatility mỗi bar; nếu annualize dùng số bar/năm của timeframe, crypto 365 ngày. `ER_n=abs(C_t-C_{t-n})/sum(abs(ΔC))`; mẫu số 0 quy ước 0.
- `z_t(x)=(x_t-mean(x[t-n:t-1]))/std(x[t-n:t-1],ddof=0)`; sd bằng 0: bỏ tín hiệu. Percentile dùng cùng cửa sổ quá khứ.
- `VWAP=Σ(P_i V_i)/ΣV_i`; dùng trade price nếu có, proxy nến `P=(H+L+C)/3` phải ghi rõ. Tất cả session VWAP reset lúc 00:00 UTC.

Với hướng `s=+1` long, `s=-1` short, giá khớp vào `E`, `A=ATR_14` tại signal; stop ban đầu `E-s kA`, TP `E+s mA`. Distance `D=kA` cố định tại entry. Trailing long là max(stop trước, highest trade price kể từ entry - kA), short là min(stop trước, lowest trade price + kA); không nới stop. Time stop đếm từ fill. Bar chạm cả stop và TP mà thiếu tick: tính stop trước hoặc đánh dấu ambiguous rồi báo sensitivity. Stop dùng mark hoặc contract price phải khớp cấu hình thực tế; fill không đồng nhất với trigger.

## 4. Mười bảy giả thuyết xác định

Mọi rule dưới đây chỉ tạo intent; risk gate chung vẫn có quyền từ chối. Cross-up(x,y) = `x_prev<=y_prev AND x_t>y_t`; cross-down đối xứng. Một intent mỗi cặp `(strategy_version,symbol,bar_close,direction)`, không martingale, không thêm vị thế thua. Số trong rule là baseline nghiên cứu đã định nghĩa, cần khóa trước out-of-sample.

### H01 — EMA trend với volatility sizing (cổ điển)

- **Khung/features:** 1h EMA20/EMA80, 4h EMA200, ATR14, ADX14. Long khi cross-up EMA20/80, C4h>EMA2004h, ADX1h>=20; short đảo bất đẳng thức và cross-down. Chỉ vào khi flat.
- **Exit:** stop 2ATR; trailing 3ATR; đóng khi EMA cross ngược; tối đa 120 nến 1h.
- **Chart:** hai EMA, EMA200 từ HTF đóng, stop/trailing, nền ADX gate.
- **Thất bại/dữ liệu:** whipsaw trong range; lag ở đảo chiều nhanh. OHLCV chi phí thấp. Kiểm định riêng long/short, độ trễ HTF; EMA chồng chéo H02/H03 không tính là đa dạng độc lập. Tham khảo họ trend trong [AQR 2012](https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum).

### H02 — Time-series momentum đa chân trời (cổ điển)

- **Khung/features:** 4h; `m=(sign(ln(C_t/C[t-42]))+sign(ln(C_t/C[t-126]))+sign(ln(C_t/C[t-252])))/3`. Long m>=1/3, short m<=-1/3 (ít nhất hai chân trời cùng hướng khi không có giá bằng nhau); vào khi flat hoặc sau exit, kiểm tra mỗi 4h.
- **Exit:** m về 0 hoặc đảo dấu; stop 3ATR; trailing 4ATR; tối đa 252 bars. Re-entry cooldown 3 bars.
- **Chart:** score m và RV42; notional tỷ lệ nghịch RV, bị cap bởi risk budget.
- **Thất bại/dữ liệu:** shock đảo chiều, momentum crash, biến động thấp giả tạo dẫn sizing quá lớn. OHLCV rẻ; RV phải có floor. Nguồn crypto momentum [NBER 2018](https://www.nber.org/papers/w24877); các lookback intraday này do tài liệu đề xuất.

### H03 — Donchian breakout (cổ điển)

- **Khung/features:** 1h; long `C_t>HH55(t-1)` và `quote_volume_t>median(volume[t-20:t-1])`; short `C_t<LL55(t-1)` cùng volume gate. HH/LL dùng high/low, volume dùng cùng unit.
- **Exit:** stop 2ATR; long đóng khi `C_t<LL20(t-1)`, short khi `C_t>HH20`; trailing 3ATR; tối đa 120 bars.
- **Chart:** kênh 55/20 đã shift 1, mũi tên bar xác nhận và actual fill.
- **Thất bại/dữ liệu:** false breakout, giao dịch tin tức với spread nở; OHLCV rẻ. Kiểm định khả năng khớp ở next tick/open, không đặt giá fill đúng HH sau khi đã biết close. Rule có thể biểu diễn trong [Jesse API](https://docs.jesse.trade/docs/strategies/api).

### H04 — Squeeze rồi mở rộng volatility (giả thuyết kỹ thuật)

- **Khung/features:** 15m; tại t-1, BW20 dưới percentile20 của 240 BW trước và RV20 dưới percentile30. Long khi t đóng trên HH20(t-1), BW_t>BW_prev và quote volume>1.5×median20; short đối xứng LL20.
- **Exit:** stop 1.5ATR; TP 3ATR hoặc trailing 2ATR; tối đa 32 bars. Một breakout cho mỗi squeeze, reset khi BW vượt median240 rồi có squeeze mới.
- **Chart:** band, BW, phân vị cố định theo quá khứ, RV.
- **Thất bại/dữ liệu:** squeeze kéo dài, gap jump làm expected move không đủ chi phí. OHLCV rẻ; sự kết hợp không có bằng chứng là phát minh 2026. So sánh ablation Donchian không squeeze để xác định gate có giá trị.

### H05 — Pullback theo trend (giả thuyết kỹ thuật)

- **Khung/features:** 1h; long khi EMA20>EMA80, EMA80_t>EMA80[t-3], C4h>EMA2004h, L_t<=EMA20_t và C_t>EMA20_t, RSI14 trong [40,60]. Short EMA20<EMA80, slope âm, C4h dưới EMA200, H_t>=EMA20 và C_t<EMA20, RSI trong [40,60].
- **Exit:** stop 2ATR; TP 3ATR; đóng khi C xuyên EMA80 theo hướng xấu hoặc sau 48 bars.
- **Chart:** EMA zone, RSI, HTF trend, wick-confirm marker.
- **Thất bại/dữ liệu:** bắt nhầm đảo chiều thành pullback. OHLCV rẻ; không dùng pivot/zigzag xác nhận bằng nến tương lai. Kiểm định so H01 cùng risk để phân biệt chất lượng entry với độ dài holding.

### H06 — RSI + z-score trong range (giả thuyết mean reversion)

- **Khung/features:** 15m; range gate ADX14<18, ER40<0.25. Long z20(C)<-2 và RSI14<30; short z20(C)>2 và RSI>70. Không vào khi có macro blackout hoặc giá vượt HH120/LL120 theo hướng xấu.
- **Exit:** z vượt -0.2 cho long hoặc dưới +0.2 cho short; stop 1.5ATR; TP tối đa 2ATR; time stop 16 bars. Không scale-in.
- **Chart:** RSI, z, range gate, mean target tại thời điểm quyết định.
- **Thất bại/dữ liệu:** range thành trend, stop liên tiếp, các kèo nhỏ bị phí ăn hết. OHLCV rẻ; kiểm định stress phí cao và nhóm regime transition. RSI oversold không tự bảo đảm hồi giá.

### H07 — Session VWAP reversion (giả thuyết intraday)

- **Khung/features:** 5m từ tick hoặc OHLCV proxy; `dev=(C-VWAP)/ATR14`; ADX<18, đã có >=12 bars trong ngày UTC. Long dev<-2 và C_t>C_prev; short dev>2 và C_t<C_prev.
- **Exit:** chạm VWAP đương thời, stop 1.5ATR, tối đa 12 bars, đóng trước reset UTC. Entry gate yêu cầu khoảng tới VWAP lớn hơn 3×expected roundtrip cost.
- **Chart:** VWAP tích lũy chỉ đến t, deviation, ngày reset.
- **Thất bại/dữ liệu:** trend day, một print lớn kéo VWAP; 5m proxy rẻ nhưng tick/trade archive tốn storage. Kiểm định reset 00:00 UTC và sensitivity session; không chọn anchor từ đỉnh/đáy tương lai.

### H08 — Cặp cointegration có hedge ratio khóa (cổ điển; RL overlay mới 2026 là mở rộng)

- **Khung/features:** 1h; hàng tuần 00:00 UTC fit OLS `ln(PA)=α+βln(PB)+ε` trên 90 ngày trước; chỉ nhận β>0, ADF residual p<0.05 và half-life 6–72h. `half_life=-ln(2)/ln(φ)` từ AR(1) residual với 0<φ<1. Khóa α/β cho tuần. Z residual so rolling168h trước t.
- **Entry:** z<-2: long A, short B với notional B=β×notional A; z>2: short A, long B. Hai legs phải khả dụng, giới hạn mất hedge ngay khi chỉ một leg khớp.
- **Exit:** abs(z)<0.25, abs(z)>3.5, PnL cặp mất risk budget, hoặc sau 3 half-lives; không tự điều chỉnh stop khi refit. Chart residual, z, β, leg fills và net delta.
- **Thất bại/dữ liệu:** quan hệ cấu trúc vỡ, coin bị delist, funding lệch, một leg không khớp. OHLCV hai coin rẻ; phí và funding cả hai legs. Multiple tests khi chọn nhiều cặp; survivorship phải kiểm soát. [Nghiên cứu 2026 và repo tác giả](https://arxiv.org/abs/2606.04574v2) là bằng chứng nghiên cứu overlay, không bảo đảm rule baseline này có lợi nhuận.

### H09 — Cross-sectional momentum có BTC hedge (cổ điển)

- **Khung/features:** 4h, rebalance 00:00 UTC mỗi ngày; universe tại thời điểm đó gồm USDT perpetual đã niêm yết >=90 ngày, top20 median quote volume30d. Rank `ln(C[t-1]/C[t-43])` (return7d bỏ 4h gần nhất); long top20%, short bottom20%; inverse-RV sizing, equal gross long/short.
- **Exit:** thay giỏ tại rebalance; từng coin stop 3ATR; portfolio gross/coin/BTC-beta cap. Beta rolling30d của returns4h dùng dữ liệu quá khứ; optional BTC hedge theo beta tổng thay vì gọi dollar-neutral là market-neutral.
- **Chart:** rank, weights, gross/net/beta, turnover. Thất bại khi rotation/crash, squeeze short loser; dữ liệu universe/delist lịch sử tốn công. Phí nhiều legs; validation cả all-listed và liquid universe, không lấy top coin hôm nay cho quá khứ. Nền tảng crypto risk/momentum [NBER](https://www.nber.org/papers/w24877).

### H10 — Funding crowding reversal có xác nhận giá (giả thuyết directional)

- **Khung/features:** 1h; `zf=z` của funding rate chuẩn hóa về mỗi giờ trên các settlement đã biết 30d; `zo=z(Δln(OI))` trên 30d. Short khi zf>2, zo>1 và C_t<LL12(t-1); long khi zf<-2, zo>1 và C_t>HH12(t-1). Không short chỉ vì funding dương.
- **Exit:** stop 2ATR, TP 3ATR, time stop24h; đóng nếu breakout ngược. Chart funding observed/settled tách riêng, OI, breakout.
- **Thất bại/dữ liệu:** crowding tiếp tục nhiều ngày; funding có cap/interval khác nhau; OI tăng không cho biết bên long đang có ưu thế. Cần archive OI/funding snapshots hoặc mua lịch sử. Mô hình phí và dấu funding theo vị thế; [BIS carry](https://www.bis.org/publications/working-paper-1087-crypto-carry) hỗ trợ cơ chế crowding, không xác nhận rule reversal này.

### H11 — Funding/basis carry đã hedge (mở rộng ngoài bot chỉ futures)

- **Khung/features:** spot+perp cùng underlying hoặc hai venue; `basis=(perp_mid-spot_mid)/spot_mid`; kỳ vọng cashflow funding theo schedule thực, trừ entry+exit spread/fee, hedge slippage và vốn.
- **Entry:** funding dương và expected net carry của 3 kỳ tới >2×roundtrip cost: long spot, short perp cùng base quantity. Funding âm: long perp, short spot chỉ khi đã có quyền borrow và giá vay xác nhận; nếu không có borrow, bỏ nhánh này. Hai futures venues: short venue funding cao, long venue thấp với matched delta và schedule riêng.
- **Exit:** expected carry<=0, basis loss vượt budget, hedge mismatch, borrow recall hoặc tối đa7d. Chart basis, accumulated funding, hedge error, margin sử dụng.
- **Thất bại/dữ liệu:** reversal funding, venue outage, liquidation dù delta gần0, collateral USDT mất peg. Cần spot/borrow/venue bổ sung, không khả thi đầy đủ với một Binance futures account. Funding dương với vị thế short trần vẫn là rủi ro hướng. Tham khảo [Hummingbot script](https://github.com/hummingbot/hummingbot/blob/master/scripts/v2_funding_rate_arb.py), [BIS](https://www.bis.org/publ/work1087.pdf), và giới hạn phân mảnh trong [Mallory 2026](https://arxiv.org/abs/2605.29309).

### H12 — OI và taker-flow xác nhận breakout (giả thuyết futures)

- **Khung/features:** 15m; `oi_growth=ln(OI_t/OI[t-4])` dùng số contract/base quantity, không dùng USD notional tăng do giá. `TI=(buy_quote-sell_quote)/(buy_quote+sell_quote)` trong bar đã đóng.
- **Entry:** long C>HH24(t-1), oi_growth>0.02, TI>0.15; short C<LL24, cùng oi_growth, TI<-0.15. Cho phép gate oi_growth bằng quantile train-only nếu coin có distribution khác, nhưng không thay live tự phát.
- **Exit:** stop2ATR, trailing2.5ATR, đóng khi TI đổi dấu hai bars liên tiếp hoặc sau32 bars. Chart giá/OI units/TI.
- **Thất bại/dữ liệu:** vị thế mới ở cả hai phía, OI công bố trễ, spoof volume. OHLCV rẻ, OI archive mức trung bình; không suy “giá lên + OI lên = chắc chắn long mới”. Kiểm định ablation từng gate và latency publication.

### H13 — CVD divergence xác nhận exhaustion (giả thuyết orderflow)

- **Khung/features:** 5m; `δV=Σ taker_buy_quote-Σtaker_sell_quote`, `CVD=ΣδV` reset 00:00 UTC, chỉ đánh giá sau ít nhất 20 bars trong cùng session. Short khi H_t>HH20(t-1), C_t<HH20(t-1), `CVD_t<CVD[t-20]`; long L_t<LL20, C_t>LL20 và CVD_t>CVD[t-20]. Không gọi đây là phát hiện mọi absorption.
- **Exit:** stop1.5ATR, TP2ATR, tối đa12 bars, đóng trước reset. Chart giá, CVD, sweep level và sequence.
- **Thất bại/dữ liệu:** spot dẫn hướng nhưng futures flow khác, aggressive flow còn kéo dài; cần trades hoặc taker buy quote từ nến; data gộp không thấy hidden liquidity. Chi phí cao do vòng ngắn; validation local futures và spot-confirmed variant riêng, không trộn data vendor units.

### H14 — Book imbalance + signed flow (microstructure, nghiên cứu nâng cao)

- **Khung/features:** snapshot L2 hợp lệ liên tục; `IB=(Σbid_qty1:5-Σask_qty1:5)/(Σbid_qty1:5+Σask_qty1:5)`; `TF=(buy_qty_3s-sell_qty_3s)/(buy_qty_3s+sell_qty_3s)`. Long IB>0.6, TF>0.3 liên tục1s; short IB<-0.6, TF<-0.3. Spread<=2bps, book age<=250ms, cooldown10s; quantity<=1% visible opposite depth.
- **Exit:** mid move thuận 8bps, adverse5bps, IB qua0, hoặc10s; FOK/IOC limit có price protection, không giả định maker queue fill. Chart imbalance/depth/spread/receive latency từng tick.
- **Thất bại/dữ liệu:** spoof/cancel, queue loss, network latency, hidden/RPI liquidity. L2 có sequence gaps: reset book và bỏ trading; data/storage/latency chi phí cao. [Cont–Kukanov–Stoikov](https://arxiv.org/abs/1011.6402) nghiên cứu cổ phiếu, không chứng minh Binance edge; [Nautilus ví dụ](https://nautilustrader.io/docs/nightly/tutorials/backtest_orderbook_binance/) chỉ minh họa engine. Chỉ triển khai sau khi replay L2 cho thấy lợi thế sau chi phí.

### H15 — Post-macro event breakout (giả thuyết có lịch sự kiện)

- **Khung/features:** CPI/PCE/NFP/FOMC từ calendar gốc; 1m/5m price, spread, optional ΔUS2Y/ΔDXY/Δequity futures từ licensed feed. Blackout entry từ -15m đến +5m. Tạo range high/low trong [release,release+5m); chỉ sau khi bar +5m đóng và dữ liệu nhận đủ.
- **Entry:** từ +5m tới+30m, long close vượt event high và spread<percentile90 pre-event; short dưới event low. Optional xác nhận long khi US2Y/DXY giảm và equities tăng, short đối xứng, chỉ khi đã chứng minh quan hệ trong train regime. Nếu không có intraday macro feed, dùng price breakout thuần; không giả lập confirmation từ daily close.
- **Exit:** stop1.5ATR5m, TP3ATR, tối đa60m từ fill, flatten trước press conference nếu đang trade statement. Chart release/received timestamps, event range, actual/consensus/surprise nếu có.
- **Thất bại/dữ liệu:** statement rồi press conference đổi hướng, revision bất ngờ, whipsaw, spread nở. Calendar miễn phí; consensus và feeds nhanh thường trả phí. [IMF crypto cycle](https://www.imf.org/en/publications/wp/issues/2023/08/04/the-crypto-cycle-and-us-monetary-policy-534834) hỗ trợ quan hệ theo regime; hướng phản ứng không được hardcode như chân lý.

### H16 — Dự báo lợi suất với cost hurdle (hướng có nghiên cứu 2026)

- **Khung/features:** 1h; XGBoost baseline với lag returns1/3/6/24h, EMA distance/ATR, RSI, RV, volume z, funding/OI đã nhận. Target log return next4h. Fit scaler/features/model chỉ train; version model khóa cho test fold.
- **Entry:** long `mu_hat > lambda*expected_roundtrip_cost + uncertainty_margin`; short `mu_hat < -(lambda*cost+margin)`; baseline λ=2, margin là percentile75 của abs forecast error train calibration. Khi đang có vị thế giữ nguyên nếu chưa qua hysteresis exit, không đảo theo mỗi sign.
- **Exit:** forecast net edge<=0 trong hai bars, stop2ATR, TP3ATR, hoặc4h. Chart prediction, uncertainty, cost threshold, model version.
- **Thất bại/dữ liệu:** drift, calibration lỗi, ít trades sau filter; inference và dataset cost trung bình. Bằng chứng [Bysik & Ślepaczuk 2026](https://arxiv.org/abs/2606.00060) hỗ trợ kiểm định cost-aware overlay; horizon/rules ở đây là mở rộng của tài liệu, không replication nguyên bài. Purge label overlap và báo số trades/coverage, không chọn model theo toàn bộ test return.

### H17 — Meta-labeling và regime routing (phương pháp đã có trước 2026)

- **Primary:** H01/H03 cho trend khi ADX>=20, H06 cho range khi ADX<18 và ER<0.25, còn lại flat. Không cho hai primary đối nghịch mở cùng một symbol. `side` chỉ do primary quyết định.
- **Label/features:** triple barrier trên next24h: TP2ATR, SL1.5ATR, expiry24h; `y=1` khi PnL ròng sau phí/funding dương tại barrier đầu, ngược lại0. Feature tại signal: primary ID, ADX/RV/spread, funding/OI, time-to-event, portfolio exposure. LLM không được thấy tương lai của label.
- **Entry:** classifier hiệu chỉnh probability p; vào theo primary nếu `p > (L+c)/(G+L)` và p>=0.6, với G/L payoff lịch sử train sau quy ước units, c là chi phí chưa nằm trong G/L. Nếu dùng G/L đã net chi phí thì c=0. Size chỉ trong risk cap; không gấp thếp theo p.
- **Exit:** primary/triple barrier, classifier không được dời hard stop. Chart primary direction, p, rejection reason, regime.
- **Thất bại/dữ liệu:** label contamination, base signal không có edge, routing overfit; OHLCV rẻ nhưng cần audit sample timestamps/model artifacts. Tham khảo phương pháp gốc [QuantResearch](https://www.quantresearch.org/Innovations.htm), không gọi meta-labeling là chiến lược mới 2026. Purge mọi train label kết thúc trong test interval và embargo theo horizon, chọn threshold trong inner walk-forward.

## 5. Bộ lọc regime và quản lý rủi ro dùng chung

Các ngưỡng là đề xuất kỹ thuật cần kiểm định, không là phát hiện trong nguồn: `NORMAL` khi RV dưới percentile90 past90d và spread dưới percentile95 past7d; `VOLATILE` khi vượt một ngưỡng thì giảm risk budget còn1/2; `SHOCK` khi abs(return1m)>5×RV1m past24h hoặc spread>3×median1h thì chặn entry và để policy bảo vệ vị thế hoạt động. Recovery cần 30 bars1m hợp lệ dưới ngưỡng. Funding/OI thiếu không làm tắt quản lý stop; chỉ tắt strategy phụ thuộc feature đó.

Risk per trade baseline 0.25% equity, tổng open stop risk<=1%, coin<=0.5%, daily realized+unrealized loss budget2%, leverage cap mặc định2x. Sizing `qty=floor_to_step(min(risk_money/(D+adverse_exit_cost_per_unit), notional_cap/E, margin_cap*leverage/E))`. Tick/step/min notional và maintenance-margin tiers lấy metadata thực. Leverage giảm margin requirement nhưng không giảm price risk; không dùng liquidation làm stop. Portfolio cap tính tương quan/BTC beta cho nhiều coin, carry/pair dùng leg gross và scenario basis/hedge-break risk.

Trend/range gates là heuristic; benchmark vẫn cần strategy không gate để xem lợi ích. HMM/change-point nâng cao chỉ dùng filtered state `P(state_t|data_<=t)`, không dùng smoothed state chứa tương lai. RL/LLM nên là overlay đề xuất hoặc veto có bounded outputs; order placement, leverage, stops và kill switch thuộc lớp xác định. Những quan sát của [production LLM 2026](https://arxiv.org/abs/2609.05663) là lý do để kiểm định sizing/exit, không phải bằng chứng rằng mọi AI đều thất bại.

## 6. Chi phí, chart và kiểm định bắt buộc trước khi cấp vốn

Với perpetual tuyến tính: `PnL_net = Σ qty_signed*(exit_fill-entry_fill) - Σ abs(fill_qty)*fill_price*commission_rate - Σ(position_qty_signed*mark_at_settlement*funding_rate) - other_costs`. Positive funding làm long trả, short nhận; funding áp dụng đúng thời điểm giữ vị thế. Slippage đã phản ánh trong fill thì không trừ lần thứ hai. Basis carry có spot/borrow/hedge fees riêng. Dùng commission tier tài khoản ở từng thời điểm, maker/taker theo fill thực; baseline nghiên cứu có thể giả định taker cả hai phía và stress gấp đôi fee/slippage, nhưng không gọi giả định đó là fee Binance hiện hành.

Expected roundtrip hurdle dùng spread, fee, price impact/latency và funding dự kiến bảo thủ; chi phí thực ghi ledger theo từng fill/settlement. Cấm fill tất cả limit orders chỉ vì high/low chạm mức giá: cần queue/fill model hoặc giả định bảo thủ. Dữ liệu 1m không thể chứng minh strategy10s. Bar stop/TP đồng thời, funding time đồng bar, partial fill và outage là kịch bản có báo cáo riêng.

Mỗi chart phải có: signal tại bar close, submit/ack/fill khác marker, entry price, stop/TP đang hiệu lực, HTF close time, feature value/gate, equity net fee/funding, drawdown, gross/net/beta exposure, predicted-vs-realized slippage, latency và dữ liệu thiếu. Hover/export phải truy được intent ID/model version/data hash. Heikin-Ashi/Renko/zigzag có thể dùng minh họa; không dùng synthetic price làm giá khớp.

Quy trình nghiên cứu đề xuất (chưa thực hiện trong tài liệu):

1. Cố định universe lịch sử bao gồm delist/onboard, snapshot metadata và dữ liệu point-in-time. Split thời gian train/validation/test, giữ giai đoạn cuối nguyên vẹn; inner selection không xem final test.
2. Walk-forward rolling theo lịch đã định; purge label horizon, embargo samples phụ thuộc; scaler, percentile, OLS hedge ratio, feature selection và calibration đều fit trong train.
3. Benchmark buy-and-hold unlevered, cash, primary không overlay và đơn giản EMA/Donchian cùng risk; report fees/funding tách riêng, annualization đúng timeframe. Không so leveraged CAGR với unlevered benchmark mà bỏ beta/risk.
4. Báo returns net, Sharpe/Sortino/Calmar, max drawdown, expected shortfall, turnover, capacity, số trade, exposure, long/short và từng regime. Block bootstrap CI; lưu cả thử nghiệm bị loại. [Deflated Sharpe Ratio, Bailey & López de Prado (2014)](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) xử lý selection bias/non-normality; không “chứng nhận” alpha.
5. Stress latency/spread/fee/funding/partial fills; kiểm tra độ nhạy vùng tham số rộng, ablation từng feature, chạy permutation/placebo phù hợp. Loại chiến lược phụ thuộc vài trades/outlier hoặc một regime duy nhất.
6. Sau offline: replay dữ liệu chưa từng dùng, shadow/paper trên production market feed, rồi canary vốn nhỏ theo tiêu chí định sẵn. Testnet xác nhận hợp đồng API, không đại diện liquidity/funding production. So backtest với paper theo fill, cost, latency và reason code; [benchmark 2026](https://arxiv.org/abs/2609.34510) cung cấp cơ sở cho phân tầng này.

Ưu tiên nghiên cứu phiên bản đầu: H01/H03 làm baseline rẻ; H06 là control regime; H12/H10 chỉ sau khi có đủ futures archive; H16/H17 sau khi pipeline point-in-time ổn định. H08/H09 tăng độ phức tạp portfolio; H11 cần mở rộng phạm vi; H14 và funding-aware market making 2026 cần dữ liệu/fill model chuyên sâu. Đây là thứ tự chi phí triển khai do tài liệu đề xuất, không xếp hạng lợi nhuận.

## 7. Khoảng trống bằng chứng tại ngày chốt

Chưa có chiến lược nào trong danh mục được dự án tái lập/backtest/paper/live. Các preprint 2026 được đọc ở metadata/abstract, riêng hai bài cost-aware và pairs được kiểm tra thêm toàn văn; chưa audit dataset/repo hay tái tính bảng kết quả. Nguồn về microstructure ở equity/DEX không thể suy rộng tự động sang Binance. Chưa chốt license/chi phí của L2 lịch sử, macro consensus và feed nhanh; vì vậy các chiến lược phụ thuộc chúng phải ở trạng thái experimental. Hiệu suất một cấu hình trong bài, rank GitHub hay sự phổ biến của indicator không là tiêu chí cấp vốn.
