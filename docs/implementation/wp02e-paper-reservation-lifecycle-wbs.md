# WBS — Vòng đời reservation theo PAPER ledger

**Mục tiêu:** đồng bộ reservation với risk đang chờ, vị thế PAPER đã mở và vị thế đã đóng; ngăn reservation bị giải phóng khi kết quả chưa rõ, tránh tính trùng risk đang chờ và exposure của trade. Đây là slice hẹp của WP-02E, không bật worker/runtime tự khởi động và không tuyên bố WP-02 hoàn tất.

**Phạm vi code:** `PaperLifecycleCoordinator`, `RiskReservationRepository`, unit tests của hai đường này và một Compose probe trong stack hiện tại. Không sửa `app.py`, runtime settings/bootstrap, hoặc runtime replay evaluator.

## Ngữ nghĩa trạng thái

| Trạng thái/trạng thái ledger | Reservation | Tác động tới hạn mức |
|---|---|---|
| Risk được duyệt, chưa tạo intent | `RESERVED` | Tính vào pending risk/notional/margin. |
| Intent đã tạo nhưng chưa có fill | `RESERVED` | Vẫn tính pending; thời hạn hết không tự giải phóng khi intent đã tồn tại. |
| Kết quả PAPER unknown | `UNKNOWN` | Giữ nguyên toàn bộ quota cho tới reconcile; không tự expire. |
| Có fill đầu tiên và `TradeRecord OPEN` đã được persist | `TRANSFERRED` với `OPEN_RISK_RECORDED` | Không còn tính pending; vị thế OPEN là nguồn exposure duy nhất. Cùng transaction với fill, position event và trade OPEN. |
| Intent `CANCELLED`/`EXPIRED`, chưa từng fill, không có trade | `RELEASED` với `CANCELLED_NO_FILL`/`EXPIRED_NO_FILL` | Không còn tính pending. Cùng transaction với OrderIntent/ExchangeOrder terminal state. |
| Trade đóng | Reservation tiếp tục `TRANSFERRED` | Reservation không tái kích hoạt; trade CLOSED không còn thuộc open exposure. Risk request sau close được phép nếu mọi hard cap khác thỏa. |
| Cancel sau partial fill | `TRANSFERRED` | Phần đã khớp được biểu diễn bằng trade OPEN; phần chưa khớp không tạo exposure thêm. Không release reservation sang trạng thái terminal giả định “không có vị thế”. |

`UNKNOWN` chỉ được giải phóng/chuyển trạng thái qua reconcile có chứng cứ. Đường lifecycle production chưa có coordinator API cho pause/resume, cancel/UNKNOWN độc lập, funding và reduce-only thì phải fail-closed; gateway mutation trực tiếp không được coi là đã bền vững.

## WBS và điều kiện nghiệm thu

| ID | Đầu việc | Bàn giao | Điều kiện nghiệm thu |
|---|---|---|---|
| RL1 | Kiểm tra contract hiện tại và lập invariant trạng thái trước code. | WBS này. | Có semantics cho RESERVED, UNKNOWN, TRANSFERRED, RELEASED, partial fill và trade close; xác định transaction boundary. |
| RL2 | Viết unit tests đỏ cho transfer/release và fail-closed conflict. | `tests/unit/test_risk_reservation_repository.py`, `tests/unit/test_paper_lifecycle.py`, `tests/unit/test_paper_lifecycle_checkpoints.py`. | Fill đầu tiên tạo OPEN trade thì reservation chuyển TRANSFERRED đúng một lần; sai account/intent/trade/status hoặc gọi lại mâu thuẫn bị từ chối; terminal no-fill release; UNKNOWN không expiry/release; rollback không publish gateway. |
| RL3 | Thêm transition repository có kiểm tra ledger và khóa account. | `RiskReservationRepository`. | Chỉ chuyển khi workspace/account/intent/reservation khớp; kiểm `TradeRecord OPEN` + ENTRY fill tồn tại; cập nhật cùng UnitOfWork, không tự commit. |
| RL4 | Cập nhật OrderIntent/ExchangeOrder terminal state bền vững trong ledger. | `PaperLedgerRepository.record_order_transition`. | Chỉ nhận trạng thái hỗ trợ; UNKNOWN không bị coi terminal; terminal no-fill chỉ khi quantity fill bằng 0; retry cùng status idempotent, trạng thái mâu thuẫn fail-closed. |
| RL5 | Thêm reason release chính xác và migration constraint. | `RiskReservationRecord`, domain enum và Alembic migration. | DB chấp nhận `CANCELLED_NO_FILL`/`EXPIRED_NO_FILL`, tiếp tục từ một migration head hiện tại và downgrade khôi phục constraint cũ. |
| RL6 | Gọi transition từ PAPER coordinator trong transaction chứa fill/trade/checkpoint. | `PaperLifecycleCoordinator`. | Lần đầu có fill atomically transfer; CANCELLED/EXPIRED với 0 fill atomically release; PARTIAL→CANCELLED giữ TRANSFERRED; UNKNOWN giữ quota; checkpoint-backed state chỉ publish sau commit. |
| RL7 | Chứng minh không tính trùng open risk. | Unit/repository tests. | Active pending totals chỉ gồm RESERVED/UNKNOWN; TRANSFERRED không cộng thêm vào pending vì OPEN trade được tính trong open exposure; close trade loại exposure nhưng reservation vẫn terminal. |
| RL8 | Compose PostgreSQL E2E cho đóng trade và request tiếp theo. | Mở rộng probe H03 hiện có trong `tests/e2e/test_compose_boundary.py`. | Qua DB runtime role: risk request đầu tạo pending reservation; PAPER fill mở trade chuyển TRANSFERRED; đóng trade; xác nhận open exposure=0/pending không còn, reservation không tái kích hoạt; risk request hợp lệ tiếp theo trong cùng cap thành công. Kiểm tra transaction/retry và RLS scope. |
| RL9 | Kiểm tra các mutation gateway ngoài coordinator. | Tests/docs. | Pause/resume, cancel/UNKNOWN, funding, reduce-only không được coi là bền vững nếu thiếu API coordinator/checkpoint tương ứng; đường gọi trực tiếp phải bị ngăn hoặc ghi rõ không được dùng production path. Không mở rộng phạm vi bằng cách sửa `app.py` hay bootstrap. |
| RL10 | Static/regression và ghi nhớ triển khai. | Ruff, format, mypy, unit, Compose E2E, diff-check; ghi chú để root hợp nhất memory/status/plan. | Tất cả test liên quan đạt; không có test Compose chạy song song; WBS còn mở cho mutation chưa hỗ trợ/runtime wiring. |

## Phụ thuộc và transaction

1. `RiskReservationRepository` chuyển trạng thái trong session/UoW caller-owned, dưới cùng account lock dùng khi reserve/fill/intent. Không commit nội bộ.
2. Khi entry fill được append, `OrderIntent` và `ExchangeOrder` được cập nhật bởi ledger; `TradeRecord OPEN` tồn tại trong transaction. Sau đó reservation được kiểm tra ledger và chuyển `TRANSFERRED`; checkpoint snapshot cùng transaction. Chỉ `after_commit` mới thay gateway RAM.
3. Khi event terminal no-fill là `CANCELLED`/`EXPIRED`, repository xác nhận intent chưa có ENTRY fill/trade và chuyển `RELEASED` trong transaction với status update/order/checkpoint. Với partial fill, reservation phải ở `TRANSFERRED`.
4. `RiskReservationRepository._HELD_STATUSES` giữ `RESERVED` và `UNKNOWN`; `TRANSFERRED` được tính vào pending totals bằng 0. Open risk được cấp từ open trade/exposure read-model, không cộng giá trị reservation lần nữa. Sau close, trade không còn open exposure và reservation vẫn `TRANSFERRED`.
5. E2E phải dùng Compose image/project hiện có và xếp lịch với root; không chạy đồng thời Compose test khác.

## Chưa được nghiệm thu

- Runtime startup/dispatch, readiness gate và tenant-scoped bootstrap thuộc [WBS runtime bootstrap](./wp02e-runtime-bootstrap-wbs.md).
- Reconcile cho order unknown bên ngoài PAPER, funding lifecycle, pause/resume bền vững, independent reduce-only lifecycle và đầy đủ cancel/expiry orchestration cần coordinator API/checkpoint riêng; không khẳng định đã hỗ trợ chỉ vì gateway domain có mutation thuần.
- Kiểm thử này không gửi lệnh sàn, không sử dụng credential và không phải bằng chứng profitability.

## Trạng thái thực hiện và bằng chứng

| Hạng mục | Trạng thái | Bằng chứng |
|---|---|---|
| RL1–RL7: invariant, transition, terminal ledger state, transfer/release và pending/open exposure | Hoàn tất trong slice hiện tại | `tests/unit/test_trade_ledger.py`, `tests/unit/test_paper_reservation_lifecycle.py`, `tests/unit/test_risk_reservation_repository.py`, `tests/unit/test_risk_reservations.py`, `tests/unit/test_paper_lifecycle.py`, `tests/unit/test_paper_lifecycle_checkpoints.py`, `tests/unit/test_paper_execution_recovery.py`, `tests/unit/test_paper_recovery_verifier.py`, `tests/unit/test_paper_checkpoint_repository.py`: **79 passed**. |
| RL8: H03 PostgreSQL Compose E2E sau khi đóng trade | Đạt | `$env:DOCKER_CONTEXT='default'; .venv/Scripts/python.exe -m pytest tests/e2e/test_compose_boundary.py::test_paper_stack_starts_and_keeps_database_and_object_store_data -q -s`: **1 passed in 100.00s**. Probe kiểm tra TRANSFERRED, pending totals bằng 0, không còn OPEN trade và request risk tiếp theo reserve thành công. |
| RL8: căn chỉnh dữ liệu strategy fixture | Hoàn tất | Compose lần trước phát hiện hash h03 hard-code không còn khớp source rule. Fixture nay lấy `content_hash` trực tiếp từ `StrategyRegistry`; lượt kiểm tra kế tiếp đạt. |
| RL9: gateway mutations ngoài coordinator | Chưa hoàn tất; fail-closed/giới hạn phạm vi | Pause/resume, reconcile độc lập cho UNKNOWN/cancel, funding và reduce-only chưa có persisted coordinator/checkpoint API đầy đủ. Không tuyên bố đã bền vững. |
| RL10: static/regression | Đạt cho slice hiện tại | Ruff, `ruff format --check`, mypy trên ba module nguồn, `alembic heads` (một head `b2e7a9d4c631`) và `git diff --check` đạt. Diff check chỉ có cảnh báo chuẩn hóa LF/CRLF ở working tree dùng chung. |
| Ghi nhớ triển khai cấp WP-02 | Chờ root hợp nhất | Theo điều phối, chưa sửa `implementation-memory.md`, `project-status.md` hoặc WP-02 master plan trong nhánh công việc này. |

Compose luôn chạy tuần tự. Sau lượt E2E cuối, test đã gọi `docker compose down --volumes --remove-orphans`; kiểm tra `docker ps -a` không còn container `crypidian-e2e-*` (container nền `crypidian-postgres-1` vẫn healthy).
