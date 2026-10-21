# Plan 07 — Operations, resilience và release gate

**Mục tiêu:** làm hệ thống quan sát được, có thể phục hồi và có playbook vận hành trước Demo dài hạn; Live vẫn cần phê duyệt riêng.
**Phụ thuộc:** các plan 01–06.
**Stories:** US-24, US-28, US-29.
**Tài liệu:** [technical operations](../architecture/technical-design.md#security-operations), [circuit breaker sequence](../product/sequence-diagrams.md#sd-13--circuit-breaker-và-recovery), [glossary](../research/glossary-vi.md).

## WP-07A — Metrics, logs, tracing và dashboard sức khỏe

1. Tạo correlation từ decision cycle → candidate → risk plan → intent → exchange order/event → fill → trade.
2. Metrics: feed freshness/gap, outbox lag/retry, UNKNOWN orders, unprotected exposure, account snapshot age, risk utilization, API latency/error, model abstain/OOD, drift và Telegram delivery.
3. Structured logs/event names tiếng Anh để máy đọc; dashboard, alert text, runbook và metric help tiếng Việt.
4. Redact token/API key/session/body; cấu hình sampling không làm mất audit ledger.
5. Thiết lập retention theo data class và alert severity; không dùng log thay cho ledger.

**Nghiệm thu:** truy từ alert về entity/correlation; secret canary không xuất hiện trong log/trace; metric không cardinality-explode theo raw content.

## WP-07B — Circuit breaker và incident drills

1. Chốt ngưỡng feed stale, loss/drawdown, API error, queue lag, clock skew, missing protection và discrepancy.
2. Khi kích hoạt, đóng entry gate atomically, lưu reason/version/metric evidence, phát alert; giữ protective exit/reconcile hoạt động.
3. Resume chỉ sau cooldown, health pass, reconciliation sạch và role/approval hợp lệ; mọi blocker hiển thị tiếng Việt.
4. Viết runbook theo triệu chứng → kiểm tra → safe action → recovery → evidence; không hướng dẫn paste secret vào chat/log.
5. Thực tập outage DB/control plane, Binance REST/WS, source provider, Telegram, model provider và secret rotation.

**Nghiệm thu:** từng drill có người phụ trách, thời gian phát hiện/phục hồi, event log và follow-up; không có tình huống breaker bị clear bởi health ping đơn lẻ.

## WP-07C — Backup, restore, migration và disaster recovery

1. Thiết lập encrypted backup PostgreSQL và object-store manifest; nêu RPO/RTO dự kiến và người có quyền restore.
2. Chạy restore vào môi trường cô lập; kiểm tra order/trade/audit, config versions, registry artifacts và checksum.
3. Kiểm tra backup không chứa plain secret; secret provider có recovery/rotation procedure độc lập.
4. Kiểm thử forward/backward compatibility migration, lock contention, partition creation/retention và disk exhaustion alert.
5. Viết restore runbook tiếng Việt theo checklist có xác nhận từng bước.

**Nghiệm thu:** restore drill thành công, có số liệu RPO/RTO thực đo; phát hiện corruption/checksum mismatch; không đè môi trường production khi phục hồi thử.

## WP-07D — Supply chain và security review

1. Khóa dependency, image digest, Python base image; tạo SBOM và quy trình cập nhật bản vá.
2. Rà soát dependency license, vulnerability, secret scanning, API authz và SSRF/HTML injection trong admin/source content.
3. Kiểm tra network egress: trainer/admin/source workers không có đường tới endpoint giao dịch; gateway chỉ ra host sàn cần thiết.
4. Kiểm thử session/MFA/CSRF/RBAC/rate limit, webhook signature, tenant isolation, log redaction và data retention.
5. Viết threat model/risk acceptance bằng tiếng Việt, owner và hạn xử lý rõ.

**Nghiệm thu:** không có critical/high finding chưa xử lý/được chấp nhận rõ; API key no-withdrawal và IP restriction được xác nhận nếu nền tảng hỗ trợ.

## WP-07E — PAPER/Demo readiness và Live gate

1. Hoàn tất AC P0; đối chiếu sequence, ledger và behavior trong paper replay.
2. Chạy demo prospective có fee/funding, stream reconnect, partial fill, stop reject, UNKNOWN timeout, rate limit và restart.
3. Đánh giá khác biệt paper/demo và mô hình slippage/fill; không chọn giai đoạn thuận lợi làm holdout mới.
4. Hoàn thành source rights, retention, backup/restore, on-call, alerts, key revoke, rollback và kill-switch drills.
5. Tạo readiness dossier: version, evidence, unresolved risks, approvals, drawdown/exposure caps và emergency contact.
6. LIVE vẫn disabled cho tới khi owner/risk approver ký gate riêng; không gộp phê duyệt code vào phê duyệt vốn.

**Nghiệm thu:** Demo readiness có bằng chứng prospective và ký xác nhận; mọi gate fail block LIVE; sau phê duyệt, live rollout bắt đầu ở canary/quota cực thấp theo quyết định risk owner và có kill switch đã diễn tập.
