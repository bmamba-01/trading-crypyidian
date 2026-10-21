# Kết nối Binance và thu nhận nguồn research — cutoff 07/10/2026

Tài liệu bổ sung cho [thiết kế bot độc lập](../superpowers/specs/2026-10-07-independent-binance-futures-bot-design.md). Đây là kết quả đọc tài liệu nguồn và đề xuất connector; chưa kết nối tài khoản, gửi lệnh, mua API hoặc thu thập corpus hàng loạt.

## 1. Nguồn chuẩn cho execution

| Nguồn chính thức | Điểm cần dùng | Quyết định thiết kế của dự án |
| --- | --- | --- |
| [Binance Trade REST](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade) | Conditional orders có `/fapi/v1/algoOrder`; query phân biệt algo và actual order | Gateway lưu cả ID của intent, algo, actual order và fills. Capability test theo phiên bản adapter; không chỉ kiểm tra tên method CCXT |
| [Binance changelog](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log) | Thay đổi endpoint và schema qua thời gian | Lưu ngày đối chiếu tài liệu trong capability manifest; xem chi tiết thay đổi tại mục 14 của thiết kế |
| [General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info) | Demo REST hiện là `https://demo-fapi.binance.com`; 503 UNKNOWN cần xác định trạng thái thực | Không retry mù lệnh UNKNOWN. Durable intent được query/đối chiếu stream trước khi có quyết định gửi lại |
| [Public WebSocket](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public) | Book/depth dùng namespace `/public`; book hiển thị có giới hạn coverage | Order book là dữ liệu có sequence và validity; gap làm mất quyền tạo entry phụ thuộc depth tới khi resync |
| [User Data Streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams) | Namespace `/private`, lifecycle listenKey/keepalive | Event stream và REST snapshot bổ sung nhau; checkpoint và dedup cần sống qua restart |
| [Binance public data](https://github.com/binance/binance-public-data) | Archive, checksum và mô tả dataset | Manifest giữ checksum, đơn vị, coverage và metadata hợp đồng theo thời điểm; archive không tự cung cấp mọi loại lịch sử L2/OI |

Một event kích hoạt trailing không được coi là fill. Cancel-all lệnh thông thường và cancel-all algo là hai capability. Luồng khôi phục phải tìm vị thế, lệnh bảo vệ và phần đã fill trước khi cho phép entry mới. Những yêu cầu này là thiết kế của dự án dựa trên vòng đời API, chưa phải hành vi adapter đã triển khai.

Các hợp đồng timestamp của OI, taker-flow, basis, funding và liquidation sample nằm ở [macro-market-data.md](./macro-market-data.md). Khi nguồn đổi schema, parser mới có version và cần replay fixture trước khi phát hành.

## 2. Khả năng lấy dữ liệu từ các nền tảng

Danh mục trader lưu nguồn để người dùng đọc và đối chiếu. Quyền đọc một trang, quyền lưu nội dung, quyền embedding và quyền huấn luyện là các thuộc tính riêng. `not_assessed` không được diễn giải thành quyền được cấp.

| Nền tảng | Khả năng đã xác nhận từ nguồn | Connector đề xuất |
| --- | --- | --- |
| TradingView | [Hỗ trợ API](https://www.tradingview.com/support/solutions/43000474413-i-need-access-to-your-api-in-order-to-get-data-or-indicator-values/) nói không có public API lấy data/indicator. [Policies](https://www.tradingview.com/policies/) hạn chế automated collection và non-display use, gồm algorithmic decision/trading | Dùng URL để đọc thủ công phương pháp; tính indicator/chart từ feed Binance của dự án. Không bật ingestion TradingView vào trading/AI khi chưa có quyền phù hợp |
| X | [Pricing API](https://docs.x.com/x-api/getting-started/pricing) dùng credits/pay-per-usage; tại cutoff posts read là $0.005/resource | Chọn pool nhỏ, dedup post ID, giới hạn quota/ngân sách. Lưu edits/deletions, published/received time và allowed uses theo entitlement |
| YouTube | [captions.download](https://developers.google.com/youtube/v3/docs/captions/download) yêu cầu quyền chỉnh sửa video | Metadata/link giúp tìm tài liệu. Transcript chỉ nhập khi tác giả hoặc license cung cấp quyền phù hợp; public video không đồng nghĩa download caption được |
| Telegram | [Bot FAQ](https://core.telegram.org/bots/faq) cho bot nhận message từ channel nơi bot là thành viên | Chỉ đọc kênh đã cấp quyền, ghi membership và forward origin; không giả định đọc được mọi history/kênh public |
| Reddit | [Data API Terms](https://redditinc.com/policies/data-api-terms) có điều kiện riêng cho AI training và sử dụng dữ liệu | Reference discovery trước; chỉ ingest/đào tạo trong phạm vi quyền, lưu source/author/deletion và tránh đếm repost như ý kiến độc lập |
| Substack | [Terms](https://substack.com/tos) hạn chế crawling/scraping và substantial storage | Ưu tiên link tham khảo; newsletter/export do tác giả cấp quyền là nguồn riêng. Một RSS/feed truy cập được vẫn cần allowed uses |
| Website/blog/podcast | Quyền phụ thuộc từng tác giả, host và nội dung | RSS hoặc API được phép, manual MethodCard, link gốc; podcast guest khác chủ kênh. Không gán mọi bài trong một kênh cho tất cả khách mời |
| GitHub | [Release API](https://docs.github.com/en/rest/releases/releases) lấy release metadata; code license theo từng repo | Pin commit/tag và license. README mới không được dùng để giải thích lịch sử như thể đã tồn tại trước ngày công bố |
| arXiv, NBER, IMF, tác giả sách | Nội dung nghiên cứu có phạm vi truy cập/license riêng | Bibliography và tóm tắt có nguồn trước; replication chỉ khi đủ dataset/code. Không đồng nhất preprint với kết quả phản biện hoặc track record |

Ví dụ ngân sách **giả định** cho X: 150 nguồn × 10 posts/ngày × 30 ngày = 45.000 posts; với mức đọc nói trên khoảng **$225/tháng** trước lookup, các endpoint khác và chi phí AI. Đây không phải dự toán đã chốt; khối lượng thực phụ thuộc nguồn đang hoạt động, quota, cache và giá tại lúc mua. Pool MVP nên nhỏ hơn danh mục discovery.

## 3. Hợp đồng SourceConnector đề xuất

```text
SourceRegistration
  canonical_person_id, channel_id, source_group, platform
  identity_evidence, channel_evidence, last_checked_at
  permitted_collection_method, auth_scope, quota, cost_budget
  allowed_uses={metadata,reference,embedding,training,redistribution}
  rights_evidence_url, rights_valid_until, retention_policy

SourceItem
  source_item_id, origin_item_id, author_id, canonical_url
  published_at, edited_at, received_at, available_at, expiry
  language, market, symbol, timeframe, horizon
  payload_hash, parser_version, quality_flags, rights_version

Extraction
  schema_version, extractor_model_version, prompt_hash
  conditional_scenario, explicit_levels, invalidation
  uncertainty, original_evidence_refs, reviewer_status
```

Connector tạo raw item đúng quyền; extractor trả dữ liệu cấu trúc, không thực thi chỉ dẫn trong nguồn. Số giá trích xuất phải khớp trích đoạn, market/symbol/timeframe phải rõ. Khi thiếu điều kiện hoặc thời gian, đưa vào kho phương pháp; không giả lập timestamp để dùng như tín hiệu lịch sử.

Thứ tự pipeline: dedup → rights gate → kiểm tra timestamp → parse → nhận diện origin/repost → kiểm tra schema/số liệu → review mẫu → index tham chiếu hoặc feature. Outcome được lưu trong partition đánh giá; retrieval tại t không trả outcome chưa có tại t.

Trước khi thu nhận một nguồn, reviewer đọc mẫu 10–20 nội dung theo cả hai hướng thị trường, ghi phương pháp/invalidation, tần suất, quảng cáo/affiliate và sự thay đổi định nghĩa. Đây là protocol đề xuất; registry hiện chưa hoàn tất sample review này. Nguồn bị thu hồi quyền hoặc xoá nội dung được xử lý theo retention và manifest downstream, gồm index và dataset.

## 4. Các khoảng trống còn phải đo

Chưa có corpus social point-in-time đã được cấp quyền; chưa biết chi phí consensus/lịch sử L2 theo entitlement; chưa đo latency thực và chưa xác nhận adapter pinned. Vì vậy baseline OHLCV/funding và paper là bước triển khai trước. Social context dùng shadow mode, chỉ ảnh hưởng model khi có dữ liệu ex-ante và ablation cho thấy lợi ích sau chi phí. AI không dựa vào việc 132 tên cùng xuất hiện trong directory để tăng confidence.

## 5. Rà soát bổ sung ngày 21/10/2026

- Telegram [Mini Apps](https://core.telegram.org/bots/webapps) nêu không tin `initDataUnsafe`; backend xác minh HMAC của raw `initData`, cùng `auth_date` để chặn phiên cũ. Bot API 10.1 changelog ghi ngày 11/06/2026. [setWebhook](https://core.telegram.org/bots/api) có header `X-Telegram-Bot-Api-Secret-Token`; `update_id` cần deduplicate. [FAQ](https://core.telegram.org/bots/faq) nói bot nhận channel posts của channel nơi bot là thành viên, không phải reader của mọi channel. Các endpoint cần recheck khi build.
- [Google Trends API](https://developers.google.com/search/apis/trends) mô tả alpha tester giới hạn tại lần xem này; không thiết kế như API production mở cho mọi người. Website/chart là attention context, không giá giao dịch.
- Mức đọc X trong ví dụ ngân sách được lưu theo **ngày research 07/10/2026**; giá theo usage có thể thay đổi. Không dùng con số ví dụ làm billing quote ngày 21/10; integration phải lấy giá/quota trực tiếp từ [pricing/docs](https://docs.x.com/x-api/getting-started/pricing), áp spend ceiling và cache trước khi bật poll.
