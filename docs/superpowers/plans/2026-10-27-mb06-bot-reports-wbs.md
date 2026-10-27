# WBS MB-06 — Báo cáo theo bot và tổng hợp workspace tường minh

**Ngày:** 2026-10-27 · **Gói:** Task 7 / MB-06 trong [WBS Multi-Bot](2026-10-24-multibot-implementation-plan.md) · **Nền:** `dev` @ `1b583f7` (MB-01..MB-03, MB-05 Part A+B). **Chế độ:** chỉ PAPER, không gọi sàn.

**Căn cứ:** [hợp đồng Multi-Bot §7](../../architecture/decisions/2026-10-24-multibot-contracts.md) (bot ID không phải bằng chứng ủy quyền; service kiểm `BotPermission` theo hành động; RLS là lớp bổ sung), Global Constraints + Review Focus 5 của plan, [nghiên cứu bảo mật](../../research/security-admin-bot-2026.md) SEC-18/SEC-19 (BOLA → 404 như nhau), [QA dataset](../../testing/qa-test-data.md) (vai trò/grant chuẩn).

## 1. Mục tiêu

1. Báo cáo một bot (`scope=bot`) chỉ đọc ledger PAPER append-only của đúng `(workspace_id, bot_id, account_id)` và đối soát chính xác (Decimal) với `trade.net_pnl`.
2. Báo cáo tổng hợp workspace (`scope=workspace`) chỉ khi người gọi khai báo tường minh **và** có quyền `bot:report:read` ở cấp workspace; tổng = tổng các báo cáo bot = tổng ledger độc lập.
3. Ủy quyền cấp đối tượng: bot không tồn tại, bot workspace khác và bot chưa được grant trả **cùng** 404 không phân biệt.
4. Mỗi báo cáo mang lineage cấu hình/strategy/model (MB-02 effective config, MB-03 decision) và hash SHA-256 tất định.

## 2. Ngoài phạm vi

- Không sửa/tính lại/ghi bất kỳ hàng ledger nào (`trade`, `trade_cost`, `fill`, `order_intent`, `bot_decision`); không migration (không cần cột/quyền mới: `crypidian_admin_web` đã có SELECT qua `crypidian_app` + RLS `bot_member_isolation`).
- Không quy đổi FX: tổng hợp theo từng quote asset để giữ đối soát chính xác. Quy đổi USDT cho báo cáo là việc sau (cần conversion point-in-time của MB-04).
- Không PnL chưa thực hiện (UPNL), không equity curve theo mark; drawdown chỉ trên PnL ròng đã thực hiện.
- Không UI (MB-07), không Telegram (MB-07a), không sửa `bots_router.py`/`bot_service.py` (MB-07a), không đụng file MB-04.

## 3. Ruling

| # | Ruling |
| --- | --- |
| R1 | Cửa sổ thời gian **nửa mở** `[start, end)` trên `trade.closed_at`; `start`, `end` bắt buộc có timezone, `start < end`, độ dài ≤ 366 ngày. Trade đóng đúng lúc `end` thuộc cửa sổ kế tiếp. |
| R2 | Trade "mở tại `end`" = `opened_at < end` và (`closed_at IS NULL` hoặc `closed_at >= end`); chỉ liệt kê định danh (không PnL) để hash ổn định khi trade đóng về sau. |
| R3 | Số liệu lấy nguyên văn từ cột ledger (`gross_pnl`, `fee_total`, `funding_total`, `slippage_total`, `total_cost`, `net_pnl`); báo cáo chỉ cộng Decimal. Đồng thời kiểm chéo: tổng `trade_cost` theo loại của từng trade phải bằng cột tổng tương ứng, `total_cost = fee + funding + slippage`, `net = gross − total_cost`; lệch → `LEDGER_RECONCILIATION_FAILED` (409), không trả số. |
| R4 | Tổng hợp theo quote asset (`totals[quote_asset]`); không cộng chéo tiền tệ. |
| R5 | Drawdown tối đa trên chuỗi PnL ròng tích lũy đã thực hiện, bắt đầu 0 tại `start`, thứ tự `(closed_at, trade_id)`; lineage drawdown = trade đỉnh và trade đáy. Workspace tính lại trên chuỗi gộp mọi bot cùng thứ tự. |
| R6 | Lineage mỗi trade: cột trade (settings version, strategy release id/hash, feature snapshot, candidate, risk decision/reservation), `trade.lineage` (plan hash, paper execution profile hash), `order_intent.evidence` (bot config version/hash, effective config hash, settings hash), `bot_decision` cùng bot + candidate (decision id/hash, model release). Thiếu nguồn (đường root-fallback cũ) → giá trị null, không đoán. |
| R7 | Hash báo cáo = SHA-256 JSON canonical (khóa sắp xếp, không khoảng trắng, Decimal chuẩn hóa không số 0 thừa, thời gian UTC ISO-8601); không chứa thời điểm sinh báo cáo hay tên bot (nhãn đổi được) → cùng ledger + cùng truy vấn cho cùng hash. |
| R8 | `BotReportQuery.scope` mặc định `bot`; `get_bot_report` bắt buộc `bot_id`; `get_workspace_report` chỉ chạy khi `scope=workspace` tường minh và `bot_id` trống. API `GET /api/v1/reports` bắt buộc tham số `scope`; thiếu scope, `scope=bot` thiếu `bot_id`, `scope=workspace` kèm `bot_id` → 422. Không có đường nào mặc định gộp bot. |
| R9 | Báo cáo bot cần `BotGrantRepository.require(..., BotPermission.REPORT_READ)` (role workspace-wide đi thẳng; operator/analyst cần grant `bot:report:read` còn hiệu lực). Thiếu quyền, bot không tồn tại, workspace khác (kể cả `workspace_id` của truy vấn khác workspace phiên) → cùng `REPORT_NOT_FOUND` 404, cùng thông điệp. |
| R10 | Tổng hợp workspace cần `bot:report:read` từ role workspace-wide (`owner`/`strategy_admin`/`risk_admin`); grant từng bot không đủ (tổng hợp bao cả bot tương lai/chưa grant) → 403 `WORKSPACE_REPORT_FORBIDDEN` (không lộ đối tượng). Vẫn gọi `require` cho từng bot trong vòng lặp. |
| R11 | Workspace luôn lấy từ phiên; mọi SELECT của một báo cáo (bot hoặc tổng hợp, gồm cả tổng ledger đối soát R12) chạy trong **một** transaction `UnitOfWork(workspace, bot, membership, snapshot_read_only=True)`: UoW chạy `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY` ngay sau `begin()`, **trước** `SELECT set_config` (PostgreSQL chỉ cho đổi isolation trước truy vấn đầu). Một snapshot duy nhất → trade đóng giữa hai câu không làm lệch đối soát (409 giả) hay hash (trade lọt giữa danh sách đóng/mở); mọi ghi bị DB từ chối. Mặc định UoW (`snapshot_read_only=False`) không đổi cho caller khác. *(Fix round 1: thay `SET TRANSACTION READ ONLY` sau truy vấn đầu dưới READ COMMITTED.)* |
| R12 | Tổng hợp workspace kiểm chéo với tổng ledger độc lập không lọc bot (`count`, `sum(net_pnl)` theo quote asset trong cửa sổ); lệch → 409. |
| R13 | Giới hạn tài nguyên: ≤ 10.000 trade đóng mỗi bot trong một báo cáo (vượt → 422 `REPORT_TOO_LARGE`, thu hẹp cửa sổ); ≤ 1.000 trade mở tại `end` mỗi bot (vượt → 422 `REPORT_TOO_MANY_OPEN_TRADES`); ≤ 200 bot mỗi báo cáo tổng hợp (vượt → 422 `REPORT_TOO_MANY_BOTS`); truy vấn dùng `LIMIT cap+1`. Mode chỉ `PAPER`; `report_version` chỉ `bot-report.v1`. |
| R14 | Cộng/so sánh Decimal trong `localcontext(prec=76)` với bẫy `Inexact` (cột Numeric(38,18) có 38 chữ số, context mặc định prec 28 làm tròn âm thầm); phép tính không chính xác → `LEDGER_RECONCILIATION_FAILED`. *(Fix round 1.)* |

## 4. Dữ liệu và truy vấn

| Truy vấn (repository `BotReportRepository`, chỉ SELECT) | Bộ lọc tường minh | Index dùng |
| --- | --- | --- |
| Bot + account | `bot_instance.workspace_id`, `id` | PK/`uq_bot_instance_scope` |
| Trade đóng trong cửa sổ | `workspace_id`, `bot_id`, `account_id`, `status='CLOSED'`, `closed_at ∈ [start,end)`, `ORDER BY closed_at, id`, `LIMIT n+1` | `ix_trade_bot_scope` |
| Trade mở tại `end` | `workspace_id`, `bot_id`, `account_id`, `opened_at < end`, `closed_at IS NULL OR closed_at >= end` | `ix_trade_bot_scope` |
| Chi phí | `trade_cost.workspace_id`, `bot_id`, `account_id`, `trade_id IN (...)` | `ix_trade_cost_bot_scope` |
| Evidence intent | `order_intent.workspace_id`, `bot_id`, `account_id`, `id IN (...)` | `ix_order_intent_bot_scope` |
| Decision MB-03 | `bot_decision.workspace_id`, `bot_id`, `status='CANDIDATE'`, `candidate_id IN (...)` | `ix_bot_decision_bot_scope` |
| Tổng ledger workspace (R12) | `trade.workspace_id`, `status='CLOSED'`, cửa sổ; `GROUP BY quote_asset` | workspace index |

RLS: `crypidian_admin_web` thấy hàng khi membership ACTIVE có role workspace-wide hoặc grant bot còn hiệu lực; `app.workspace_id` khác → 0 hàng. Ứng dụng vẫn lọc `workspace_id`/`bot_id`/`account_id` và builder kiểm lại từng hàng (sai scope → 409).

## 5. Phân rã công việc và nghiệm thu

| ID | Việc | Nghiệm thu |
| --- | --- | --- |
| W1 | Domain thuần `domain/portfolio/reporting.py`: kiểu dữ liệu ledger, builder bot/workspace, drawdown, canonical Decimal/JSON, hash | Unit: đếm trade, PnL/fee/funding/slippage, drawdown + lineage, biên `[start,end)`, lọc bot, trade sai scope bị từ chối, cost lệch → lỗi, hash tất định/độc lập thứ tự đầu vào |
| W2 | Repository chỉ đọc `adapters/database/repositories/bot_reports.py` | Unit: câu SQL có bộ lọc workspace/bot/account/cửa sổ; E2E PostgreSQL |
| W3 | Service `application/bot_reporting.py` (`BotReportQuery`, `get_bot_report`, `get_workspace_report`) | Unit ma trận quyền: role × bot có/không grant → 200/404 như nhau; thiếu bot → 422; aggregate thiếu scope/quyền; workspace khác → 404; read-only được bật trước khi đọc |
| W4 | Router `admin/bot_reports_router.py` + đăng ký tối thiểu trong `app.py` | Unit API: 422 scope, 404 thông điệp giống nhau, 403 aggregate, `Cache-Control: no-store`, 503 khi chưa có service |
| W5 | E2E Compose `tests/e2e/test_multibot_reports.py` + probe stdin | Hai bot QA A/B, trade tạo bằng `PaperLedgerRepository` thật; báo cáo A/B chỉ chứa trade của mình; workspace = A + B = SQL ledger độc lập; analyst A 200 / B 404; operator A 404; owner/strategy/risk aggregate 200; analyst/operator aggregate 403; owner W2 → 404; RLS W2 thấy 0 hàng |
| W6 | Gates + report | Unit đầy đủ, ruff/format, mypy, `git diff --check`, một alembic head (`e1f3a5c7b9d2`, không migration mới) |

## 6. Ánh xạ bảo mật

| Yêu cầu | Biện pháp | Test |
| --- | --- | --- |
| SEC-19 BOLA | `require(REPORT_READ)` + lookup theo workspace phiên; 404 đồng nhất | Unit service/API, E2E analyst B, operator A, owner W2 |
| SEC-18 deny-by-default | Route GET dùng `current_identity`; service kiểm quyền trước truy vấn ledger | Unit API |
| Review Focus 5 | Không mặc định gộp; aggregate cần scope + role workspace | Unit + E2E |
| API4:2023 tài nguyên | Giới hạn cửa sổ 366 ngày, 10.000 trade/bot | Unit |
| Toàn vẹn ledger | Transaction read-only, đối soát cost/net, kiểm chéo tổng ledger | Unit + E2E |

## 7. Nguồn (kiểm tra 2026-10-27)

- PostgreSQL 18 [SET TRANSACTION](https://www.postgresql.org/docs/current/sql-set-transaction.html): transaction read-only cấm INSERT/UPDATE/DELETE/MERGE/COPY FROM trên bảng không tạm; isolation không đổi được sau truy vấn đầu tiên của transaction → đặt `REPEATABLE READ, READ ONLY` ngay sau BEGIN (R11); REPEATABLE READ: mọi câu thấy cùng snapshot chụp ở truy vấn đầu.
- PostgreSQL 18 [Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) (đã ghi trong memory MB-01/02).
- OWASP [API1:2023 Broken Object Level Authorization](https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/): kiểm quyền trên bản ghi ở mọi hàm nhận ID từ client.
- OWASP [API4:2023 Unrestricted Resource Consumption](https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/): giới hạn kích thước truy vấn/kết quả.
- OWASP [Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html): deny by default, kiểm quyền mọi request.
- Python [`decimal`](https://docs.python.org/3/library/decimal.html): cộng Decimal chính xác, `normalize()` cho biểu diễn canonical.

## 8. Thuật ngữ mới (đề xuất cho glossary — controller cập nhật)

- **Workspace aggregate report — báo cáo tổng hợp workspace:** báo cáo cộng số liệu mọi bot trong workspace, chỉ khi yêu cầu tường minh và có quyền cấp workspace.
- **Realized drawdown — mức sụt giảm đã thực hiện:** khoảng giảm lớn nhất từ đỉnh xuống đáy của PnL ròng tích lũy từ các trade đã đóng.
- **Half-open interval — khoảng nửa mở `[start, end)`:** gồm `start`, không gồm `end`; tránh đếm trùng ở biên giữa hai kỳ báo cáo.
- **Report hash — mã băm báo cáo:** SHA-256 trên nội dung canonical để chứng minh cùng dữ liệu cho cùng báo cáo.
