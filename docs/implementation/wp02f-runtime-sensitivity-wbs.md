# WBS — sensitivity replay dùng PAPER runtime evaluator

**Ngày:** 23/10/2026
**Phạm vi:** WP-02F / Task 9–10. Mở rộng `run_runtime_paper_replay` từ BASE-only sang sensitivity run có cấu hình thật, rồi persist từng run/trial/report/scenario/trade outcome qua repository hiện có và PostgreSQL Compose. Không thay đổi Admin API, runtime app, `paper_lifecycle.py` hoặc schema/repository backtest.

> Các giới hạn về replay state/provider và phạm vi “không thay runtime app” dưới đây mô tả WBS sensitivity ban đầu. Follow-up [PP2/RP4/PP6](./wp02e-profile-pinning-and-replay-progression-wbs.md) đã thay đổi/nối evaluator runtime với reducer multi-observation; trạng thái hiện hành ghi tại mục “Kết quả hiện tại”.

## Vấn đề cần giải quyết

**Vấn đề ban đầu:** `run_runtime_paper_replay` chỉ chạy BASE; `run_sensitivity` chưa áp overrides vào evaluator. E2E khi đó dùng callback/trade fixture. Các vấn đề này được xử lý trong WP-02F sensitivity implementation; đoạn này giữ bối cảnh lịch sử.

**Gap ban đầu:** `ReplayEvaluationContext` provider cấp state tại từng observation. Gap này đã được xử lý bằng reducer; provider hiện cấp base account state cùng market/execution facts, reducer sở hữu progression qua event. Giới hạn còn lại là mark-to-market và daily/rolling risk-window semantics.

## Ranh giới cấu hình và bất biến

- Mọi scenario chạy chính `RuntimeReplayEvaluator` → `PaperDecisionCycle` → `TradePlanner`/`RiskEngine` → `PaperExecutionGateway`/position monitor; không thay bằng callback tổng quát tạo `EvaluationTrade` giả.
- Manifest, evaluator, cost model, fill policy và các tham số strategy hiệu dụng phải cùng scenario ID, trial index và hash. Hash thay đổi nhưng runtime values không đổi là sai và phải bị test bắt.
- COST override chỉ được áp vào các khóa được runtime hỗ trợ rõ ràng, cả Planner `CostModel` lẫn PAPER fill policy khi khóa tương ứng ảnh hưởng fill. Không cho override khóa lạ, không cập nhật một phía, không giảm chi phí so với BASE.
- PARAMETER override chỉ được áp dụng nếu strategy runtime có contract rõ ràng cho đúng key/type/range. Không được coi việc đổi `strategy_content_hash` là đã đổi hành vi strategy. Built-in rule hiện hard-code ngưỡng/recipe; nếu chưa có một parameter path đã được kiểm định thì evaluator phải fail-closed và không tạo report/outcome cho scenario đó.
- Mỗi scenario có evaluator/gateway/context-provider state mới; provider phải chứng minh có thể reset/cấp lại cùng input point-in-time, account baseline và reservation behavior cho trial mới. Không chia sẻ reservation side effect giữa runs.
- Backtest report/outcome là evidence của replay PAPER; không tự tạo hay ghi TradeRecord PAPER vận hành. Repository append-only hiện tại lưu BacktestRun/Trial/Report/Scenario/TradeOutcome trong cùng caller transaction.
- Không Binance network/order endpoint; không credential; không dùng `PaperLifecycleCoordinator` hoặc sửa runtime app trong task này.

## Contract được giới hạn cho slice này

- H03-v1 hỗ trợ đúng hai tham số số nguyên: `donchian_lookback` (mặc định 55, miền 20–200) và `volume_median_lookback` (mặc định 20, miền 5–100). Kết quả strategy phải lấy tham số từ `StrategySelection` của runtime scenario; không sửa root settings đã publish hay ghi đè settings version.
- PARAMETER scenario chỉ cho phép thay một hoặc cả hai key trên. Hash strategy hiệu dụng được tạo theo `run_sensitivity`; evaluator phải xác minh hash derived từ release code H03 gốc + overrides và truyền đúng effective parameters vào H03 runtime rule. Key/range khác fail-closed.
- COST scenario của slice này chỉ hỗ trợ tăng đồng thời `entry_fee_rate` và `exit_fee_rate` tới cùng một giá trị. PAPER fill policy hiện chỉ có một `fee_rate`; hai cost model rates phải bằng nhau để phản ánh đúng Planner và fill gateway. Chỉ tăng một phía hoặc override `entry_slippage_rate`/`adverse_funding_per_unit` bị từ chối vì execution event hiện chưa mang đủ mô hình tương ứng.
- Mỗi scenario factory phải tạo `RuntimeReplayEvaluator`, `PaperExecutionGateway`, `PaperDecisionCycle`, provider và reservation recorder mới. E2E chỉ dùng fixture một tín hiệu H03 đơn lẻ mỗi trial, entry→stop exit; không suy rộng kết quả sang nhiều vị thế/risk progression.

## WBS và điều kiện nghiệm thu

| ID | Công việc | Bàn giao | Điều kiện hoàn tất |
|---|---|---|---|
| S1 | Chốt H03 runtime parameters và giới hạn scenario keys. | `StrategySelection`/H03 rule contracts cùng unit tests. | Defaults 55/20; chỉ hai keys trên trong miền; candidate/no-trade thay đổi khi fixture biên chứng minh; key/range lạ bị từ chối. |
| S2 | Viết test RED cho clone/reset context theo từng scenario. | Unit test runner qua BASE + cost + parameter dùng `RuntimeReplayEvaluator` thật. | Mỗi trial tạo gateway/context state riêng; cùng observation được dùng lại từ đầu; mọi scenario manifest/value khớp context. State/reservation của trial trước không rò sang trial sau. |
| S3 | Nối sensitivity evaluator với runtime values. | Runtime replay composition cho BASE và supported variants. | Fee stress ghi bằng nhau vào Planner entry/exit fee và gateway fee; H03 period overrides thay đổi actual breakout condition/candidate. Content hash khớp values; outcome tạo từ gateway fills/monitor, không từ fabricated callback. Unsupported slippage/funding stress bị reject. |
| S4 | Tăng cường contract fail-closed. | Negative unit coverage. | Không persist report/outcome nếu trial bị reject, context thiếu, override không áp dụng, cost source lệch manifest, provider tái sử dụng mutable state, tương lai/ngoài range hoặc outcome totals khác report. |
| S5 | Nối test Compose persistence thật. | Sửa `tests/e2e/test_backtest_outcomes_persistence.py`. | Tạo ít nhất BASE, một COST và một PARAMETER trial với `RuntimeReplayEvaluator`; ghi tất cả qua `BacktestOutcomeRepository.append_sensitivity_run` trong PostgreSQL caller transaction; tải lại report/trade outcomes và kiểm hash/totals/manifest/scenario. Không generic evaluator factory/trade callback. |
| S6 | Kiểm tra tenant, retry, mismatch và migration. | Compose assertions trên role hiện hữu. | Retry nguyên payload idempotent; cùng trial key nhưng override/outcome khác conflict; tenant khác không đọc được; immutable; `alembic check`, downgrade/upgrade hiện hành đạt; image tag dùng shared content-addressed image. |
| S7 | Quality/review handoff. | Unit/static evidence, cập nhật riêng WBS này. | Unit liên quan, Ruff check/format, mypy, `git diff --check` đạt. Compose chỉ chạy khi điều phối viên xác nhận slot; teardown sạch. Nêu rõ gap state/risk progression còn lại. |

## Thiết kế test bắt buộc

1. `test_runtime_sensitivity_applies_cost_override_to_planner_and_fill` — BASE vs COST từ cùng fixture; fee rates, actual gateway fee và net thay đổi đúng theo cùng mức stress qua runtime fill/monitor.
2. `test_h03_runtime_parameters_change_candidate_with_confirmation` — thay Donchian và volume median lookback trên fixture ngưỡng biên; candidate/no-trade thay đổi đúng lookback đã áp dụng và vẫn giữ point-in-time gates.
3. `test_runtime_sensitivity_applies_parameter_override_to_strategy_behavior` — PARAMETER dùng H03 hai keys, tạo candidate/fill/closed trade theo rule hiệu dụng và hash đúng; không chấp nhận chỉ đổi hash.
4. `test_runtime_sensitivity_uses_fresh_context_and_gateway_per_trial` — mutation/counter từ run trước không ảnh hưởng baseline run kế tiếp; cùng observations được dùng lại từ đầu; baseline lặp lại cho hash/outcomes bằng nhau.
5. `test_runtime_sensitivity_rejects_unsupported_or_unapplied_override` — key không được hỗ trợ hoặc context không phản ánh manifest bị từ chối, không có report/outcomes.
6. Compose test phải trực tiếp tạo `RuntimeReplayEvaluator` cho BASE/COST/PARAMETER, chạy entry+exit PAPER từ cùng fixture, persist qua repository thật, query canonical report/outcome hashes và kiểm tra cross-tenant/immutability/retry.

## Giới hạn/parity gate

**Trạng thái tại thời điểm WBS mở:** context provider còn tự cấp account/exposure/risk state theo observation và không có reducer quản lý state từ ledger. Đây là gap đã được xử lý bởi reducer trong [WBS pinning/replay progression](./wp02e-profile-pinning-and-replay-progression-wbs.md); không dùng đoạn lịch sử này làm mô tả source hiện tại. Reducer giờ sở hữu equity/exposure/reservation progression; provider cấp base state cùng market/execution facts. Không đồng nhất reducer với full production lifecycle: mark-to-market và semantics daily/rolling risk window vẫn còn ngoài phạm vi.

## Ngoài phạm vi

- Tenant dispatcher/runtime startup, readiness, live PAPER account lifecycle; do luồng WP-02E runtime bootstrap.
- Ghi reservation/TradeRecord PAPER vận hành hoặc thay `paper_lifecycle.py`.
- Thêm DB schema/repository hoặc migration mới.
- Xếp hạng/tự promote strategy, kết luận profitability, lựa chọn thông số tối ưu.

## Trạng thái và giới hạn trước khi implement

WBS được mở rộng trước khi sửa source. H03 được chọn vì có hai period parameters có ý nghĩa trực tiếp với breakout rule. Mỗi scenario factory vẫn tạo evaluator/gateway/provider riêng để cách ly trial; fixture sensitivity ở WBS này bao phủ một H03 trade mỗi trial. Reducer multi-observation/multi-trade đã được bổ sung trong follow-up RP1–RP4; đây là test scope của sensitivity fixture, không phải tuyên bố reducer chỉ xử lý một trade. Không có mark-to-market hoặc daily/rolling risk-window semantics hoàn chỉnh.

## Tiến độ triển khai

> Các trạng thái “chưa nghiệm thu” trong nhật ký lỗi phía dưới phản ánh thời điểm trước lần full E2E cuối; phần **Kết quả hiện tại** ở cuối tài liệu thay thế các trạng thái lịch sử đó.

- **S1 — Đạt ở unit:** `StrategySelection` nhận đúng hai period tham số H03; H03 dùng giá trị thực tế cho Donchian/volume median. Test ngưỡng biên chứng minh lookback đổi candidate/no-trade; khóa lạ, kiểu bool/số thực và giá trị ngoài miền bị từ chối.
- **S2–S4 — Đạt ở unit slice:** runner yêu cầu factory trả `RuntimeReplayEvaluator` thực, ghim đúng scenario manifest/parameters, và tạo gateway/context provider riêng ở từng trial. COST chỉ nhận đồng thời hai fee bằng nhau; validator đối chiếu manifest, Planner `CostModel` và PAPER fill policy. Unsupported cost/parameter/release fail-closed.
- **Runtime parameter path:** scenario PARAMETER truyền period vào `StrategySelection` của `PaperDecisionCycle`; hash hiệu dụng được tính từ release H03 gốc và override, trong khi hash release đã publish vẫn được xác minh độc lập.
- **S5 — Đạt trong sensitivity scope:** `tests/e2e/test_backtest_outcomes_persistence.py` gọi runtime H03 evaluator cho BASE/COST/PARAMETER và lưu outcomes qua `BacktestOutcomeRepository`; PP5 nối strategy parameters vào runtime. Fixture sensitivity ở đây tập trung một trade/scenario.
- **S6 — Đạt:** final Compose suite kiểm tra migration/RLS/retry; E2E có migration consistency và downgrade/upgrade checks. PP6 H03 Compose bao gồm kiểm tra provider strategy đã pin; targeted PP6 đạt 1/109.47s.
- **Quality kiểm trong workspace:** 67 unit test liên quan đạt; Ruff và mypy phần source/fixture đạt; source của inline Compose probe parse được; `git diff --check` không có lỗi whitespace (Git có cảnh báo đổi LF/CRLF trên các file khác trong workspace).
- **E2E Compose:** final `tests/e2e` đạt **13 passed / 381.82s** trên tree cuối. Teardown xóa project E2E tồn dư; giữ `crypidian-postgres-1` là dịch vụ dev/user-owned.

### Giới hạn đã xác nhận

Sensitivity fixture dùng một H03 candidate/trade mỗi trial và dựng evaluator/gateway/provider/reservation closure mới. RP1–RP4 follow-up hiện chứng minh reducer progression nhiều observation/trade riêng biệt; không suy rộng một-trade sensitivity fixture thành multi-trade sensitivity comparison. Mark-to-market và daily/rolling risk-window semantics vẫn chưa có.

### Follow-up — tính ổn định hash khi dựng lại fixture

- **Lỗi phát hiện:** Compose E2E `tests/e2e/test_backtest_outcomes_persistence.py` thất bại khi so report hash của sensitivity replay với replay độc lập cùng workspace và cùng scenario. Kết quả trade, trade ID và PnL giữ nguyên, nhưng manifest/report hash khác nhau.
- **Nguyên nhân gốc:** `build_h03_runtime_sensitivity()` tạo `risk_id` mới bằng `uuid4()` mỗi lần gọi; ID này được đưa vào `ReplayManifest.risk_profile_version_id`, nên làm đổi `ReplayManifest.content_hash` và report hash dù rule, cost, input bars và outcome không đổi.
- **Sửa tối thiểu:** fixture dùng `uuid5(workspace_id, "h03-runtime-replay-risk-v1")` để tạo risk profile version ổn định theo workspace. Không thay đổi cách version risk profile ở production.
- **Regression/TDD:** test `test_h03_sensitivity_fixture_hashes_are_stable_across_replays` ban đầu thất bại đúng tại so sánh evidence BASE/COST/PARAMETER giữa hai fixture build có thứ tự scenario đảo ngược. Sau khi cố định fixture ID, `.venv/Scripts/python.exe -m pytest tests/unit/test_runtime_replay_fixture.py -q` → **2 passed**; Ruff check, Ruff format check và mypy cho fixture/test đạt; `git diff --check` đạt.
- **Compose:** lần chạy đầu có **1 failed** do hash mismatch trên đây. Lần chạy tiếp theo sau sửa UUID đã qua phép so report hash, nhưng dừng ở assertion trade ID cũ; chẩn đoán được ghi riêng bên dưới. S5/S6 vẫn chưa đạt vì E2E còn đỏ.

### Chẩn đoán E2E — trade ID từ runtime khác fixture cũ

- **Triệu chứng:** sau khi hash BASE/COST/PARAMETER khớp giữa replay đầu và replay độc lập, `tests/e2e/test_backtest_outcomes_persistence.py` dừng tại assertion cuối: test mong `trade:outcome-snapshot`, DB trả `paper:replay:h03-entry-signal:h03-v1`.
- **Nguồn ID thực:** `RuntimeReplayEvaluator.__call__()` tạo idempotency key `replay:{snapshot_id}:{strategy_release_id}`; `PaperExecutionGateway.submit()` tạo `intent_id` bằng `paper:{idempotency_key}`; `_add_to_position()` dùng chính `order.intent_id` làm `position_id`; `_advance_execution()` đưa `position.position_id` vào `EvaluationTrade.trade_id`. Với fixture H03, ID thực ổn định là `paper:replay:h03-entry-signal:h03-v1`.
- **Đánh giá:** `trade:outcome-snapshot` không được tạo ở runtime evaluator/PAPER gateway hiện tại; đây là kỳ vọng còn sót từ fixture outcome tổng hợp cũ. Không đổi ID production để chiều theo assertion.
- **Sửa test:** inline probe E2E xuất `source_trade_id` từ `parameter_run.trade_outcomes[0].trade_id`, rồi so `reloaded_trade_id == source_trade_id`. Assertion xác nhận repository giữ nguyên ID do runtime replay tạo và không ghim test vào định dạng nội bộ của intent. Không thêm unit test cho ánh xạ runtime vì `tests/unit/test_runtime_replay_evaluator.py` đã kiểm tra `EvaluationTrade.trade_id == gateway.position(intent_id).position_id`.
- **Kiểm tra tĩnh sau sửa:** `.venv/Scripts/python.exe -m pytest tests/unit/test_runtime_replay_evaluator.py -q` → **12 passed**; `.venv/Scripts/python.exe -m pytest tests/e2e/test_backtest_outcomes_persistence.py --collect-only -q` → **1 collected**; Ruff check và format check file E2E đạt. Chạy mypy trực tiếp trên file E2E hiện báo lỗi có sẵn tại helper `_compose` (dòng 18): thiếu kiểu trả về (`no-untyped-def`); chưa thay helper ngoài phạm vi sửa assertion. Diff-check không phát hiện whitespace lỗi; Git chỉ cảnh báo chuyển LF/CRLF.
- **Trạng thái tại thời điểm ghi lịch sử:** assertion đã sửa nhưng E2E chưa chạy lại; kết quả full E2E ngày 2026-10-24 ở mục dưới đã thay thế trạng thái này.

### Kết quả hiện tại — 2026-10-24

- Historical full suites: **13 E2E / 375.64s** (initial sensitivity) và **13 E2E / 382.19s** (trước các cập nhật PP2/RP4/PP6 cuối) đã được thay bằng full E2E cuối **13 passed / 381.82s**.
- Current unit/quality evidence: **520 unit passed**; Ruff check đạt, format **227 files already formatted**, mypy `src` thành công trên 94 files; Alembic có single head `b2e7a9d4c631`.
- Reducer hiện sở hữu account/risk/exposure progression theo chuỗi event, provider chỉ cấp base state/market facts. Fixture sensitivity vẫn một trade mỗi trial; reducer multi-trade persistence được kiểm tra riêng tại RP4 (**1 Compose pass / 57.69s**, **30 regression unit pass**). Mark-to-market và semantics risk window ngày/rolling cần hoàn thiện. Independent review: không có P0/P1/source P2; documentation-only P2 đã đóng.
