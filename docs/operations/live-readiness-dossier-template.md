# Mẫu Hồ sơ Readiness G0–G5 & Release Gate (OPS07F / Task 10)

**Schema Version:** `ops07f-dossier-v1`
**Nguyên tắc:** Mọi quyết định mở Live đều phải đi qua server-side gate (`evaluate_live_readiness_gate`). Tài liệu/UI không thể tự mở dispatch. Ngay cả khi G0–G5 đều đạt, **Live vẫn giữ khóa (`live_dispatch_allowed = false`)** cho tới khi có hồ sơ và chữ ký **G6 (Canary Live)** tách biệt.

---

## 1. Thông tin định danh & Scope bất biến (gắn vào `dossier_hash`)

| Trường | Giá trị (Owner/Release cung cấp — không tự bịa) |
| --- | --- |
| `schema_version` | `ops07f-dossier-v1` |
| `build_hash` | `<git-commit-sha + image-@sha256:...>` |
| `config_hash` | `<sha256-published-server-config>` |
| `risk_profile_hash` | `<sha256-SignedLiveRiskProfile-CRY-464>` |
| `venue` / `environment` | `binance_usdm` / `LIVE` |
| `account_id` | `[BLOCKER-OWNER — ID tài khoản Live đã duyệt]` |
| `symbols` / `strategy_release_ids` | `[BLOCKER-OWNER — danh sách symbol & strategy pin]` |
| `required_demo_days` / `required_demo_samples` | `[BLOCKER-RISK-OWNER — ngưỡng Demo tối thiểu đã ký]` |
| `created_at` / `expires_at` | `<UTC ISO-8601>` |

---

## 2. Trạng thái bằng chứng theo từng cổng độc lập (G0–G5)

> Không coi trạng thái ticket "Done" là bằng chứng thay cho kết quả test/evidence thực tế có `evidence_hash` và thời hạn `expires_at`.

| Cổng | Phạm vi kiểm chứng | Ticket liên quan | Trạng thái hiện tại (25/11/2026) | Chủ thể xử lý (Actionable Owner) |
| --- | --- | --- | --- | --- |
| **G0** | Điều kiện tài khoản Owner + Ma trận quyền & maker-checker | `CRY-459`, `CRY-460` | **BLOCKED / OPEN** (chờ xác nhận tài khoản & maker-checker coverage) | Owner |
| **G1** | Tách mode Live + Ranh giới secret/key/egress gateway-only | `CRY-461`, `CRY-462`, `CRY-148` | **BLOCKED / OPEN** (chờ WP-03 Demo boundary + secret manager thật) | Security / Platform Owner |
| **G2** | Preflight chỉ đọc & đối soát trạng thái sàn | `CRY-463` | **CODE & E2E VERIFIED** (16 passed/18.75s, commit `75ca5dee`) | Execution / Ops Engineer |
| **G3** | Semantics equity, cửa sổ lỗ & hard limits có chữ ký | `CRY-464` | **CODE & E2E VERIFIED / CHỜ KÝ NGƯỠNG THẬT** (11 passed/15.45s, commit `a6ea483f`) | Risk Owner |
| **G4** | Production Compose overlay, Backup/Restore RPO/RTO & Demo prospective window/sample | `CRY-465`, `CRY-106`, `CRY-17` | **OVERLAY & DRILL VERIFIED / CHỜ HOST & DEMO THẬT** (4 passed/17.26s, commit `73dfa8eb`) | Platform & Risk Owner |
| **G5** | Ma trận E2E fail-closed, Security review & Runbook/On-Call/Drill | `CRY-466`, `CRY-467`, `CRY-271` | **FAULT MATRIX DONE (57 passed) / CHỜ ON-CALL & SECURITY SIGN-OFF** | Ops & Security Reviewer |

---

## 3. Chữ ký phê duyệt độc lập (Tách `CODE_RELEASE` và `RISK_CAPITAL`)

- **Phê duyệt phát hành mã nguồn (`CODE_RELEASE`):**
  - `actor_id` / `membership_id` / `role`: `[Chờ Human Release Reviewer / Owner ký]`
  - `approved_dossier_hash`: `<khớp chính xác dossier.compute_dossier_hash()>`
- **Phê duyệt rủi ro & vốn (`RISK_CAPITAL`):**
  - `actor_id` / `membership_id` / `role`: `[Chờ Independent Human Risk Approver — bắt buộc khác người lập & khác người duyệt CODE_RELEASE]`
  - `approved_dossier_hash`: `<khớp chính xác dossier.compute_dossier_hash()>`
- **Cấm tuyệt đối:**
  - Tự duyệt (`actor_id == executor_actor_id` hoặc `code_approver == risk_approver`).
  - Bất kỳ `AI_AGENT` nào tạo chữ ký, nới cap hoặc chuyển trạng thái gate (`AI_APPROVAL_FORBIDDEN`).
  - Dùng role đã bị thu hồi (`role_revoked = true`).
