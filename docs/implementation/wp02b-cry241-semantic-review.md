# Semantic review — CRY-241 / WP-02B/golden-ref

**Trạng thái: ĐÃ PHÊ DUYỆT TOÀN BỘ.** Người dùng xác nhận "tôi đồng ý vào toàn bộ các thay đổi này" trong phiên Hermes ngày 25/11/2026; ghi nhận dưới tên `leecris241` (Owner theo Itsaplan MCP), kèm comment #4934. Chữ ký dưới là bản ghi xác nhận bằng lời, không phải chữ ký tay tự dựng.

**Mục đích:** xin chữ ký phê duyệt ngữ nghĩa của `FeatureCalculator` và nhãn `MarketRegime`. Bản này đã được duyệt; không còn ô trống cần reviewer điền.

**Phạm vi đã làm:** `tests/unit/test_cry241_golden_feature_formulas.py` (11 golden test, giá trị tính tay), `docs/research/glossary-vi.md` mục CL (nguồn từng công thức). Không đổi code sản phẩm, `formula_version = crypidian-formulas-v1`, `schema_version = crypidian-feature-snapshot-v1` hay snapshot hash cũ.

---

## 1. Cần review 3 nhóm

1. **Ngữ nghĩa công thức & giá trị biên** (mục 2, 3): có chấp nhận các quy ước seed/warmup/mẫu số 0 và các khác biệt có chủ đích với TA-Lib không?
2. **Nhãn regime** (mục 4): có chấp nhận ngưỡng, thứ tự ưu tiên, độ tin cậy và diễn giải "không có hướng mua/bán" không?
3. **Quản trị thay đổi** (mục 5): có đồng ý rằng mọi sửa công thức phải tăng `formula_version` và không đụng hash snapshot cũ không?

---

## 2. Checklist ngữ nghĩa công thức (đánh dấu từng dòng)

| ID | Hànhvi cần phê duyệt | Giá trị golden đã kiểm | Nguồn | Quyết định |
|---|---|---|---|---|
| F1 | `sma`: seed = trung bình cửa sổ đầu đủ `N`; thiếu `N` → `None` | `[1..6], N=3 → (None,None,2,3,4,5)` | Murphy 1999 ch.10 · TA-Lib `sma` | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F2 | `ema`: `alpha=2/(N+1)`, seed = SMA đầu; **không** tự rút ngắn warmup | `[1,2,3,4,5], N=3 → (None,None,2,3,4)`; 4 nến với `N=5` → toàn `None` | TA-Lib `ema` | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F3 | `true_range`: nến đầu `H−L` (chưa có close trước) | nến phẳng → `0` | Wilder 1978 tr.23 | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F4 | `atr`: Wilder, seed = SMA(TR) rồi RMA `((N−1)ATR+TR)/N` | phẳng `N=2 → (None,0,0,0,0)` | Wilder 1978 tr.35 | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F5 | `+DI/−DI/DX`: biến động âm/clamp 0; DX = 0 khi tổng DI = 0; ADX bắt đầu ở `2N−1` | nến phẳng `N=2`: DI/DX/ADX = 0 | Wilder 1978 tr.36–50 · TA-Lib `adx` | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F6 | `rsi`: Wilder RMA;(window phẳng) = 50, chỉ tăng = 100, chỉ giảm = 0; không chia cho 0 | flat `N=2 → (None,None,50,50,50)`; tăng → 100 | Wilder 1978 tr.61–65 · TA-Lib `rsi` (cùng convention flat=50) | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F7 | `efficiency_ratio` (Kaufman): `|Δ_N| / Σ|Δ|`; **đường đi bằng 0 → 0** (khác TA-Lib KAMA trả 1) | flat `N=3 → (None,None,None,0)` | Kaufman 1998 ch.7; khác biệt đã ghi ở memory 22/10 | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F8 | `bollinger_bands`: middle = SMA, std population `ddof=0`; nến phẳng 3 đường trùng nhau | `100/100/100` | Bollinger 2001 ch.3 | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F9 | `zscore`: population std; phương sai 0 → `None`, **không** gán 0; có 2 biến thể prior/exclusive (mặc định) và inclusive | inclusive `[1,2,3,4],N=2 → (None,1,1,1)`; exclusive → `(None,None,3,3)`; flat → `None` | FX-6 · `feature_snapshot.py` | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F10 | `realized_volatility`: `sqrt(mean(log-return²))`, **không annualize** | `approx` đối chiếu công thức | Andersen et al. 2001, Econometrica 69(2) | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F11 | `vwap`: tích lũy `ΣPV/ΣV` **trong một session**; caller chia session, volume 0 → `None` | `(0,1,3) → (None,20,27.5)`; 2 call riêng → `(10,)`, `(20,)` | Investopedia VWAP | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F12 | `macd`: line = EMA(fast)−EMA(slow); signal = EMA của chuỗi MACD đã có giá trị (seed = SMA 2 mẫu đầu); histogram = line − signal; `fast>=slow` bị từ chối | `line[4]=3, line[5]=17/6; signal[5]=35/12, signal[6]=271/108; hist[5]=−1/12` | Appel 1979 · TA-Lib `macd` | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |
| F13 | Warmup multiplier `3×period` cho mọi bộ lọc mũ (EMA/RMA/RSI/ATR/ADX); seed phải đủ 600 nến cho `ema_200` | fixture unit/Compose dùng 640–1200 nến | FX-6 · Murphy 1999 | ☑ Đồng ý ☐ Từ chối ☐ Cần đổi |

---

## 3. Khác biệt có chủ đích cần chữ ký (không được sửa âm thầm)

| Khác biệt | Dự án | Tham chiếu | Lý do dự án | Cần reviewer |
|---|---|---|---|---|
| ER phẳng | `0` | TA-Lib KAMA ER = `1` | Không có chuyển động thì không có hiệu quả xu hướng; tránh "tín hiệu hoàn hảo" giả | ☑ Chấp nhận ☐ Bắt đổi |
| RSI phẳng | `50` | TA-Lib cũng `50` | Trung tính, không thiên hướng | ☑ Chấp nhận ☐ Bắt đổi |
| TR nến đầu | `H−L` | Wilder | Chưa có close trước đó, không đoán | ☑ Chấp nhận ☐ Bắt đổi |
| ADX bắt đầu | chỉ số `2N−1` | Wilder | Cần `N` mẫu DX hoàn chỉnh sau `N` mẫu TR | ☑ Chấp nhận ☐ Bắt đổi |
| Realized volatility | không annualize | Nhiều nơi nhân `sqrt(periods/year)` | Giữ nguyên đơn vị log-return cho so sánh phân vị nội bộ | ☑ Chấp nhận ☐ Bắt đổi |
| VWAP reset | do caller chia session | Định nghĩa VWAP "theo ngày" | Hàm thuần không biết session; trộn ngày là sai | ☑ Chấp nhận ☐ Bắt đổi |
| Z-score phương sai 0 | `None` | Một số thư viện trả `0` | Không gán điểm bất thường khi không có phân tán | ☑ Chấp nhận ☐ Bắt đổi |
| MACD signal seed | SMA của 2 mẫu MACD đầu | TA-Lib có unstable period riêng | Đồng nhất với seed EMA của dự án | ☑ Chấp nhận ☐ Bắt đổi |

---

## 4. Semantic review nhãn `MarketRegime`

Giá trị cho phép: `TREND`, `RANGE`, `HIGH_VOLATILITY`, `UNKNOWN`. **Mọi nhãn đều là trạng thái thị trường, không phải hướng mua/bán và không phải tín hiệu đảo chiều** (đã ghim trong docstring và `reason_vi`).

**Tham số hiện hành** (`FeatureSettings` mặc định): `trend_adx_threshold=20`, `range_adx_max=18`, `range_efficiency_max=0.25`, `high_volatility_quantile=0.90`, `regime_history=90 ngày`, `expected_interval=1h` → `history_count=2160`.

| ID | Điều kiện (theo thứ tự ưu tiên trong code) | Nhãn / reason_code | Độ tin cậy | Cần review |
|---|---|---|---|---|
| R1 | `data_usable == False` | `UNKNOWN` / `DATA_QUALITY_BLOCKED` | `0` | ☑ Đồng ý ☐ Cần đổi |
| R2 | Thiếu `adx_14` / `efficiency_ratio_40` / `realized_volatility_20` hoặc lịch sử RV < 2160 | `UNKNOWN` / `REGIME_WARMUP_INCOMPLETE` | `0`; coverage = min(1, available/3, past/2160) | ☑ Đồng ý ☐ Cần đổi |
| R3 | `RV hiện tại > phân vị 0.90` của 2160 mẫu trước | `HIGH_VOLATILITY` / `RV_ABOVE_PRIOR_QUANTILE` | `1` nếu ngưỡng = 0, ngược lại `min(1, 0.5+0.5·(rv−q)/rv)` | ☑ Đồng ý ☐ Cần đổi |
| R4 | `ADX >= 20` (sau R3) | `TREND` / `ADX_TREND_THRESHOLD` | `min(1, (adx−20)/(100−20))` | ☑ Đồng ý ☐ Cần đổi |
| R5 | `ADX < 18` **và** `ER < 0.25` | `RANGE` / `ADX_ER_RANGE_THRESHOLDS` | `min((18−adx)/18, (0.25−er)/0.25)`, clamp ≥ 0 | ☑ Đồng ý ☐ Cần đổi |
| R6 | Còn lại | `UNKNOWN` / `NO_REGIME_RULE_MATCHED` | `0` | ☑ Đồng ý ☐ Cần đổi |

**Câu hỏi cụ thể cho reviewer — đã trả lời:** người dùng đồng ý toàn bộ: (1) giữ vùng `18 ≤ ADX < 20` và `ER ≥ 0.25` với `ADX < 18` là `UNKNOWN` (fail-closed); (2) giữ `HIGH_VOLATILITY` ưu tiên hơn `TREND`; (3) giữ 2160 mẫu lịch sử (90 ngày × 24h) làm mức tối thiểu. Không yêu cầu đổi ngưỡng.

---

## 5. Quản trị thay đổi

- Nếu reviewer yêu cầu đổi công thức: **phải tăng `formula_version`** (`crypidian-formulas-v1` → v2…), cập nhật golden fixture và glossary; **không** sửa `crypidian-feature-snapshot-v1` hay làm thay đổi hash của snapshot đã lưu.
- Phối hợp FX-6 để tránh sửa cùng một công thức hai lần.
- Golden fixture hiện tại chỉ là bằng chứng đối chiếu kỹ thuật; **không thay thế chữ ký review**.

---

## 6. Bằng chứng đính kèm

- `tests/unit/test_cry241_golden_feature_formulas.py` — 11 test, giá trị tính tay.
- Chạy: golden **11 passed**; cùng `test_feature_snapshot.py` + `test_fx6_warmup_3x.py` → **30 passed / 0,14s**; ruff + `git diff --check` sạch.
- `docs/research/glossary-vi.md` mục CL — bảng nguồn + tên test tương ứng.
- Nguồn đã web_extract thành công 25/11/2026: `ta-lib.org/functions/{ema,adx,rsi,macd}.md`, `investopedia.com/terms/v/vwap.asp`.
- Tracker: comment #4886 (nhận), #4887 (vòng 1), #4894 (vòng 2).

---

## 7. Chữ ký review (đã điền theo xác nhận của người dùng)

| Trường | Giá trị |
|---|---|
| Reviewer (họ tên / username) | `leecris241` (người dùng phiên này) |
| Vai trò (Owner / Strategy Admin / Risk Reviewer) | Owner (theo Itsaplan MCP `viewer.role`) |
| Ngày review | 2026-11-25 |
| Quyết định nhóm 1 (F1–F13) | ☑ Toàn bộ đồng ý ☐ Đồng ý có điều kiện ☐ Từ chối |
| Quyết định nhóm 2 (bảng khác biệt) | ☑ Chấp nhận toàn bộ ☐ Chấp nhận có điều kiện ☐ Bắt đổi |
| Quyết định nhóm 3 (R1–R6) | ☑ Toàn bộ đồng ý ☐ Đồng ý có điều kiện ☐ Từ chối |
| Ghi chú / yêu cầu đổi | "Tôi đồng ý vào toàn bộ các thay đổi này" (xác nhận trong phiên Hermes 25/11/2026; tracker comment #4934). Không yêu cầu đổi ngưỡng hay công thức. |
| Chữ ký | `leecris241` — xác nhận bằng lời, ghi nhận trong phiên; không phải chữ ký tay tự dựng |

Đã cập nhật comment CRY-241 và chuyển ticket từ **In Progress** sang **Done**. Mọi thay đổi công thức sau này phải tăng `formula_version` và làm golden mới.
