# WBS WP-SC — Kiến trúc Strategy Card và Phân tầng Chiến lược L0→L6 (QUANTSMART Synthesis)

**Ngày:** 2026-11-05 · **Tài liệu nghiên cứu:** [QUANTSMART Synthesis & Architecture Plan](../../research/quantsmart-synthesis-and-integration-plan.md) · **Glossary:** [Mục R — glossary-vi.md](../../research/glossary-vi.md)
**Kế hoạch cha:** [Kế hoạch tổng thể độc lập Binance Futures](./2026-10-21-independent-binance-futures-bot.md) · [Plan 02 — Trading Core PAPER](../../implementation/02-trading-core-paper.md) · [Plan 06 — AI Learning](../../implementation/06-ai-learning.md)
**Trạng thái:** DRAFTING & IMPLEMENTATION.
**Chế độ:** PAPER / Offline Simulation. Tuyệt đối không sinh lệnh trực tiếp, không can thiệp cổng sàn, tuân thủ nguyên tắc bất biến của Risk Engine.

---

## 1. Mục tiêu và Ranh giới An toàn

### Mục tiêu
Chuyển đổi mô hình chiến lược của Crypidian từ dạng nguyên khối cố định (hardcoded Python rules) sang **Kiến trúc Bản kê Chiến lược Khai báo (Declarative Strategy Card)** lấy cảm hứng từ QUANTSMART:
1. Mổ xẻ chiến lược thành đồ thị có hướng không chu trình (DAG) phân định 7 tầng chuẩn mực **L0 → L6**:
   - **L0 (Data Hygiene):** Kiểm tra độ sạch của nến và tính nhân quả dữ liệu point-in-time.
   - **L1 (Candle Anatomy):** Thư viện khối nguyên tử đo đạc hình học nến (`lower_wick_ratio`, `upper_wick_ratio`, `body_ratio`, `close_location`).
   - **L2 (Indicators):** Các phép đo làm mượt kinh điển (EMA, ATR, RSI, Bollinger, VWAP).
   - **L3 (Algorithms):** Diễn giải, chuẩn hóa thang đo $[-1.0, 1.0]$, Z-score đa khung thời gian.
   - **L4 (Context Gate):** Cổng lọc bối cảnh thị trường (Trend/Chop, Biến động, Khung giờ). *Nếu L4 là BLOCK $\rightarrow$ Dừng xử lý ngay lập tức (short-circuit).*
   - **L5 (Strategy Trigger):** Ngòi nổ kích hoạt điểm vào lệnh (Breakout, Crossover, Pullback confirm). *Chỉ chạy khi L4 là PASS.*
   - **L6 (Zones / Exits):** Tọa độ Vùng quan sát, Điểm vào, Vùng rủi ro (Stoploss) và Mục tiêu (Take-profit).
2. Tích hợp các bộ kiểm định chất lượng thuật toán trước khi backtest:
   - **Anti-Consensus Illusion Analyzer:** Cảnh báo đa cộng tuyến / ảo giác đồng thuận khi các chỉ báo phụ thuộc cùng một chuỗi giá đóng cửa ($r > 0.85$).
   - **Dead Node Detector:** Phát hiện các khối mồ côi không nối vào đầu ra, các điều kiện logic tự triệt tiêu.
   - **Coordinate Lab Math Engine:** Khảo sát độ nhạy gradient ($\partial y / \partial x$) và quét tìm điểm kỳ dị (singularity sweep) để tránh lỗi chia cho 0 khi nến không có biên độ ($H - L = 0$).

### Ranh giới An toàn Tối cao
- **Không sinh lệnh trực tiếp:** Pipeline L0-L6 chỉ sinh ra đối tượng đề xuất thô `StrategyCandidate`.
- **Chốt chặn Risk Engine:** Mọi candidate bắt buộc phải đi qua `TradePlanner` và `RiskEngine` của Crypidian để thẩm định vốn và tạo `Atomic Risk Reservation` trước khi vào Paper Simulator hay Live Gateway.
- **Bất biến và Provenance:** Mỗi `StrategyCard` được mã hóa băm SHA-256 bất biến (`canonical_hash`), lưu trữ có phiên bản SemVer và quản lý tenant isolation theo `workspace_id`.

---

## 2. Phân chia Gói Công việc (Work Packages Breakdown)

```text
WP-SC.1: Domain Models, Pydantic v2 Schema & Canonical Hash
   │
   ├─► WP-SC.2: Verification Engine (Anti-Consensus, Dead Node, Coordinate Lab)
   │      │
   │      └─► WP-SC.3: Strategy Pipeline Runtime Engine (L0-L6 Topo Executor & Context Gate)
   │             │
   │             └─► WP-SC.4: PostgreSQL Persistence (RLS) & Admin Management API
   │                    │
   │                    └─► WP-SC.5: Microscope Replay UI & Visual Inspector
```

### Chi tiết các gói công việc:

| Mã WP | Tên gói công việc | Phạm vi & Module thực hiện | Tiêu chí Nghiệm thu (Acceptance Criteria) |
| :--- | :--- | :--- | :--- |
| **WP-SC.1** | Domain Models & Declarative Schema | `src/trading_bot/domain/strategy_card/models.py`<br>`src/trading_bot/domain/strategy_card/schema.py`<br>`src/trading_bot/domain/strategy_card/anatomy.py`<br>`tests/unit/test_strategy_card_schema.py` | - Định nghĩa đầy đủ `GraphNode`, `GraphEdge`, `TypedPort`, `ParameterBound`, `CompatibilityMap`, `StrategyCard`.<br>- Triển khai 4 candle anatomy primitives chuẩn mực: `lower_wick_ratio`, `upper_wick_ratio`, `body_ratio`, `close_location`.<br>- Canonical JSON serialization sinh mã băm SHA-256 bất biến xác thực.<br>- 100% unit tests pass, không rò rỉ dữ liệu. |
| **WP-SC.2** | Verification Engine & Coordinate Lab | `src/trading_bot/domain/strategy_card/orthogonality.py`<br>`src/trading_bot/domain/strategy_card/graph_verifier.py`<br>`src/trading_bot/domain/strategy_card/geometry_lab.py`<br>`tests/unit/test_strategy_card_verification.py` | - `AntiConsensusAnalyzer`: Phát hiện chỉ báo tương quan $r > 0.85$, xuất cảnh báo đỏ.<br>- `DeadNodeDetector`: Duyệt ngược từ L6 về L0, phát hiện node mồ côi và điều kiện logic mâu thuẫn.<br>- `CoordinateLabEngine`: Quét không gian tham số, phát hiện điểm chia cho 0 khi nến phẳng ($H=L$), tính gradient sensitivity.<br>- 100% unit tests pass. |
| **WP-SC.3** | Pipeline Runtime Engine L0→L6 | `src/trading_bot/application/strategy_pipeline/pipeline_engine.py`<br>`src/trading_bot/application/strategy_pipeline/context_gate.py`<br>`src/trading_bot/application/strategy_pipeline/trigger_evaluator.py`<br>`tests/unit/test_strategy_pipeline_engine.py` | - Nhận `DecisionSnapshot` thời gian thực, duyệt đồ thị L0→L6 theo thứ tự topo.<br>- L4 Context Gate trả về FAIL $\rightarrow$ ngắt mạch tức thì (short-circuit), không đánh giá L5 Trigger.<br>- L4 PASS + L5 Trigger $\rightarrow$ L6 Zones sinh `StrategyCandidate` hợp lệ chuẩn bị cho `TradePlanner`.<br>- 100% unit tests pass. |
| **WP-SC.4** | Database Persistence (RLS) & Service API | `src/trading_bot/adapters/database/models/strategy_card_entities.py`<br>`src/trading_bot/adapters/database/repositories/strategy_card_repository.py`<br>`migrations/versions/<rev>_strategy_card_storage.py`<br>`tests/unit/test_strategy_card_persistence.py` | - Bảng `strategy_cards` có RLS theo `workspace_id`, lưu payload JSONB canonical và hash.<br>- Trigger append-only / immutable chống sửa đổi phiên bản đã publish.<br>- Application service hỗ trợ đăng ký, xác thực và nạp phiên bản có hiệu lực. |
| **WP-SC.5** | Admin Strategy Cards UI & Automation | `src/trading_bot/templates/admin/strategy_cards.html`<br>`src/trading_bot/admin/strategy_card_service.py`<br>`tests/e2e/test_admin_strategy_cards_playwright.py` | - Xem danh sách thẻ chiến lược, đồ thị L0-L6 và ma trận tương quan trực quan trên Admin Web.<br>- Chạy Playwright E2E tự động trên Desktop (1280x800) và Mobile (390x844), chụp screenshots xác nhận. |
| **WP-SC.6** | Microscope Replay & Visual Inspector | `src/trading_bot/templates/admin/microscope_replay.html`<br>`src/trading_bot/application/strategy_pipeline/microscope.py`<br>`tests/e2e/test_admin_microscope_playwright.py` | - Chế độ Microscope Replay: Soi vết nến 7 tầng L0-L6, hiển thị giá trị các node trung gian tại thời điểm vào lệnh, ngắt mạch L4 và cấu trúc SL/TP L6.<br>- Playwright E2E browser tests trên Desktop + Mobile chụp screenshots. |
| **WP-SC.7** | TradePlanner Bridge Adapter | `src/trading_bot/application/strategy_pipeline/planner_bridge.py`<br>`tests/unit/test_strategy_pipeline_planner_bridge.py` | - Chuyển đổi an toàn từ đề xuất hình học L6 sang `contracts.StrategyCandidate` với `ExitRecipe` chuẩn.<br>- Bảo đảm mọi lệnh mô phỏng PAPER phải qua `TradePlanner` và `RiskEngine` của Crypidian thẩm định vốn. |

---

## 3. Quy trình Kiểm thử TDD và Quy tắc Triển khai

1. **RED First:** Viết các unit tests kiểm tra trước khi cài đặt logic production.
2. **Deterministic & Offline:** Toàn bộ quá trình tính toán hình học, DAG execution và thẩm định chạy thuần túy in-memory, không gọi API ngoài, không phụ thuộc kết nối mạng.
3. **Tuân thủ AGENTS.md:**
   - Mã định danh, tên biến, lớp, hàm viết bằng tiếng Anh.
   - Comment, docstring, giải thích và báo cáo viết bằng tiếng Việt.
   - Bổ sung thuật ngữ vào `docs/research/glossary-vi.md`.
   - Cập nhật nhật ký triển khai vào `docs/implementation/implementation-memory.md` sau mỗi vòng.
