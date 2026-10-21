# Hồ sơ ứng viên crypto phục vụ nghiên cứu bot

Ngày chốt nghiên cứu: **2026-10-07**, múi giờ Asia/Saigon. Đây là danh mục con gồm **63 cá nhân hoặc bút danh cá nhân**, dùng để học phương pháp và so sánh luận điểm. Có **47 dòng `verified_channel`** và **16 dòng `discovered_candidate`**. **Không có dòng nào được xác minh PnL bằng kiểm toán độc lập.** Không có cơ sở để gọi đây là 63 trader có thành tích tốt nhất thế giới.

## Cách hiểu và phương pháp thu thập

- `verified_channel`: tìm thấy nội dung tự xuất bản, hồ sơ tác giả, website của cá nhân, hoặc liên kết chéo từ kênh chính. Mức này xác minh việc gán nguồn cho tác giả; không xác minh danh tính pháp lý, tài sản, mọi tài khoản phụ, hay khả năng sinh lời.
- `discovered_candidate`: có nguồn công khai dẫn tới cá nhân nhưng cần xác minh kênh riêng, liên kết chéo hoặc nội dung mới. Một số nguồn chỉ trả về tiêu đề kênh; điều này được ghi trong CSV.
- `not_checked`: chưa kiểm tra hoạt động hiện tại. Nhãn `dated_2026_publication` chỉ nói công cụ nghiên cứu trả về bài/video mang ngày năm 2026; chưa đối chiếu với log nền tảng.
- `historical_*`: bài, video, giấy nghiên cứu hoặc phỏng vấn cũ. Không tự động suy ra tác giả đã nghỉ giao dịch, còn giữ chức danh cũ, hoặc đã qua đời. Danh mục này không có dòng được đánh dấu đã qua đời.
- `priority`: độ phù hợp sơ bộ với nghiên cứu BTC/ETH, cấu trúc giá, order flow, quản lý rủi ro và chế độ thị trường; không phải thứ hạng lợi nhuận.

Tìm kiếm theo tên/bút danh, sau đó ưu tiên website riêng, Linktree có mô tả tự xuất bản, hồ sơ nền tảng, kho bài có tên tác giả và feed của nhà sản xuất phỏng vấn. Không dùng số người theo dõi, một dự báo đúng, lời quảng cáo tỷ lệ thắng hoặc điểm sentiment như bằng chứng PnL. Những tài khoản X tên gần giống nhưng dấu hiệu mạo danh rõ đã bị loại. Daan, Trader XO và các kênh chỉ có định danh tối thiểu vẫn ở mức ứng viên.

CSV có đúng schema được yêu cầu. Các kênh dùng chung như TechnicalRoundup, DataDash và feed podcast chỉ tính người khác nhau khi nguồn nêu riêng từng cá nhân. Khi hợp nhất các danh mục, cần chuẩn hóa alias: **James Check = Checkmate; Cheds = BigCheds; CrediBULL Crypto = CredibleCrypto; Josh Olszewicz = CarpeNoctom; Adrian Zduńczyk = CryptoBirb; CBS = ColdBloodedShiller; Ledger Status = Brian Krogsgard**. CBS trong danh mục hiện chỉ có bằng chứng Mayne giới thiệu; alias phải xác minh thêm trước nhập dữ liệu.

## Mười nguồn nên xem trước

| Cá nhân | Giá trị nghiên cứu | Nguồn chính và giới hạn |
|---|---|---|
| CryptoCred | Khung cấu trúc thị trường, vùng giá và quản trị rủi ro để chuyển lời diễn giải thành giả thuyết có điều kiện. | [Kho liên kết tự xuất bản](https://linktr.ee/cryptocred); chưa kiểm tra bài mới và PnL. |
| DonAlt | So sánh kịch bản BTC/ETH và cách đọc vùng giá giữa hai người cùng chương trình. | [TechnicalRoundup](https://www.youtube.com/@TechnicalRoundup), [feed nhà sản xuất](https://rss.com/podcasts/technicalroundup/); phải gán đúng người nói. |
| Peter Brandt | Mẫu hình cổ điển, điểm vô hiệu hóa và cách quản lý vị thế. | [Website](https://www.peterlbrandt.com/), [kho cập nhật có ngày](https://www.peterlbrandt.com/home-1/); nhận định và thành tích tự mô tả không phải kiểm toán. |
| James Check | Kết hợp on-chain, tâm lý người nắm giữ, futures và thay đổi chế độ. | [Giới thiệu cá nhân](https://www.checkonchain.com/about), [newsletter](https://newsletter.checkonchain.com/); nhiều nội dung có paywall. |
| Skew | Cách đọc luồng giao dịch và quan hệ giữa spot/perpetual. | [Blog tác giả](https://52kskew.medium.com/); kho được đọc có bài cũ, cần kênh mới được liên kết chéo. |
| Keith Alan | Order book, heatmap và CVD, hữu ích để phân biệt thanh khoản hiển thị với khớp lệnh thật. | [Kho bài theo tác giả](https://materialindicators.com/author/keithadmin/); không đồng nhất feed công ty với toàn bộ ý kiến cá nhân. |
| RektProof | Giá, range và cách mô tả setup có điều kiện. | [Website](https://rektproof.com/), [TradingView có liên kết tác giả](https://www.tradingview.com/u/Rektproof/); chưa có PnL kiểm toán. |
| Trader Mayne | Range, support/resistance, liquidity sweep và nhật ký giao dịch. | [Website cá nhân](https://www.tradermayne.com/home); có quảng bá dịch vụ, cần đánh dấu xung đột lợi ích. |
| Benjamin Cowen | Chu kỳ, rủi ro danh mục và bối cảnh vĩ mô cho bộ lọc chế độ. | [Website cá nhân](https://www.benjamincowen.com/); nhà nghiên cứu/giáo dục, không có bằng chứng chiến lược futures đã sinh lời. |
| David Puell | Các thước đo on-chain có định nghĩa để kiểm định độc lập. | [Cointime Economics đồng tác giả](https://charts.checkonchain.com/webassets/docs/Cointime%20Economics.pdf); đây là nghiên cứu, không phải luồng tín hiệu thời gian thực. |

Các nguồn bổ sung đáng xem gồm [Willy Woo](https://woobull.com/author/willywoo/), [Cheds](https://bsky.app/profile/chedstrading.bsky.social), [Josh Olszewicz trong loạt nội dung do BNC sản xuất](https://bravenewcoin.com/insights/cryptocurrency-trading-tips), [Rekt Capital](https://www.rektcapital.co.uk/) và [Noelle Acheson](https://www.cryptoismacro.com/). Tác giả vĩ mô nên dùng cho bối cảnh; độ chính xác nhận định dài hạn không đại diện khả năng timing futures ngắn hạn.

## Cách đưa nguồn vào nghiên cứu

Mỗi luận điểm được chọn nên có URL gốc, tên tác giả, thời điểm công bố và tiếp nhận, asset, timeframe, điều kiện kích hoạt, vùng vô hiệu hóa, mục tiêu và câu nói nguyên bản ngắn có quyền sử dụng. Tách ngày công bố khỏi ngày crawler đọc; không điền tín hiệu vào quá khứ theo thời điểm nến mà bài xuất hiện sau đó.

Giữ cả luận điểm sai, trung tính, không vào lệnh, bài bị sửa và setup hết hiệu lực. Đánh giá tất cả trên cùng giá sàn, phí, funding, trượt giá và quy tắc thoát. Dự báo chiều giá, hiệu suất giả lập theo một quy tắc do người nghiên cứu dựng, và PnL tài khoản thật là ba phép đo khác nhau. Chọn nhiều tác giả cùng phương pháp có thể tạo ảo giác đồng thuận vì họ đọc cùng nguồn dữ liệu.

Không tự động gửi lệnh từ bài viết/chart. Chỉ tạo giả thuyết có thể kiểm định trên dữ liệu Binance được phép dùng, rồi so sánh với baseline và dữ liệu ngoài mẫu. Không có nguồn nào trong danh mục bảo đảm lợi nhuận cho bot USDT-M.

## Giới hạn khi nhập nội dung công khai

**Công khai để đọc không có nghĩa được phép sao chép hàng loạt hoặc dùng cho huấn luyện.** Cần xét giấy phép, điều khoản và quyền tác giả riêng cho từng nguồn trước xây connector:

- **TradingView:** điều khoản hiện đọc được cấm thu thập tự động và cấm nhiều mục đích sử dụng dữ liệu ngoài hiển thị, bao gồm quyết định thuật toán/giao dịch tự động. Chỉ dùng liên kết để con người đọc hoặc có quyền sử dụng riêng bằng văn bản; OHLCV của bot lấy từ nhà cung cấp được cấp phép. [Điều khoản](https://www.tradingview.com/policies/), [giải thích chính thức về thu thập tự động](https://www.tradingview.com/support/solutions/43000674726-why-is-my-account-banned-due-to-suspicious-activity/).
- **YouTube:** API có thể phục vụ metadata trong phạm vi điều khoản; endpoint captions.download yêu cầu quyền chỉnh sửa video. Không giả định có thể lấy transcript của mọi video qua API chính thức. Đọc trực tiếp, dùng transcript do tác giả cung cấp hoặc xin quyền thích hợp. [Tài liệu captions.download](https://developers.google.com/youtube/v3/docs/captions/download).
- **Substack:** ToS hiện đọc được cấm crawling/scraping và lưu phần đáng kể nội dung. Không dựng crawler thu hàng loạt bài miễn phí hoặc trả phí. Chỉ dùng nội dung tác giả cung cấp theo quyền được cho, và giữ liên kết/ghi chú tự viết trong phạm vi được phép. [ToS chính thức](https://substack.com/tos).
- **X, Telegram, Discord và website riêng:** kiểm tra API/điều khoản hiện hành và quyền sử dụng trước triển khai; không vượt đăng nhập, paywall hoặc hàng rào kỹ thuật. Không suy ra tính chính chủ chỉ từ chữ “official”, avatar, dấu tick hay tên gần giống. Danh mục chưa kiểm toán đầy đủ điều khoản riêng của các nền tảng này.
- **Nguồn phỏng vấn cũ:** Avi Felman, Max Boonen, CL và Ledger Status được tìm thấy từ feed nhà sản xuất; đây là ứng viên tìm kênh mới, không phải bốn feed hiện tại đã xác minh.

## Phần còn thiếu

Danh mục con đạt 63 cá nhân; chưa phải trọn yêu cầu 100–200 người. Hoạt động hiện tại của phần lớn nguồn, khả năng truy cập API, quyền cấp phép nội dung, tính đầy đủ của lịch sử bài và hiệu suất giao dịch thật còn chưa xác minh. Có thiên lệch ngôn ngữ tiếng Anh, thiên lệch sống sót, dữ liệu bị xóa/sửa và xung đột thương mại từ subscription, referral bursa, prop firm hoặc quảng bá token.

Nên mở rộng theo cá nhân có phương pháp và nguồn rõ, rồi loại trùng alias giữa các danh mục. Không thêm tài khoản mơ hồ để đủ số lượng, không gọi quan điểm tự xuất bản là “thành tích đã xác minh”, và không sao chép chỉ báo độc quyền thành strategy mà chưa có quyền.
