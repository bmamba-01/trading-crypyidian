# Thiết kế WP06 Task3: candidate manifest và duyệt split trong admin

**Ngày:** 2026-11-08 <br>
**Trạng thái:** được triển khai theo ủy quyền tiếp tục hoàn tất WP06. <br>
**Thiết kế nền:** [Thiết kế hoàn tất WP06](2026-11-06-wp06-completion-design.md)

## Vấn đề đã xác nhận

`LearningSplitApprovalService` hiện chỉ nhận `DatasetManifest` và các cửa sổ fold do caller cung cấp. Không có production path tạo một manifest đã được verifier xác nhận nhưng chưa seal; `DatasetManifestBuilder` chưa có DB verifier cụ thể. Candidate-universe proof chỉ lưu metadata/digest và chủ ý không cấp quyền TRAINING. Vì vậy admin không được gửi canonical manifest/sample từ trình duyệt vào service duyệt.

## Luồng đã chọn

1. Principal riêng `crypidian_candidate_builder` nhận workspace/trial UUID và split configuration có giới hạn; nó lấy trial/outcome từ DB, chạy lại candidate-universe proof và current TRAINING rights gate trong cùng transaction, rồi dựng canonical manifest cùng fold plan từ dữ liệu authoritative. Caller không gửi sample, feature, label, count hoặc membership.
2. Builder lưu candidate bất biến ở bảng riêng trước khi seal. Canonical manifest và fold membership chỉ builder được đọc. Admin chỉ được SELECT cột preview an toàn: candidate ID, manifest/proof/fold hashes, thời gian, symbol, counts, phiên bản policy và fold windows/counts đã làm sạch. Không có label value/hash, sample ID, outcome UUID, feature hoặc holdout payload trong preview.
3. Trang `/admin/learning/splits` liệt kê candidate trong workspace, hiển thị cấu hình và số lượng theo từng fold, rồi cho reviewer có `LEARNING_SPLIT_APPROVE` cùng MFA gần đây duyệt bằng candidate ID, lý do và `Idempotency-Key`. Request không nhận actor/workspace/hash/window từ browser. Service nạp metadata candidate authoritative, kiểm tra manifest chưa seal, tạo approval append-only gắn đúng manifest/policy/fold hash và actor membership từ Principal.
4. Lệnh builder `seal` chỉ chấp nhận candidate đã được duyệt. Nó tái dựng canonical manifest/folds, xác minh lại proof và quyền TRAINING hiện hành dưới source lock, kiểm tra approval exact-match, rồi trong cùng transaction ghi manifest và normalized fold payload/profiles. Payload chỉ do builder truy cập; admin không có quyền đọc nó.

## Lựa chọn fold và holdout

- Split configuration được cung cấp cho builder bằng JSON cục bộ có schema nghiêm ngặt, không qua HTTP; builder kiểm mọi window với toàn bộ sample authoritative rồi lưu bản canonical để reviewer duyệt đúng nội dung đó.
- Mọi sample thuộc `DEVELOPMENT` hoặc `HOLDOUT` do builder phân loại theo cutoff cấu hình; final holdout không nằm trong bất kỳ fold plan hay normalized dataset nào.
- Fold plan được dựng bằng `build_manifest_fold_plan`; hash lưu trong candidate là hash duy nhất được approval cho phép seal.
- Không cho approval nếu candidate không có train/validation/test đủ điều kiện theo policy hoặc chứa source/lineage không thể recheck.

## Giao dịch, quyền và retry

- Staging và seal chạy dưới role builder, workspace RLS, transaction `READ COMMITTED`, advisory source lock theo thứ tự nguồn và khóa đúng trial. Lease quyền phải còn hiệu lực ở bước cuối trước commit.
- Approval là transaction admin riêng, có CSRF, MFA gần đây, quyền riêng, unique idempotency và không lộ canonical payload.
- Retry stage cùng scope trả lại candidate có canonical hash trùng; cùng key nhưng scope khác bị conflict. Retry seal chỉ thành công nếu manifest, approval và normalized payload đã lưu khớp chính xác.
- Cross-workspace, manifest hash/fold hash giả, stale/thiếu quyền, candidate đã seal, approval thiếu hoặc khác hash đều fail-closed.
- Downgrade migration bị chặn khi có candidate/approval/payload.

## Kiểm chứng giao diện

Playwright phải thao tác flow API bằng fixture builder có dữ liệu candidate đã stage, kiểm tiếng Việt/Be Vietnam Pro, responsive desktop 1280px và mobile 390px, no horizontal overflow, control >=44px, CSRF/MFA/idempotency, response chỉ metadata và không có label/feature/sample/holdout. PostgreSQL Compose phải kiểm builder/admin column grants, RLS, immutable candidate/approval, exact seal, quyền hiện hành, retry, cross-workspace và không đọc payload bằng admin.

## Ngoài phạm vi

Không bật scorer, không thêm order/LIVE, không tự chọn policy bằng AI. Policy mới phải được version hóa và bổ sung EN→VI glossary trước khi dùng.
