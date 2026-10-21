# Plan 05 — Research ingestion và phân tích thông tin

**Mục tiêu:** thu thập market context/news/trending/ý kiến công khai theo quyền, thời gian point-in-time và provenance; không dùng nội dung bên ngoài như lệnh giao dịch.
**Phụ thuộc:** [Plan 01](./01-foundation-database.md), market snapshot/event schema từ [Plan 02](./02-trading-core-paper.md).
**Stories:** US-09…13, US-23.
**Tài liệu:** [research source policy](../research/execution-and-ingestion-2026.md), [macro data](../research/macro-market-data.md), [directory trader/source](../research/trader-source-directory.md), [glossary](../research/glossary-vi.md).

## WP-05A — Source registry, quyền truy cập và quota

1. Với mỗi provider ghi source ID, publisher/account, endpoint, phương thức truy cập, authentication, quota, rate policy, terms/robots, retention, attribution và người phụ trách.
2. Chia trạng thái `verified`, `discovered`, `blocked`, `rights_pending`; mặc định không train từ source chưa có quyền rõ ràng.
3. Thêm connector interface theo provider; không cào diện rộng, không vượt paywall/đăng nhập/giới hạn truy cập.
4. Thực thi quota/backoff, cursor, retry policy và lịch poll; lưu metadata skip/error để người vận hành thấy nguyên nhân.
5. Version policy thay đổi; khi source bị thu hồi, dừng fetch và xác định artifact/dataset cần loại theo retention/license.

**Nghiệm thu:** fetch chỉ bắt đầu sau policy pass; nguồn pending không ghi body hoặc vào training set; quota 429 có backoff; mọi item có source provenance.

## WP-05B — Normalize, deduplicate và sửa nội dung

1. Tạo `SourceItem` với canonical URL, hash, publisher, language, `published_at`, `edited_at`, `observed_at`, `available_at`, source group và revision.
2. Phân biệt bài gốc, repost, quote, correction và deleted/updated item; tránh đếm bản sao thành nhiều ý kiến.
3. Với nội dung bị sửa, giữ version/hash/observed time cũ; replay chỉ thấy bản đã có tại thời điểm đó.
4. Giữ raw body chỉ khi quyền/retention cho phép; nếu không, lưu excerpt hợp lệ/reference/hash.
5. Tách metadata/event quan trọng khỏi blob; blob có checksum, storage URI và policy retention.

**Nghiệm thu:** canonical duplicates liên kết cùng nhóm; revision không ghi đè nội dung lịch sử; truy vấn as-of chỉ trả phiên bản đã khả dụng.

## WP-05C — News và macro catalyst

1. Ưu tiên nguồn chính thức/được cấp quyền: lịch công bố, cơ quan thống kê, ngân hàng trung ương, sàn, project/company announcement và RSS/API đã duyệt.
2. Extract loại catalyst, entity/ticker, vùng ảnh hưởng, thời hạn, polarity/stance nếu phù hợp, evidence offsets, confidence, model/version và trạng thái human review.
3. Lưu `event_at` riêng với `available_at`; correction/revision và timezone có trường riêng.
4. Không gửi raw article prompt vô hạn; dùng trích đoạn cần thiết, giới hạn token, bảo vệ khỏi prompt injection, schema validate và provenance.
5. Tạo calendar/event risk context cho strategy; event context chỉ làm feature/circuit policy, không ra order trực tiếp.

**Nghiệm thu:** event có thể truy ngược tới nguồn/evidence; late event không xuất hiện trong replay trước lúc bot nhận; model extraction lỗi thì đánh `unknown/review`, không bịa giá trị.

## WP-05D — Trending/attention

1. Định nghĩa input được phép: volume/price/OI/funding (exchange), query interest nếu API chính thức khả dụng, public social metrics nếu điều khoản cho phép.
2. Chuẩn hóa theo symbol, nguồn, seasonality, time window và baseline; tạo score kèm source coverage, freshness, missingness và formula version.
3. Deduplicate nội dung theo canonical item/source cluster; hạn chế bot/repost hoặc burst giả nếu có tín hiệu đủ.
4. Với nguồn thiếu hoặc down, đánh dấu `degraded/unknown`; không diễn giải số 0 là không có attention.
5. Tách “đang được chú ý” khỏi “hướng giá” và “chất lượng tín hiệu”; không gửi order từ trending score.

**Nghiệm thu:** score tái lập từ snapshot/version; low coverage bị gắn cờ; source outage không tạo false negative/positive mặc định.

## WP-05E — Trader profile/opinion và reviewer feedback

1. Nạp registry hiện có nhưng giữ nhãn `verified_channel` khác `verified_performance`; không gọi danh sách candidate là top trader có lợi nhuận.
2. Ghi public channel URL, nội dung tham chiếu, stance, asset, horizon, thời điểm dự báo, thời gian công bố/thu thập và evidence.
3. Chuẩn hóa dự báo có thể chấm được: hướng, time horizon, invalidation/target nếu công khai; không suy ngược dự báo từ nội dung sau kết quả.
4. Lưu bất đồng/coverage thay vì trung bình hóa thành consensus giả; mọi performance score phải có methodology và out-of-sample window.
5. Reviewer correction append-only, phân biệt đánh giá chất lượng luận điểm với PnL.

**Nghiệm thu:** mỗi opinion có attribution/link và timestamp; chưa audited performance luôn hiển thị cảnh báo; reviewer correction không xóa source extraction ban đầu.

## WP-05F — Rights, privacy và kiểm soát dữ liệu

Viết source runbook tiếng Việt cho thêm nguồn, kiểm tra terms, quota, retention/deletion, takedown, xuất dữ liệu và tái tạo dataset manifest. Thêm bảng lưu nội dung được phép, thời hạn xóa và thông tin người duyệt.

**Kiểm tra khi triển khai:** fixtures cho license gate, dedup/revision, timestamp as-of, prompt injection và source outage; rà soát quyền theo từng provider trước khi bật connector.
