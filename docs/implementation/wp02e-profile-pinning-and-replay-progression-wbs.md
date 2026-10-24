# WBS bổ sung WP-02E/F — Ghim policy vị thế và tiến triển tài khoản replay

**Ngày:** 2026-10-24  
**Trạng thái:** Hoàn tất trong phạm vi PP2/RP4/PP6; WP-02 gate đã nghiệm thu sau final full suite và independent review.  
**Phụ thuộc:** WP-02G root WorkspaceSettings/Admin API, WP-02E PAPER bootstrap/monitor/ledger, WP-02F `RuntimeReplayEvaluator` và sensitivity.

## Mục tiêu

Đảm bảo publish settings mới không làm mất khả năng theo dõi/thoát vị thế PAPER đang mở; đồng thời replay nhiều observation phải tự quản lý reservation, equity, exposure và giới hạn vị thế theo những giao dịch PAPER đã phát sinh. Admin tiếp tục cấu hình tại root WorkspaceSettings qua API hiện có.

## Quyết định thiết kế cần giữ

- Mỗi vị thế/order PAPER đang hoạt động phải giữ nguyên execution profile và strategy/risk lineage lúc mở; monitor/exit dùng policy đã ghim cho vị thế đó.
- Settings publish mới áp dụng cho quyết định entry mới sau thời điểm publish. Thay đổi strategy/features không được làm đổi profile fill của vị thế cũ.
- Mọi publication và thao tác runtime có thể tạo intent phải tuần tự hóa theo workspace trong cùng transaction/lock contract. Không cho phép khoảng hở giữa kiểm tra active state và publish.
- Replay là một mô phỏng tuần tự cô lập theo trial. Sau fill/close, replay tự cập nhật state tài khoản từ ledger PAPER nội bộ; provider chỉ cấp market/execution facts và cấu hình point-in-time, không cấp lại vốn/exposure tùy ý mỗi observation.
- Replay chưa được gọi là parity đầy đủ nếu không có bằng chứng multi-trade, realized PnL, exposure đồng thời, reservation và max-position behavior.

## WBS và tiêu chí nghiệm thu

| ID | Công việc | Bàn giao | Điều kiện nghiệm thu |
|---|---|---|---|
| PP1 | Định nghĩa execution-profile identity độc lập với hash/version settings không liên quan đến execution. | ADR ngắn trong tài liệu thiết kế, profile hash tests. | Strategy/features-only publish không đổi fill policy identity; quote asset, fee schedule, fill model và starting-account bootstrap vẫn có identity/version rõ; không bỏ lineage settings khỏi decision/trade records. |
| PP2 | Ghim execution profile và strategy/risk lineage cho từng active order/position. | Runtime account/position model và restore contract. | Restart sau settings publish khôi phục active position; monitor/exit dùng profile cũ; entry mới dùng settings mới; mismatch/thiếu profile vẫn fail-closed. |
| PP3 | Tuần tự hóa publish với tạo intent/reservation theo workspace. | Repository/UoW lock contract hoặc DB advisory-lock helper dùng chung. | Hai transaction cạnh tranh publish và entry có thứ tự tuyến tính; không tồn tại intent mới dưới settings cũ sau khi publish commit; test concurrent trên PostgreSQL xác nhận. |
| PP4 | Viết unit tests RED cho profile drift, pinned exit, policy mutation và mọi race có thể tái hiện. | Unit regression tests. | Bao phủ strategy-only settings change khi vị thế OPEN, đổi fill policy khi OPEN, restart recovery, entry sau publish, rollback, và duplicate retry. |
| PP5 | Áp dụng `WorkspaceSettings.strategy.parameters` thật vào `StrategySelection` runtime. | Bộ chuyển settings→selection và unit tests. | Tham số H03 được chuẩn hóa theo khóa/kiểu/miền đã hỗ trợ; giá trị mới làm đổi hành vi rule và được ghim theo settings version; khóa/kiểu/miền không hỗ trợ fail-closed; strategy không có tham số không nhận cấu hình lạ. |
| RP1 | Tạo state reducer tuần tự cho replay account và reservations theo từng PAPER event. | `ReplayAccountState`/port thuần domain hoặc application. | Khởi đầu từ manifest/context; pending risk được reserve trước entry; fill chuyển pending exposure thành open exposure; close giải phóng exposure, ghi realized PnL/fees/funding/slippage và cập nhật equity; ID/số học Decimal xác định. |
| RP2 | Nối reducer với runtime evaluator và gateway PAPER. | Evaluator multi-observation. | Context provider không thể ghi đè account/exposure/reservation đã được reducer sở hữu; mỗi observation dùng state point-in-time đúng thời điểm; reservation bị giải phóng/cancel khi order không fill hoặc hết hạn theo contract. |
| RP3 | Thêm unit tests RED cho chuỗi giao dịch nhiều bước. | Unit test replay lifecycle. | Ít nhất 2 vị thế nối tiếp thể hiện equity sau realized PnL; 2 vị thế đồng thời làm tăng exposure/count; max positions/notional/risk chặn giao dịch vượt ngưỡng; reservation không bị tính hai lần; scenario mới dựng state mới và determinism giữ nguyên. |
| RP4 | Thêm Compose E2E cho persistence và transaction boundaries. | E2E PostgreSQL qua runtime/ledger/replay hiện hữu. | Hành trình multi-observation gồm entry, open exposure, close, equity update, entry thứ hai; DB ledger/report/outcomes khớp reducer; restart/replay hash ổn định; migration/RLS/retry liên quan đạt. |
| PP6 | Thêm Compose race thực giữa quyết định entry qua application service và publish settings. | Mở rộng H03 Compose boundary probe. | PostgreSQL test chứng minh nếu decision giành lock trước thì intent/version cũ commit trước publish; publisher bị chặn cho đến commit; nếu publish giành lock trước thì decision đọc version mới. Không tạo intent dưới settings cũ sau publish commit; cùng probe kiểm strategy-only publish khi intent đang hoạt động, restart recovery và monitor theo strategy release đã pin. |
| DOC | Đồng bộ WBS, master plan, project status, recovery/sensitivity docs và implementation memory. | Traceable status theo cùng working tree. | Không còn số test/ trạng thái mâu thuẫn; các giới hạn còn lại ghi rõ; memory được đọc lại và đối chiếu source/output. |
| GATE | Chạy đầy đủ gates sau cùng và review độc lập. | Evidence cuối WP-02. | Unit, Compose E2E, Ruff, format, mypy, `alembic check`, downgrade/upgrade, diff-check đạt; không có Compose artifact tồn; review không còn P1; chỉ sau đó mới đóng WP-02. |

## Trạng thái theo evidence — 2026-10-24

| Hạng mục | Trạng thái hiện tại | Evidence và giới hạn |
|---|---|---|
| PP1 / PP3 / PP4 | Hoàn tất ở source/unit | Execution profile identity được tách khỏi settings không liên quan tới fill; publication/runtime chia sẻ serialization contract; unit bao gồm drift, conflict, rollback/retry và race logic. Hai thứ tự cạnh tranh được kiểm tra trong PP6 Compose target. |
| PP2 | Đạt source/unit và Compose provider check | Profile và strategy/risk lineage được pin cho active order/position; **17 focused unit tests passed**. PP6 target Compose xác minh provider dùng đúng strategy/settings đã publish. |
| PP5 | Hoàn tất ở source/unit | `WorkspaceSettings.strategy.parameters` đi vào `StrategySelection` runtime; tham số H03 được validate, ảnh hưởng hành vi và pin theo settings version; unsupported values fail-closed. Đã nằm trong **520 unit tests passed** và PP2 focused tests. |
| RP1–RP3 | Hoàn tất ở source/unit | Reducer sở hữu account equity/exposure/reservation progression sau bootstrap; context provider chỉ cung cấp base state và market/execution facts. Regression group **30 passed** gồm chuỗi nhiều trade, exposure và risk limits. |
| RP4 | Đạt theo phạm vi được kiểm tra | PostgreSQL Compose target → **1 passed / 57.69s**; **30 regression unit tests passed**. Multi-observation entry → exposure → close → realized equity → entry tiếp theo, ledger/report/outcome và RLS/retry được đối chiếu. Hash determinism được xác nhận bằng replay lặp **không restart worker**; không tuyên bố restart-worker determinism. |
| PP6 | Đạt | Target E2E → **1 passed / 109.47s**. Bao phủ cả decision-lock-first/publish-waits và publish-first/decision-uses-new-settings; kiểm tra strategy-only publish với active state, lineage/profile pin và provider settings. PP2 pinned-provider scenario nằm trong H03 Compose probe này. |
| GATE | Đạt | Final full unit **520 passed**; final full E2E **13 passed / 381.82s**; Ruff/format/mypy/Alembic/migration checks và teardown đạt. Independent review: không còn P0/P1/source P2; documentation-only P2 được đóng trong lần cập nhật này. |

### Quality evidence hiện có

- Full unit: **520 passed**; RP4 regression: **30 passed**; PP2 focused: **17 passed**.
- Ruff check đạt; Ruff format: **227 files already formatted**; mypy `src`: thành công trên **94 files**.
- Alembic single head: `b2e7a9d4c631`.
- RP4 target Compose **1 passed / 57.69s**; PP6 target Compose **1 passed / 109.47s**; final full E2E **13 passed / 381.82s**. Teardown xóa một stale E2E project; giữ nguyên PostgreSQL dev container.

### Điều kiện đóng còn lại

1. Review đã xác nhận không còn finding P0/P1/source P2; documentation-only P2 được đóng tại lần reconcile này.
2. Chốt WP-02 complete trong phạm vi PAPER/replay hiện có; giữ các giới hạn mark-to-market/risk windows, dispatcher/venue và profitability claims.

## Trình tự thực hiện và phụ thuộc

1. PP1–PP3/PP6 là thay đổi xuyên settings, DB transaction và runtime; hoàn thành hợp đồng + test trước khi sửa production source.
2. PP5 nối root settings với rule runtime; dùng TDD và fail-closed cho mọi tham số không hỗ trợ.
3. RP1–RP3 dựa trên semantics PAPER đã ghim ở PP1–PP3; dùng TDD, không sửa acceptance bằng cách thu hẹp thành một trade/trial.
4. PP4/RP4/PP6 xác nhận ranh giới PostgreSQL/Compose sau khi unit đạt. Compose chạy tuần tự.
5. DOC/GATE chỉ chốt theo output của đúng working tree cuối.

## Ngoài phạm vi

- Binance private API/order submission, LIVE execution, UI admin WP-04.
- Tự tối ưu/promote strategy hoặc cho AI tự thay đổi risk limits.
- Đa bot; chỉ brainstorm kiến trúc sau nghiệm thu WP-02, chưa triển khai ở đây.

## Thuật ngữ EN→VI

- **Execution profile — hồ sơ thực thi:** bộ phiên bản phí, fill/slippage, latency và quote asset dùng để mô phỏng thực thi.
- **Position pinning — ghim cấu hình vị thế:** lưu lineage thực thi lúc mở để vị thế có thể được quản lý/thoát nhất quán sau khi cấu hình workspace đổi.
- **State reducer — bộ rút gọn trạng thái:** hàm tuần tự áp từng sự kiện đã xác nhận để tạo trạng thái tài khoản mới.
- **Linearizability — tính tuần tự hóa quan sát được:** các thao tác cạnh tranh có thể xem như xảy ra theo một thứ tự duy nhất.

