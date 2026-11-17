# Nguồn tin, mạng xã hội và lịch sự kiện cho ~500 coin USDⓈ-M (2026-11-16)

**Bối cảnh:** quyết định owner 16/11/2026 (tin uy tín, X, cộng đồng, lịch listing/unlock; TRAINING nội bộ). Đầu vào cho [WBS AI-4](../superpowers/plans/2026-11-16-ai-analysis-and-research-pipeline-wbs.md) và [Plan 05](../implementation/05-research-ingestion.md). Nguồn truy cập **2026-11-16**; kiểm lại trước khi bật connector.

**Nguyên tắc:** TRAINING của owner **không thay thế** điều khoản bên thứ ba; registry gán `training` theo từng nguồn, nhiều nguồn cấm rõ.

## 1. X (Twitter)

- **Giá:** pay-per-use bằng credit, không thuê bao: $0.005/post, $0.010/user; trần 3 triệu post/tháng, vượt thì Enterprise. Dedup 24 giờ [X1].
- **Search:** endpoint `search/recent` (7 ngày) 450 req/15′/app, ≤100 kết quả, query 512 ký tự. `search/all` (full-archive) 1 req/s, 300 req/15′, ≤500 kết quả, query 1.024 ký tự [X2]. Full-archive trên pay-per-use cần xác nhận trong console.
- **Lưu trữ:** được lưu nhưng phải đồng bộ với trạng thái trên X. Phải xóa/sửa trong 24 giờ khi có yêu cầu, và gỡ khi nội dung không còn trên API. Cấm lưu location. Chỉ phân phối ID, tối đa 1,5 triệu ID/30 ngày [X3].
- **Hiển thị:** giữ nguyên vẹn, lấy bản mới nhất, giữ attribution [X3].
- **AI:** từ 06/2025, Developer Agreement cấm dùng X API/X Content để "fine-tune or train a foundation or frontier model" [X4]. Scorer logistic nội bộ là **vùng xám**; mặc định không gán `training`.
- **Scraping:** ToS (hiệu lực 09/10/2026, theo nguồn thứ cấp) cấm crawl/scrape "in any form, for any purpose" nếu không có văn bản đồng ý. Phạt định trước $15.000 cho mỗi 1 triệu post vượt 1 triệu/24 giờ [X5]. (x.com/tos trả 402, chưa đọc trực tiếp). **Tránh** scraping và reseller/scraper bên thứ ba (Apify…): không có chuỗi quyền.
- **Đề xuất:** dùng pay-per-use với danh sách tài khoản chính thức theo coin, query dạng `from:handle1 OR from:handle2…`. (≤512 ký tự), cộng ít keyword sự kiện (`unlock`, `exploit` + cashtag). Ước tính 500 coin × 5 post/ngày × 30 ≈ 75k post ≈ **$375/tháng**, keyword thêm $100–300. Hard cap ở console + quota registry.

## 2. Báo crypto uy tín và aggregator

- **CoinDesk:** có RSS `coindesk.com/arc/outboundfeeds/rss/` [N5]. ToS cấm robot/scraper khi chưa có văn bản cho phép, chỉ cho dùng cá nhân/phi thương mại [N1]. Poll RSS lấy tiêu đề/link là vùng xám; TRAINING phải xin phép.
- **Cointelegraph:** cấm scraping và cấm dùng cho "training, fine-tuning, validation, benchmarking or operation" AI [N2]. **Tránh**, kể cả đưa vào LLM.
- **The Block:** RSS chỉ dùng trong "RSS feed reader", cấm train và cấm tóm tắt bằng AI [N3]. **Tránh**.
- **Decrypt:** trang điều khoản trả 404, cần kiểm lại. **Bloomberg/Reuters:** chỉ dùng khi mua license.
- **CryptoPanic:** API free 5 req/s, bắt buộc link về nguồn; Enterprise 10 req/s [N4]. Chưa thấy điều khoản AI. Ứng viên S2.
- **CoinGecko:** Demo $0 (10k call, 100/phút), Basic $35, Analyst $129, bắt buộc attribution, cấm resell [N6]. Không có news; dùng làm mapping dự án.
- **Messari:** chỉ báo giá Enterprise [N7]. Để sau.

## 3. Kênh chính thức của sàn và dự án

- **Binance CMS WebSocket** `wss://api.binance.com/sapi/wss`, topic `com_announcement_en`. Payload: `catalogId/catalogName` (161 = Delisting), `publishDate`, `title`, `body`; ký HMAC, 5 msg/s, kết nối 24 giờ [B1][B2]. Cần **key chỉ-đọc riêng** trong `.secrets/`; owner phải duyệt vì Plan 05 cấm WP-05 dùng credential account.
- **Binance `exchangeInfo`** (đã duyệt) cho `onboardDate` và `status` để phát hiện listing/delisting.
- **GitHub Releases API:** 60 req/giờ nếu không xác thực, 5.000 req/giờ nếu có token [G2]. API không bị coi là scraping, nhưng vẫn cấm "reproduce… exploit" dịch vụ [G1]. Chỉ lưu metadata (tag, `published_at`, URL).

## 4. Lịch sự kiện

- **Vĩ mô (public domain):** BLS có ICS `bls.gov/schedule/news_release/bls.ics`, public domain, ghi nguồn BLS [M1][M2]. BEA có ICS và JSON; GDP/PCE kỳ tới là 25/11/2026 [M3]. Fed có lịch FOMC và RSS: kỳ tới 08–09/12/2026, rồi 26–27/01/2027 [M4]. Mở khóa H06.
- **Phái sinh Binance:** `openInterestHist` và `globalLongShortAccountRatio` có weight 0, 1000 req/5 phút, chỉ giữ 30 ngày gần nhất [B3]. Phải **tự snapshot liên tục**. Market data đã duyệt.
- **Unlock:** Tokenomist có gói Pro ~$69 (1.000 call/tháng), Standard/Elite $249.95–449.95. Dữ liệu chỉ dành cho "personal or non-commercial, non-redistribution", commercial license là add-on [C1]. Chưa thấy điều khoản AI. CryptoRank cần kiểm thêm.
- **CoinMarketCal:** API `/v1/events` (`x-api-key`); trang giá trả 403, chưa xác minh [C2]. Để sau.

## 5. Cộng đồng

- **Reddit:** Data API Terms bản 2026-07-20 yêu cầu rightsholder cho phép trước khi training AI/ML, kèm điều kiện xóa [R1]. Free 100 QPM chỉ cho phi thương mại; giá $0.24/1k call chỉ có ở nguồn thứ cấp. S3, M/D, không T.
- **Telegram:** API ToS §1.5 cấm "using, accessing or aggregating data… to train" AI/ML [T1]. Bot chỉ nhận update ở kênh mà nó là thành viên. Chỉ theo kênh chính thức, M.
- **Discord:** tuyên bố không cho công ty AI train trên tin nhắn [D1]; self-bot vi phạm ToS. **Tránh**.
- **TradingView:** §12 cấm scraper và cấm "train, fine-tune, test, or improve" AI; script mặc định MPL-2.0 [TV1]. **Tránh** ingest; chỉ đọc thủ công.
- **StockTwits:** ToS 10/07/2026 cấm scraping nếu không qua API được duyệt [S1]. Để sau.

## 6. Ma trận nguồn

Purpose: M=`metadata`, D=`display`, B=`backtest_research`, P=`paper_demo`, T=`training`. Lát: S1 ngay · S2 X trả phí/aggregator · S3 cộng đồng.

| Nguồn | Loại dữ liệu | Cách truy cập | Chi phí | Rate | Lưu/Train | Phủ ~500 coin | Purpose registry | Lát |
|---|---|---|---|---|---|---|---|---|
| BLS/BEA/Fed | Lịch vĩ mô | ICS/JSON/RSS | $0 | thấp | Có/Có (public domain) | Toàn thị trường | M,D,B,P,T | **S1** |
| Binance OI/LS/funding | Phái sinh | REST công khai | $0 | 1000/5′ | Đã duyệt | 100% | M,D,B,P,T | **S1** |
| Binance `exchangeInfo` | Listing/delisting | REST | $0 | weight | Đã duyệt | 100% | M,D,B,P,T | **S1** |
| Binance CMS WS | Thông báo | WS ký key | $0 | 5 msg/s | Cần đọc ToS Binance | 100% | M,P | **S1** (khi owner duyệt key) |
| CoinGecko | Mapping dự án | REST | $0–35 | 100–300/′ | Attribution, cấm resell | ~95% | M | **S1** |
| GitHub Releases | Upgrade | REST | $0 | 5k/giờ | Chỉ metadata | Theo repo | M,P | **S1** |
| CoinDesk RSS | Tiêu đề tin | RSS | $0 | poll 15′ | Metadata; T cần xin phép | Coin lớn | M,D | S1 sau khi xin phép / S2 |
| X API | Post chính thức + keyword | REST pay-per-use | ~$400–700/tháng | 450/15′ | Lưu kèm đồng bộ xóa; cấm train foundation | Cao | M,D,P | **S2** |
| CryptoPanic | Tin tổng hợp | REST | Free/Enterprise | 5 req/s | Chưa rõ | Cao | M | S2 |
| Tokenomist | Unlock | REST | $69–450/tháng | 1k–500k/tháng | Phi thương mại, không redistribute | Token có unlock | M,P | S2 |
| Reddit/Telegram | Cộng đồng | API/Bot | Free–? | 100 QPM | Cấm train | Coin lớn | M | S3 |
| Cointelegraph/The Block/TradingView/Discord | — | — | — | — | Cấm AI | — | — | **Tránh** |

## 7. Thiết kế ingest cho ~500 coin

1. **Mapping** `project_map(base_asset, coingecko_id, website, x_handle, github_repos[], rss_urls[], verified_at, verified_by)`. Gốc từ CoinGecko; owner duyệt handle (nhiều tài khoản giả); chuẩn hóa `1000PEPE`→`PEPE`; không đoán từ cashtag.
2. **Event schema:** `event_id, source_id, policy_seq, canonical_url, url_hash, title_hash, published_at, observed_at, available_at, event_at?, symbols[], event_type ∈ {listing, delisting, futures_launch, maintenance, unlock, upgrade, hack, macro, indicator_idea, other}, short_meta (≤280 ký tự, chỉ khi policy cho phép), confidence, extractor_version`. Không lưu full-text.
3. **Dedup:** canonical URL, rồi `(event_type, symbols, event_at ±1h)`. Cụm đa nguồn tính một sự kiện.
4. **Chống look-ahead:** replay/snapshot chỉ thấy `available_at <= as_of`, với `available_at` = lúc hệ thống nhận; không dùng `published_at` (có thể backdate).
5. **Prompt injection:** nội dung là dữ liệu; phân loại bằng rule trước; vào LLM thì bọc delimiter, giới hạn token, không tool/secret, output qua JSON schema; LLM không tạo lệnh.

## Đề xuất lát S1

1. Connector lịch vĩ mô (BLS ICS, BEA JSON, Fed FOMC) để H06 hết `MACRO_UNKNOWN`.
2. Snapshot OI/long-short (period 5m, `limit` lớn) theo lô mỗi giờ: 500 coin × 2 endpoint × 5 phút sẽ chạm đúng trần 1000 req/5′.
3. Diff `exchangeInfo` để sinh event listing/delisting.
4. Bảng `project_map` từ CoinGecko Demo, owner duyệt handle.
5. GitHub Releases cho top ~100 coin theo volume (METADATA).
6. Binance CMS WS sau khi owner duyệt key chỉ-đọc.

## Câu hỏi cho owner

1. Ngân sách X API mỗi tháng (gợi ý $500) và hard cap?
2. Có duyệt API key Binance chỉ-đọc riêng cho CMS WebSocket không?
3. Có chấp nhận rằng TRAINING không áp cho X/Telegram/Reddit/báo chí khi chưa có license không?
4. Có xin phép CoinDesk, hoặc mua Tokenomist ($69–250/tháng)?
5. Ai duyệt danh sách handle X và GitHub cho từng coin?

## Nguồn (truy cập 2026-11-16)

- [X1] https://docs.x.com/x-api/getting-started/pricing · [X2] https://docs.x.com/x-api/fundamentals/rate-limits · [X3] https://docs.x.com/developer-terms/agreement-and-policy · [X4] https://techcrunch.com/2025/06/05/x-changes-its-terms-to-bar-training-of-ai-models-using-its-content · [X5] https://conductatlas.com/platform/x/x-terms-of-service/acceptable-use-and-scraping-prohibition/ (thứ cấp)
- [N1] https://www.coindesk.com/terms · [N2] https://cointelegraph.com/terms-and-privacy · [N3] https://www.theblock.co/terms-of-service · [N4] https://cryptopanic.com/developers/api/about · [N5] https://feeder.co/knowledge-base/rss-content/crypto-news-rss-feeds/ · [N6] https://www.coingecko.com/en/api/pricing · [N7] https://pricingsaas.com/companies/messari (thứ cấp)
- [B1] https://developers.binance.com/docs/cms/general-info · [B2] https://developers.binance.com/docs/cms/announcement · [B3] https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Long-Short-Ratio
- [G1] https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies · [G2] https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- [M1] https://www.bls.gov/schedule/news_release/ · [M2] https://www.bls.gov/opub/copyright-information.htm · [M3] https://www.bea.gov/news/schedule · [M4] https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
- [C1] https://tokenomist.ai/llms.txt · https://docs.tokenomist.ai/api-documents · [C2] https://pipedream.com/apps/coinmarketcal (thứ cấp)
- [R1] https://redditinc.com/policies/data-api-terms (không tải được hôm nay; nội dung theo Plan 05 kiểm 2026-11-01) · https://data365.co/blog/reddit-api-pricing (thứ cấp)
- [T1] https://core.telegram.org/api/terms · [D1] https://discord.com/safety/important-policy-updates · [TV1] https://www.tradingview.com/policies/ · [S1] https://stocktwits.com/terms
