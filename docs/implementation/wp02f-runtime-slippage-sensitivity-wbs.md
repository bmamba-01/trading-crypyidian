# WBS — sensitivity trượt giá qua PAPER runtime

**Ngày:** 23/10/2026  
**Phạm vi:** Follow-up WP-02F / Task 9 Step 3. Bổ sung sensitivity cho phí và một biến thể trượt giá có contract được áp dụng đồng nhất vào sizing Planner và fill PAPER, rồi lưu manifest/scenario/report/outcome bằng repository hiện có. WBS này đã được implement và kiểm tra trong full Compose suite; lịch sử bên dưới được giữ làm nhật ký, bảng evidence cuối là nguồn trạng thái hiện tại.

## Mục tiêu và ranh giới

Đóng khoảng trống runtime sensitivity: hiện BASE/COST-fee/H03-PARAMETER chạy qua `RuntimeReplayEvaluator`, nhưng COST chỉ hỗ trợ tăng đồng thời `entry_fee_rate` và `exit_fee_rate`. Task 9 yêu cầu sensitivity phí/trượt giá; thay đổi manifest đơn thuần hoặc chỉ chỉnh `CostModel` không đủ nếu `PaperExecutionPolicy` vẫn tạo fill theo mức khác.

Lát cắt nhỏ nhất được đề xuất hỗ trợ là **một stress trượt giá PAPER đối xứng**, áp dụng cùng rate cho entry và exit. Giữ funding và spread stress fail-closed trong follow-up này. Không tự tạo funding charge từ `CostModel`, không thay planner/gateway một phía, không gọi sàn và không tuyên bố đây là mô hình fill thực tế.

## Khảo sát contract hiện tại

- `domain/risk/planner.py::CostModel` có `entry_slippage_rate`, nhưng chưa có exit slippage rate. Planner dùng entry rate để điều chỉnh giá entry và ghi entry slippage estimate; hiện `RiskSizingInput.slippage_stress_per_unit` được truyền `ZERO`, nên chưa dự trù trượt giá ở lệnh đóng trong risk sizing.
- `domain/execution/paper.py::PaperExecutionPolicy` có `spread_rate` và `slippage_rate`; gateway áp tổng hai rate ở cả `_create_fill()` và `_close_fill()`. Fill đang ghi tổng chênh lệch giá khớp so với reference vào `PaperFill.slippage_cost`; report hiện không tách spread khỏi slippage.
- `application/runtime_replay_evaluator.py::_validate_context()` đối chiếu manifest với các trường `CostModel` và đối chiếu `fee_rate` gateway, nhưng chưa kiểm tra spread/slippage policy so với manifest. Sensitivity validator hiện từ chối override `entry_slippage_rate` và `adverse_funding_per_unit`.
- `CostModel.adverse_funding_per_unit` đi vào ước tính RiskPlanner, nhưng replay không phát funding vào vị thế. Funding thực tế chỉ được cộng qua `PaperExecutionGateway.apply_funding_event()`; `ReplayEvaluationContext` hiện chỉ mang `execution_events` là `PaperBar`, và evaluator không gọi API funding.
- Repository hiện lưu `ReplayManifest.cost_assumptions`, scenario override dạng JSON canonical, report payload và canonical trade outcomes. Report/outcome validation kiểm tổng gross, fee, funding, slippage và net; không cần schema mới nếu các key mới vẫn nằm trong JSON hiện hữu.
- Unit tests hiện có gồm `test_runtime_sensitivity_builds_fresh_cost_context_and_fill_gateway_per_trial` (fee stress), `test_runtime_sensitivity_rejects_unsupported_cost_override_before_factory`, và PAPER execution tests cho fee/slippage/funding riêng. Chưa có test runtime sensitivity chứng minh một slippage override làm thay đổi cả plan risk budget lẫn entry/exit fills.

## Contract mục tiêu cho follow-up này

### Scenario được hỗ trợ

1. **BASE:** giữ cost assumptions đã ghim vào manifest; context, Planner và gateway phải phản ánh đúng cùng profile.
2. **FEE:** giữ contract hiện tại: tăng `entry_fee_rate` và `exit_fee_rate` đồng thời tới cùng một mức vì `PaperExecutionPolicy` chỉ có một `fee_rate`.
3. **SLIPPAGE:** nhận đúng hai override, `entry_slippage_rate` và `exit_slippage_rate`, phải hữu hạn, không âm, tăng ít nhất một trong hai so BASE và bằng nhau trong lát cắt này. Rate phải nhỏ hơn 1. Lý do bắt buộc bằng nhau: Paper gateway dùng một `slippage_rate` đối xứng cho fill entry và exit.

`CostModel` được mở rộng bằng `exit_slippage_rate` để Planner có thể tính khoản trượt giá thoát trong `slippage_stress_per_unit`. Planner tiếp tục áp `entry_slippage_rate` vào entry price; không cộng entry slippage lần hai vào risk sizing. Phần exit stress mỗi đơn vị được tính theo stop/exit reference, contract multiplier và `exit_slippage_rate`, sau đó tham gia `estimated_loss`/quantity cap. Kiểm tra phải chứng minh không đếm trùng entry và không bỏ sót exit.

Trong SLIPPAGE trial:

- `CostModel.entry_slippage_rate == CostModel.exit_slippage_rate == PaperExecutionPolicy.slippage_rate ==` rate ghi trong manifest.
- `PaperExecutionPolicy.spread_rate` phải được pin rõ trong manifest, giữ nguyên giữa BASE và SLIPPAGE, và bằng 0 trong lát cắt này. Planner hiện không có spread input nên không được cho sensitivity đổi spread hoặc chạy SLIPPAGE với spread chưa được mô hình trong sizing.
- `adverse_funding_per_unit` phải bằng 0 trong fixture SLIPPAGE; funding stress bị từ chối.
- Không sửa root settings đã publish. Mỗi scenario tạo CostModel/gateway/provider/reservation riêng; strategy, risk profile, execution bars và các input point-in-time khác giữ nguyên.
- Manifest phải đóng gói tất cả giá trị hiệu dụng: fee entry/exit, entry/exit slippage, spread giữ cố định, funding baseline, fill model version và trial lineage. `CostModel` đối chiếu các cost fields của nó; evaluator kiểm tra riêng `spread_rate` với `PaperExecutionPolicy.spread_rate`. Derived cost-model version/content hash phải xác định từ scenario values, không sinh ngẫu nhiên.
- Nếu fill policy hiện tại có spread khác 0, thiếu lineage spread, cost assumptions không khớp, hoặc planner/gateway không thể xác minh cùng rate thì từ chối trial trước khi tạo report/outcome.

### Funding và spread: giới hạn chủ động

- **Funding tiếp tục unsupported trong WP này.** Không thay `adverse_funding_per_unit` để giả lập funding đã realized. Nếu chưa có event trong `ReplayEvaluationContext`, evaluator không thể bảo đảm event time, availability, amount theo vị thế/contract multiplier, long/short sign, idempotency, và funding cost được cộng đúng một lần vào `PaperPosition`/`EvaluationTrade`. BASE runtime sensitivity chỉ nhận funding assumption bằng 0; nonzero funding override fail-closed.
- **Spread stress tiếp tục unsupported trong WP này.** `PaperExecutionPolicy` áp spread vào fill, nhưng Planner/`CostModel` chưa tính spread trong entry/exit risk budget và `PaperFill.slippage_cost` gộp spread với slippage. Giữ spread bằng 0 trong scenario fixture này. Một WBS riêng phải thêm spread vào cost/risk contract và quyết định accounting/report phân biệt spread trước khi mở biến thể spread.
- Funding chỉ mở lại khi có WBS riêng cho `FundingEvent` point-in-time trong replay, áp dụng event theo lịch và vị thế qua gateway, signed amount, idempotency/event lineage, persist và report comparison. Không làm trong task hiện tại.

## WBS và điều kiện nghiệm thu

| ID | Công việc | File/bàn giao dự kiến | Điều kiện hoàn tất |
|---|---|---|---|
| SL1 | TDD contract cho slippage hai chiều và funding/spread fail-closed. | `tests/unit/test_runtime_replay_evaluator.py`, `tests/unit/test_trade_planner.py`, `tests/unit/test_paper_execution.py` | Test mới RED trước source. BASE/FEE hiện hữu tiếp tục hợp lệ; SLIPPAGE chỉ nhận cặp entry/exit rate bằng nhau; đơn phương, không hữu hạn, âm, >=1, key lạ, funding nonzero, spread nonzero/không ghim hoặc context-policy mismatch bị từ chối trước report/outcome. |
| SL2 | Mô hình hóa exit slippage trong Planner mà không đếm trùng. | `domain/risk/planner.py`, `domain/risk/sizing.py` nếu cần, `tests/unit/test_trade_planner.py`, `tests/unit/test_risk_sizing.py` | `CostModel` mang `exit_slippage_rate`; sizing tính adverse exit slippage từ stop reference * rate * multiplier. Entry rate tiếp tục nằm trong entry price; không cộng lại cùng entry cost vào `slippage_stress_per_unit`. BASE và stress có expected-loss/quantity cap đúng, stress không thể nâng quantity so BASE. |
| SL3 | Nối manifest, sensitivity validator, Planner và PAPER policy. | `application/runtime_replay_evaluator.py`, `application/backtest_sensitivity.py`, `tests/unit/test_runtime_replay_evaluator.py`, `tests/unit/test_backtest_sensitivity.py`, `tests/fixtures/runtime_replay.py` | SLIPPAGE factory tạo evaluator/gateway/context riêng. Manifest fee/slippage/funding fields khớp `CostModel`; `spread_rate` khớp riêng `PaperExecutionPolicy`; policy `slippage_rate` khớp entry/exit, `spread_rate` pinned bằng 0; actual entry/exit fills chịu đúng adverse rate. `EvaluationTrade.slippage` bằng tổng fill slippage và report net chỉ trừ một lần. Không report khi mismatch. |
| SL4 | Giữ lineage/persistence/report canonical. | `adapters/database/repositories/backtest_outcomes.py` (chỉ nếu cần), `tests/unit/test_backtest_outcomes_repository.py` | `cost_assumptions` mới lưu trong canonical manifest và pair overrides trong canonical scenario JSON; hashes/trial IDs thay đổi khi rate thay đổi và ổn định khi replay cùng inputs. Persist report chứa slippage totals và canonical outcome; load lại rows tái tạo cùng outcome hash; gross − fees − funding − slippage = net theo contract hiện hành. Retry nguyên payload idempotent; đổi rate dưới cùng trial key conflict. Không có schema migration nếu JSONB/contract hiện tại đủ; nếu cần cột mới, WBS phải tách migration RLS/immutable/role và upgrade/downgrade trước implementation. |
| SL5 | Compose E2E trên PostgreSQL/repository thật. | `tests/e2e/test_backtest_outcomes_persistence.py` | Chạy BASE + FEE + SLIPPAGE qua `RuntimeReplayEvaluator` thật, mỗi run có trade đóng qua PAPER gateway/monitor; fee stress tăng phí, slippage stress làm actual entry/exit fill cost tăng và net PnL đổi theo outcome; expected-risk/quantity behavior khớp Planner. Append report/scenario/outcome trong transaction thật; query `BacktestReportRecord`/scenario/outcome; xác nhận manifest/scenario/report/outcome hashes và cost totals; replay lần hai khớp; retry, payload conflict, cross-tenant RLS, immutability và `alembic check` + downgrade/upgrade đạt. Dùng shared content-addressed image; teardown không còn project container/volume. |
| SL6 | Quality và handoff. | WBS WP02F, test evidence | Chạy unit liên quan, Ruff check/format, `.venv/Scripts/mypy.exe src`, `git diff --check`; Compose chỉ chạy khi được cấp slot độc quyền. Ghi chính xác unsupported funding/spread và risk progression còn mở; không commit trong WBS này. |

## Thiết kế test bắt buộc

1. `test_runtime_sensitivity_applies_slippage_override_to_planner_and_entry_exit_fills`: BASE so với SLIPPAGE cùng observations; rate đã pin vào plan/gateway; actual fill slippage tăng ở cả entry và exit; realized report slippage khớp hai fill; net PnL giảm theo cost; không trừ estimated plan cost thêm lần nữa.
2. `test_planner_reserves_exit_slippage_without_double_counting_entry`: entry price đã bao gồm entry adverse move; `slippage_stress_per_unit` chỉ thêm expected exit adverse amount; estimated loss không vượt risk budget và stressed quantity không cao hơn BASE.
3. `test_runtime_sensitivity_rejects_unaligned_slippage_or_unmodeled_costs`: chỉ một phía đổi, entry/exit rate khác nhau, policy mismatch, spread khác 0, funding nonzero, key lạ hoặc rate không hợp lệ đều fail-closed trước report/outcome.
4. `test_sensitivity_slippage_manifest_scenario_and_outcome_are_replayable`: semantic duplicate/ordering deterministic; changed rate thay manifest/trial/scenario hashes; exact same workspace/data/settings/rates replay ra hash/outcomes giống nhau.
5. Compose E2E: persist source evaluator’s BASE/FEE/SLIPPAGE report/outcomes; re-query persisted report payload and canonical trades to verify exact fee/slippage/funding/net totals and lineage; then repeat to verify deterministic hash and idempotent retry.

## Ngoài phạm vi

- Funding rate ingestion, funding schedule/event replay, payment calculation, sign policy, event idempotency và funding report attribution.
- Spread sensitivity hoặc tách riêng spread khỏi slippage trong realized report.
- Multi-trade sensitivity comparison chưa có trong fixture này; mỗi scenario dùng một H03 trade. Reducer multi-observation/account progression được kiểm tra riêng trong RP4; mark-to-market và daily/rolling risk-window semantics chưa có.
- Benchmark profitability, strategy ranking, automatic parameter selection/promotion hoặc live execution.
- Migration mới nếu không cần thay đổi schema hiện có.

## Evidence implementation hiện tại

| WBS | Trạng thái | Evidence / giới hạn |
|---|---|---|
| SL1 | Hoàn tất ở unit | TDD đã có test exit slippage không đếm trùng entry, runtime fill hai chiều, và từ chối cost override lệch cặp, spread/funding stress, key lạ, rate không hợp lệ. |
| SL2 | Hoàn tất ở unit | `CostModel` có `exit_slippage_rate`; Planner đưa `stop * exit_rate * contract_multiplier` vào sizing. Test xác nhận entry price đã bao gồm entry slippage, expected loss chỉ cộng phần exit và quantity không tăng. |
| SL3 | Hoàn tất ở unit | Runtime evaluator khớp manifest với CostModel/PAPER policy, bắt buộc spread 0 và slippage entry/exit đối xứng; H03 fixture dựng CostModel version từ assumptions chuẩn hóa, gateway/provider riêng theo trial. Fill entry có slip và tổng position slippage lớn hơn entry fill, chứng minh close fill cũng chịu slip. |
| SL4 | Hoàn tất theo contract JSON hiện có | Không cần migration. Scenario overrides, manifest assumptions, report JSON và trade outcome đã được kiểm tra qua repository hiện có. E2E source bổ sung đọc lại report/scenario/outcome và thử retry SLIPPAGE. |
| SL5 | Đạt | Final full `tests/e2e`: **13 passed / 381.82s**; BASE/FEE/SLIPPAGE/PARAMETER, persisted reports/outcomes, replay hash, retry/conflict, RLS/immutability và migration checks. Teardown xóa E2E project tồn dư và giữ `crypidian-postgres-1` dev-owned. |
| SL6 | Đạt; WP-02 gate hoàn tất | Unit full **520 passed**; Ruff check đạt; format **227 files already formatted**; mypy `src` thành công trên 94 files; Alembic single head `b2e7a9d4c631`. Independent review: không P0/P1/source P2; documentation-only P2 đã được xử lý. Reducer progression được kiểm tra riêng qua RP4; mark-to-market và risk-window semantics vẫn là giới hạn. |

Các quyết định giữ nguyên: funding và spread stress bị từ chối; đây không phải mô hình funding event hay spread accounting. SL5 đạt trên Compose; không mở rộng kết luận sang funding/spread.

## Gate trước khi đóng Task 9 Step 3

Task 9 Step 3 đạt trong phạm vi fee + slippage đã hỗ trợ: final Compose suite trên tree cuối chứng minh runtime values, fills, risk sizing, persisted report/outcome và migration checks. **Spread và funding chưa hỗ trợ**; cần WBS riêng nếu yêu cầu thực thi các stress đó. Không diễn giải việc từ chối stress là đã thực thi stress thành công.
