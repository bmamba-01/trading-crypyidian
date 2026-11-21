# WP06F — Operator block cho entry PAPER

**Ngày:** 2026-11-17
**Trạng thái:** Người dùng đã duyệt thiết kế ngày 2026-11-18; implementation plan đã lập và đang được rà soát trước khi code.
**Itsaplan:** CRY-440, con của CRY-323.

## Mục tiêu

Cho phép người có thẩm quyền chặn một entry PAPER mà scorer đã phê duyệt, bằng thao tác chủ động có MFA và lý do. Hành động bổ sung veto cho entry mới, không sửa sự thật scorer đã ghi và không thể dùng để mở quyền hoặc bỏ qua RiskEngine.

## Bối cảnh đã kiểm tra

- BotDecisionStatus có CANDIDATE, VETO, ABSTAIN. Scorer APPROVE trong PAPER_GATE được ghi thành score event riêng; không nên giả lập thao tác thủ công thành scorer VETO.
- PaperRuntimeApplicationService.evaluate_and_submit() mở transaction entry, lấy workspace settings lock L1, kiểm control/fence rồi xác thực BotDecision trước khi dựng intent và reservation.
- Incident entry hold đã có pattern owner/risk-admin, CSRF, MFA TOTP gần đây tối đa 15 phút, lý do, audit và khóa L1; ledger đó có phạm vi workspace rộng, không phù hợp để biểu diễn chặn một quyết định scorer cụ thể.
- Protective exits không đi qua entry gate.

## Phương án

1. **Ledger bất biến cho từng BotDecision PAPER — đề xuất.** Lưu thao tác riêng, ràng buộc workspace/bot/decision/hash/candidate và model target; runtime kiểm tra trong transaction trước mọi side effect entry. Không có RELEASE cho cùng quyết định.
2. **Workspace kill switch hoặc incident hold.** Chặn được entry nhưng phạm vi quá rộng, làm lẫn operator override với vận hành hoặc data-quality incident.
3. **Sửa BotDecision/score event thành VETO.** Không chọn vì làm sai nguồn gốc quyết định, khiến báo cáo scorer mất phân biệt giữa AI và con người.

Người dùng chọn mục tiêu “cho phép chặn thêm lệnh vào PAPER” ngày 2026-11-17 và duyệt phương án 1 cùng phạm vi một BotDecision ngày 2026-11-18.

## Hành vi đề xuất

- Chỉ owner hoặc risk-admin có quyền chuyên biệt được tạo action BLOCK_PAPER_ENTRY; yêu cầu CSRF, TOTP MFA còn hiệu lực theo RECENT_MFA_LIFETIME 15 phút và lý do 1–500 ký tự.
- Chỉ cho block score event có APPROVE, mode PAPER_GATE, cùng workspace/bot/candidate và target scorer còn active. Không áp cho VETO, ABSTAIN, TEST, SHADOW hoặc quyết định không gắn score event hợp lệ.
- Block gắn đúng một BotDecision; không tồn tại trạng thái bật/tắt kéo dài cho bot hoặc chiến lược. Không có RELEASE/reversal; muốn đánh giá lại cần candidate/decision mới.
- Ghi ledger append-only với actor, MFA timestamp, lý do, thời điểm, idempotency key, BotDecision ID/hash/candidate và tuple target/pointer revision. Actor chỉ lấy từ Principal đã xác thực.
- Không đổi BotDecision hay score event gốc. UI hiển thị quyết định scorer và kết quả riêng “Entry PAPER bị chặn thủ công”.
- PAPER entry và thao tác block dùng cùng workspace L1 lock rồi khóa BotDecision theo cùng thứ tự. Nếu block lấy khóa trước, runtime thấy block và từ chối trước khi ghi reservation/OrderIntent/trade. Nếu entry commit trước, thao tác block bị từ chối là quá muộn; không hủy intent đã tạo.
- Khi block thắng, chỉ từ chối NEW_ENTRY; RiskEngine vẫn bắt buộc với entry khác. Không ảnh hưởng SHADOW, vị thế đang mở, protective exit, close/flatten hoặc cơ chế venue.
- Thao tác và lịch sử xuất hiện trong trang scorer hiện hữu, tiếng Việt, responsive cho mobile/desktop.

## Ngoài phạm vi

- Không tự động block theo drift/metric.
- Không cho phép owner/risk-admin nâng VETO/ABSTAIN thành APPROVE hoặc sửa nội dung scorer.
- Không chặn bot/strategy/workspace lâu dài; không thay thế incident entry hold.
- Không mở SHADOW/PAPER_GATE nếu deployment chưa đủ điều kiện; tính năng không tự bật scorer.
- Không hủy OrderIntent hoặc đổi vị thế sau khi entry transaction đã thắng race.

## Tiêu chí nghiệm thu

1. Unit kiểm role/permission, MFA hết hạn, CSRF, reason rỗng/quá dài, stale target, sai workspace/bot/candidate, quyết định không APPROVE và idempotency.
2. PostgreSQL E2E kiểm hai thứ tự L1/decision lock; block thắng thì không có reservation/OrderIntent/trade; entry thắng thì block bị từ chối vì quá muộn; không có partial write.
3. Kiểm trực tiếp ledger append-only/RLS, actor và target revision do server nạp lại, không lấy từ payload caller.
4. Playwright desktop/mobile kiểm chỉ hiện nút với PAPER_GATE APPROVE đủ điều kiện, nhập lý do/MFA, kết quả block, lịch sử/audit và không tràn ngang.
5. Regression chứng minh SHADOW, VETO/ABSTAIN, RiskEngine, close/protective exit và incident hold giữ nguyên hành vi.

## File dự kiến

- Domain/application service và repository/migration ledger riêng.
- Policy/permission và router/API có CSRF/MFA.
- Trang scorer history hiện hữu và form confirmation tiếng Việt.
- Unit, PostgreSQL Compose E2E và Playwright desktop/mobile.

## Quyết định còn để implementer

- Phạm vi một BotDecision là đề xuất; không tạo block kéo dài cho bot.
- Không sửa score event; UI trình bày quyết định gốc cạnh trạng thái áp dụng.
- Block là cuối cho decision cụ thể; race được phân xử bằng L1 và row lock, không bằng thời gian từ client.
- Chặn không hủy ý định PAPER đã được ghi; request sau đó trả lỗi đã quá muộn.

## Nguồn nội bộ

- docs/superpowers/plans/2026-11-17-wp06f-signal-action-latency.md
- docs/superpowers/specs/2026-11-08-wp06-task7-calibrated-scorer-design.md
- docs/superpowers/specs/2026-11-12-wp06-learning-incident-entry-hold-design.md
- src/trading_bot/application/paper_runtime_application.py
- src/trading_bot/application/learning_incident_entry_hold.py
