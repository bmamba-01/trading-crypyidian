# WBS: nối PAPER runtime evaluator vào replay/backtest

**Ngày duyệt:** 23/10/2026  
**Phạm vi:** WP-02F / WP-02 Task 9–10. Chỉ nối evaluator PAPER dùng chung vào runner và tạo kết quả có thể truyền qua hợp đồng persistence hiện có. Không sửa migration hoặc repository backtest outcomes.

## Vấn đề và API hiện có

`BacktestRunner.run` nhận callback trả một `EvaluationTrade | None` cho mỗi snapshot. `RuntimeReplayEvaluator` là callback trạng thái: nó trả trade khi vị thế đóng, nên `opened_at` có thể thuộc một snapshot trước đó. Runner hiện từ chối trường hợp này vì buộc `opened_at >= decision_time` của snapshot đóng. Persistence hiện có là `BacktestOutcomeRepository.append_sensitivity_run(workspace_id, sensitivity_run, trade_outcomes)`; nó kiểm report totals và lưu sensitivity manifest cùng outcome theo thứ tự/hash.

Phạm vi nhỏ nhất là khai báo rõ callback nào phát trade tại thời điểm đóng. Với callback stateful `emits_trade_on_close=True`, runner yêu cầu `range_start <= opened_at <= closed_at <= observation.decision_time < range_end`. Callback generic hiện hữu vẫn giữ hợp đồng legacy: trade được phát tại decision, có thể mang `closed_at` tương lai mô phỏng; thay đổi toàn cục sẽ phá fixture replay/sensitivity/E2E đang sở hữu bởi luồng khác. Thêm composition helper chỉ chạy một scenario BASE, tạo `RuntimeReplayEvaluator` bằng manifest thực tế của scenario, thu cùng các trade evaluator trả về và trả `SensitivityRun + tuple[EvaluationTrade, ...]` đúng shape cho repository hiện có.

## WBS

1. **Viết test RED cho runner:** callback trả trade đã mở ở observation trước và đóng tại observation hiện tại phải được nhận; trade mở trước range, mở/đóng sau observation, hoặc đóng ngoài range phải bị từ chối.
2. **Viết test RED cho composition:** helper chạy `BacktestRunner` với `RuntimeReplayEvaluator` cụ thể qua hai snapshots (PAPER entry rồi stop exit), tạo report đã đóng và thu chính trade đã dùng cho report.
3. **Viết kiểm tra hợp đồng persistence trước khi code:** output phải là `SensitivityRun` scenario BASE cùng `trade_outcomes` tuple; truyền qua `BacktestOutcomeRepository.append_sensitivity_run` bằng fake session query-aware phải đi qua cả `BacktestRepository.append_result` và outcome repository, append run/trial/report/scenario/outcome với cùng manifest/trade ID. Không thay repository/migration.
4. **Implement phần nhỏ nhất:** sửa điều kiện thời gian trong `BacktestRunner`; thêm `run_runtime_paper_replay` trong module application mới/cùng adapter để gọi sensitivity BASE và thu outcome. Mọi evaluator exception/lineage reject vẫn tạo trial FAILED/REJECTED; không trả outcome để persist khi replay không hoàn tất.
5. **Chạy regression và kiểm tra chất lượng:** adapter + runner + outcomes repository unit tests, Ruff check/format, mypy module nguồn và `git diff --check`. Không chạy exchange/network; không sửa E2E/repository/migration do quyền sở hữu file thuộc luồng song song.
6. **Cập nhật memory:** ghi command/kết quả và giới hạn, sau đó đọc lại entry vừa ghi.

## Điều kiện nghiệm thu

- Replay runner chấp nhận trade của callback close-emitting chỉ khi `range_start <= opened_at <= closed_at <= observation.decision_time < range_end`; evaluator không thể ghi trade từ tương lai hoặc trước replay range. Callback generic legacy không đổi.
- Hàm composition thực sự truyền `RuntimeReplayEvaluator` đã cấu hình manifest cho `BacktestRunner` qua API sensitivity BASE; không tự tính PnL hoặc thay strategy/risk/fill.
- Hai-observation test tạo trade PAPER đã đóng qua runtime gateway/monitor; report count, gross/fee/funding/slippage/net totals khớp chính `EvaluationTrade` được thu.
- Kết quả hoàn tất có `SensitivityRun` và outcome tuple được repository hiện có chấp nhận, lưu run/trial/report/scenario/trade theo manifest/hash; kiểm thử gọi thật cả hai repository bằng fake session query-aware, không monkeypatch repository.
- Missing context, future bars, replay failure hoặc mismatch phải fail-closed và không tạo payload report/outcome giả.
- **Trạng thái tại thời điểm WBS ban đầu:** context provider còn sở hữu account/risk/exposure/reservation progression và Compose chưa xác nhận reducer/persistence end-to-end. Đây là mô tả lịch sử, đã được thay thế bởi reducer trong [WBS PP2/RP4/PP6](./wp02e-profile-pinning-and-replay-progression-wbs.md): hiện reducer sở hữu progression, provider cấp base state và market/execution facts.

## Kết quả vòng triển khai 23/10/2026

- Đã thêm marker `emits_trade_on_close` cho evaluator stateful và adapter thu outcome; `BacktestRunner` áp rule close-time nghiêm cho adapter này, giữ nguyên callback generic cũ để không đổi fixture replay độc lập.
- RED xác nhận runner trước đó từ chối trade mở ở observation trước (`TRADE_BEFORE_DECISION`) và không từ chối close-time tương lai cho evaluator stateful. Sau implement, boundary tests phân biệt rõ generic/stateful.
- Đã thêm `run_runtime_paper_replay`: chạy BASE scenario qua `run_sensitivity`/`BacktestRunner`, dùng manifest sau khi được runner ghim, thu trade thực từ `RuntimeReplayEvaluator`, trả `SensitivityRun` cùng tuple outcome. Nếu run lỗi, chỉ trả partial count chẩn đoán; không trả trade payload để lưu cùng report không hoàn tất.
- Integration unit dựng PAPER entry rồi stop exit qua gateway/monitor, xác nhận report totals bằng outcome, sau đó gọi `BacktestOutcomeRepository.append_sensitivity_run` với fake session query-aware. Test chạy thật cả `BacktestRepository.append_result` lẫn outcome repository và xác nhận run/trial/report/scenario/trade cùng manifest, trial ID, report hash, scenario ID, trade ID và workspace-scoped lookups. Không monkeypatch repository; không sửa repository/migration.
- **Kiểm tra đạt:** `.venv/Scripts/python.exe -m pytest tests/unit/test_backtest_runner.py tests/unit/test_runtime_replay_evaluator.py tests/unit/test_backtest_outcomes_repository.py tests/unit/test_backtest_sensitivity.py tests/unit/test_paper_decision_cycle.py tests/unit/test_paper_execution.py tests/unit/test_position_monitor.py -q` → 54 passed; Ruff check; format check bốn file; mypy hai module source; `git diff --check` sáu file.
- **Chưa chạy E2E:** boundary thay đổi là Python callable composition/unit persistence contract; test Compose persistence của agent khác giữ nguyên API và không sửa repository/schema/E2E. H03 runtime application wiring và Compose E2E đầy đủ vẫn còn yêu cầu độc lập.
