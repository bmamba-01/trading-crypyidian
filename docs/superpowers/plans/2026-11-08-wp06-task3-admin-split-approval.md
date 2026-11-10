# Kế hoạch WP06 Task3: staging candidate và admin duyệt split

**Thiết kế:** [Task3 admin split approval](../specs/2026-11-08-wp06-task3-admin-split-approval-design.md) <br>
**WBS:** [WP06 learning](2026-11-01-wp06-learning-wbs.md) <br>
**Điều kiện:** giữ mọi thay đổi WP03/WP05/WP06/WP07 khác trong shared worktree; không stage/commit/push/reset/clean.

## Giao diện và ranh giới

- Builder staging nhận `workspace_id`, `trial_record_id` và schema split JSON đã giới hạn; đọc authoritative replay rows và rights, không nhận sample/feature/label/count.
- Candidate là immutable/private payload gồm canonical manifest + canonical fold plan, kèm preview metadata không nhạy cảm.
- Admin route chỉ xem preview và gửi candidate UUID + reason; Actor/workspace/MFA lấy từ Principal. Approval append-only trước khi final manifest được seal.
- Builder seal exact candidate sau approval, recheck proof/current rights, append manifest và normalized fold payload/profile atomic.
- UI/API: `/admin/learning/splits`, `/api/v1/learning-splits/candidates`, `POST /api/v1/learning-splits/candidates/{candidate_id}/approve`.

## RED → GREEN → REFACTOR

1. Thêm unit contract cho split config/candidate preview; candidate builder dựng manifest từ outcome rows authoritative, bỏ trade thiếu entry provenance, tính label hash canonical và chia development/holdout bằng cutoff.
2. Thêm repository/model/migration candidate với append-only trigger, workspace RLS, builder full access và admin column-only SELECT. Kiểm downgrade guard và một head.
3. Thêm stage service/CLI có transaction, candidate proof + current TRAINING rights, manifest/fold-plan canonical hash, idempotent retry và JSON split config strict.
4. Thêm admin approval service/API/schemas với permission/MFA/CSRF/idempotency, candidate metadata lookup, candidate-not-sealed check, exact hashes và append-only approval.
5. Thêm seal command kiểm candidate/approval exact match, recheck proof/current rights, dựng normalized dataset authoritative, ghi manifest + payload + profiles trong cùng transaction.
6. Thêm trang tiếng Việt responsive và navigation; Playwright approve/reject invalid body, desktop/mobile screenshots và no payload assertion.
7. Compose PostgreSQL E2E cho stage → preview → approve → seal; kiểm RLS/grants/immutability/idempotency/current rights/cross-workspace/payload boundary.
8. Chạy focused unit/UI/Compose E2E, Ruff/format/mypy/Alembic/diff-check; cập nhật WBS, plan, module, project-status, glossary nếu có thuật ngữ mới và implementation-memory; đọc lại memory sau vòng.

## Trọng điểm review

- Không cho `crypidian_admin_web` SELECT canonical candidate, feature, label, holdout hoặc sample identity.
- Manifest/fold plan phải được tái dựng bằng DB data authoritative; hash từ browser không thể cấp quyền.
- Current TRAINING rights lock phải giữ đến lúc transaction seal commit; replay research receipt không thay quyền TRAINING.
- Holdout tuyệt đối không được chuẩn hóa hay xuất hiện trong admin preview.
- Chỉ thêm quyền DB/secret cần thiết cho builder; không mount candidate-builder secret vào admin-web.
