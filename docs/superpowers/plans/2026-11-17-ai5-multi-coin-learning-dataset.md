# AI-5 (CRY-236) — Dataset học nhiều coin ("trial cohort")

Ngày: 17/11/2026 · Owner chọn hướng "Mở rộng dataset nhiều coin" · Phối hợp: phiên WP-06.

## Bối cảnh

- `learning_split_candidate.py:218-228` buộc mọi outcome ghim **một** replay bundle và **một** trial.
- Bundle replay chỉ có một symbol, nên một dataset bị giới hạn ở một bot trên một coin.
- Đo thật trên BTC: H03 15m 180 ngày cho 190 lệnh; H03 1h 365 ngày cho 138 lệnh. Tập TEST vì vậy chỉ khoảng vài chục mẫu, trong khi policy `wp06-scorer-test-v1` yêu cầu ≥1.000 mẫu và ≥100 mẫu mỗi lớp.

## Quyết định thiết kế

**Đơn vị dataset = trial cohort:** N trial BASE, mỗi trial là một job replay của một coin.

- Mọi trial trong cohort phải cùng `strategy_release`, profile, khung vào lệnh và schema feature.
- "Một trial = một bundle = một candidate-universe proof" giữ nguyên.
- Job replay, worker, outcome, opportunity, rights gate và gateway mở/tiêu thụ TEST không đổi.

**Nguyên tắc tương thích:** khi N = 1, mọi hash mới phải ra **đúng giá trị hiện tại**: `query_hash`, `candidate_id`, `bundle_set_hash`, `source_scope_hash`. Candidate, proof, receipt và report cũ vẫn hợp lệ, test cũ không phải đổi kỳ vọng hash.

### Python (≈12 file)

1. **`learning_split_candidate`** (`build_split_candidate`, `stage`, `seal`) nhận `trials: tuple[TrialSource]` thay cho một trial.
   - `TrialSource` gồm trial id, proof id, proof và bundle.
   - Ordinal kiểm theo từng trial. Membership và số đếm so theo từng proof.
   - `query_hash` và `candidate_id` (uuid5) dùng danh sách sắp theo trial id; N = 1 giữ dạng cũ.
   - Mỗi outcome ghim bundle của trial mình. Tập bundle của sample phải bằng tập `bundle_hash` trong lineage.
   - Lineage là hợp các lineage từng trial, sắp theo child.
2. **`domain/learning/dataset.py:338-345`:** manifest v5 cho phép N bundle, miễn tập bundle của sample bằng tập bundle của lineage. Không cần v6 vì N = 1 cho hash không đổi.
3. **`learning_fold_samples.py:252-299`** và **`replay_training_rights.py:233-262`:** nhóm theo bundle, gọi `authorize` một lần mỗi bundle, gộp receipt.
4. **Proof TEST (`learning_scorer_test_opportunity_proofs.py`):** nhóm hàng TEST theo trial, chạy inventory, expected events và gap từng trial, rồi cộng entries và số đếm.
   - Cột scalar `bundle_hash` mang nghĩa `bundle_set_hash`: sha256 canonical của danh sách bundle đã sắp; N = 1 thì là chính bundle hash.
   - `source_scope_hash` phủ danh sách `{bundle_hash, trial_id, identity_scope_hash}` đã sắp; N = 1 giữ dạng cũ.
5. **CLI và schema admin:** `learning_split_candidate stage --trial-record-id` cho phép lặp lại nhiều lần.
6. **CLI mới cho production:**
   - `learning_scorer_test_proof` (role `crypidian_candidate_builder`) ghi proof TEST, vì hiện chỉ probe làm được bước này.
   - Script tạo bundle ghim nguồn (v4) từ `market_bar` và lưu manifest/bundle bằng role `admin_web`.
   - Script xuất artifact gốc (role admin) để truyền vào CLI `learning_evaluation`.

### DB (2 migration, nối sau `d4f6a8b0c2e4`)

- **M1:**
  - Bảng con `learning_split_candidate_source(candidate_id, trial_record_id, proof_record_id, proof_hash, bundle_hash)`: FK, unique `(candidate_id, trial)`, RLS và grant như bảng cha, append-only. Candidate giữ `trial_record_id` scalar làm "neo" (trial id nhỏ nhất) nên FK và unique cũ vẫn dùng được.
  - Bảng con `learning_scorer_test_proof_bundle(proof_id, bundle_hash, trial_record_id, run_id, identity_scope_hash)`: digest của các dòng phải bằng `bundle_hash` của proof.
  - Hàm mới `wp06_scorer_manifest_replay_bundles` trả `text[]`. Hàm scalar cũ giữ nguyên.
- **M2:** viết lại các hàm có ghim hash theo mẫu `_rewrite` của `c2e4a6b8d0f1`/`d4f6a8b0c2e4`: đo preimage/postimage trên DB thật, kèm unit test hai chiều.
  - `wp06_check_replay_training_scope`: bỏ `MIXED_BUNDLE`/`MIXED_RUN`; kiểm từng bundle/run một lần; lọc lineage theo bundle của sample.
  - `wp06_recheck_scorer_current_rights`: lặp các dòng proof-bundle; `json_agg ORDER BY child_dataset_hash` qua mọi bundle; so tập bundle của manifest với tập bundle của proof.
  - `wp06_claim_learning_training_lineage`: trial của outcome phải thuộc `learning_split_candidate_source` hoặc là trial neo.
  - Lock trial theo thứ tự trial id đã sắp để tránh deadlock.

### Nhãn dải R (làm cùng đợt)

- Outcome replay thêm khóa tùy chọn `risk_amount` (1R).
- Label version mới: lớp 0 khi |net R| ≤ 0,1R.
- Đổi 5 chỗ dẫn xuất nhãn: `labels.py`, `learning_split_candidate.py` ×2, `learning_fold_samples.py`, `learning_scorer_test_access.py`, SQL `c7e9a1b3d5f8`.
- Migration M3 riêng.

## Kiểm thử

- **Unit:**
  - N = 1 cho hash không đổi, kiểm bằng golden của test hiện có.
  - N = 2 chấp nhận được.
  - Từ chối cohort lệch release, profile hoặc schema.
  - Từ chối tập bundle của sample khác tập bundle của lineage.
  - Hai chiều preimage/postimage cho mọi hàm viết lại.
- **E2E:**
  - Probe pinned có thêm biến thể hai coin: replay → split → seal → train → proof TEST → evaluate → recheck.
  - Toàn bộ 22 file E2E WP-06 phải đạt.

## Chạy thật (sau khi code đạt)

1. Dựng bundle v4 cho 20 coin; H03 15m dùng 180 ngày, hoặc 1h dùng 365 ngày.
2. Replay `TRAINING_BASE` mỗi coin một job.
3. Stage cohort với split theo thời gian, rồi owner duyệt trên Admin (cần MFA).
4. Seal → train → proof TEST → evaluate.
5. Báo cáo TEST đúng như kết quả, kể cả khi trượt ngưỡng. Dừng trước SHADOW.
