# Nguồn học giao dịch quốc tế — futures, FX, quant, kỹ thuật và macro

Mốc nghiên cứu: **07/10/2026**. Tệp đi kèm: [global-traders.csv](global-traders.csv). Danh mục có **71 cá nhân riêng biệt**, **71 dòng xác minh danh tính/kênh bằng nguồn sơ cấp**, **0 dòng chỉ mới phát hiện**, và **66 URL kênh chính khác nhau**. Có **38 dòng ưu tiên cao** theo độ phù hợp học thiết kế bot. Đây là danh mục nghiên cứu, không phải bảng xếp hạng người có lợi nhuận cao nhất hoặc danh sách được khuyến nghị mua khóa học.

## Cách xây dựng và mức xác minh

Nguồn ưu tiên là website/blog của cá nhân, trang About, repository mã sách của tác giả, hồ sơ của tổ chức nơi cá nhân làm việc, bài có byline trên nền tảng xuất bản và trang sự kiện do chính đơn vị giao dịch tổ chức. Duyệt web đã đọc hoặc tìm thấy bản lập chỉ mục của từng URL bằng chứng. Không dùng danh sách tổng hợp hoặc lời kể trên Reddit làm bằng chứng cuối cùng.

`verified` chỉ xác nhận được người và nguồn phát hành gắn với người đó. **Không xác nhận lợi nhuận, lịch sử tài khoản, năng lực quản lý vốn, tính đúng của dự báo hoặc mức độc lập của người bán khóa học.** Nguồn tự giới thiệu có thể có marketing; CSV ghi chú các trường hợp nổi bật. Website còn truy cập được cũng không chứng minh tác giả đang phát hành tín hiệu mới.

`activity_status` mô tả phần đã quan sát: có bài/sự kiện mang ngày 2026, bài mới nhất nhìn thấy 2025/2023/2021, hoặc chỉ xác nhận danh tính mà chưa kiểm tra tần suất đăng. Một số nguồn được tìm bằng kết quả search khi fetch trả lỗi 403/429/timeout. Không ghi những nguồn đó là API đã kiểm tra hay feed hoạt động. Người đọc cần kiểm tra lịch xuất bản và quyền truy cập khi đưa vào hệ thống.

`role` phân biệt người giao dịch thực hành, tác giả kỹ thuật, giáo viên, người phỏng vấn, người làm phần mềm và nhà nghiên cứu macro. Ví dụ Michael Covel là tác giả/người phỏng vấn; Lyn Alden, Jim Bianco và Darius Dale được gắn vai trò nghiên cứu/chiến lược, không được diễn giải thành trader futures intraday. Hồ sơ [Cornell của Marcos López de Prado](https://www.duffield.cornell.edu/people/marcos-lopez-de-prado/) xác nhận vai trò nghiên cứu định lượng; tài liệu của ông có giá trị về suy luận thống kê hơn là nhận định chart theo phút.

Không có người đã qua đời được đưa vào phần này. Các tài liệu cũ của Ed Seykota, Andreas Clenow, Corey Hoffstein hoặc mã sách Yves Hilpisch được ghi trạng thái lưu trữ; không dùng chúng làm quan điểm thị trường hiện tại. Sách sắp xuất bản sau mốc nghiên cứu không được tính là tài liệu đã xuất bản.

## Mười nguồn nên học đầu tiên

Độ ưu tiên dưới đây là đánh giá về **mục tiêu học thiết kế hệ thống Binance futures**, không phải chất lượng tín hiệu đã được đo:

| Người | Nên học gì | Bằng chứng/kênh |
|---|---|---|
| Robert Carver | Quy tắc futures, chuẩn hóa độ biến động, sizing, kết hợp chiến lược và danh mục | [Bio chính chủ](https://www.systematicmoney.org/bio) |
| Kevin Davey | Quy trình loại bỏ ý tưởng yếu, out-of-sample, walk-forward, Monte Carlo và chạy thử | [Tài nguyên miễn phí](https://kjtradingsystems.com/free.html), [About](https://kjtradingsystems.com/about.html) |
| Ernest P. Chan | Mean reversion, mô hình định lượng, dữ liệu và quản trị rủi ro khi dùng ML | [Website tác giả](https://epchan.com/) |
| Adam H. Grimes | Cấu trúc thị trường, chart với bối cảnh, thống kê và độ biến động | [Blog chính chủ](https://www.adamhgrimes.com/) |
| Linda Bradford Raschke | Setup ngắn hạn, quá trình ra quyết định, kỷ luật và bài học thực hành futures | [Thư viện chính chủ](https://lindaraschke.net/) |
| Jim Dalton | Auction market, Market Profile, giá trị, acceptance/rejection và quan sát phiên | [About tác giả](https://jimdaltontrading.com/about/) |
| Jason Shapiro | Positioning, crowded trades, COT và cách thị trường phản ứng trái với tin | [Crowded Market Report](https://www.crowdedmarketreport.com/) |
| Cesar Alvarez | Thử ý tưởng có số liệu, mean reversion, đa dạng hóa và kiểm tra khi hệ thống suy yếu | [Blog/tài nguyên](https://alvarezquanttrading.com/), [Bio](https://alvarezquanttrading.com/bio/) |
| John Ehlers | Bộ lọc, DSP, chu kỳ, giới hạn độ trễ và sửa lỗi công thức | [Technical papers](https://www.mesasoftware.com/TechnicalArticles.htm) |
| Brent Donnelly | FX/macro, bối cảnh tin, hành vi thị trường và sizing có kỷ luật | [Spectra Markets](https://www.spectramarkets.com/), [About](https://www.spectramarkets.com/about/) |

Các nguồn bổ sung tốt cho nhánh chuyên biệt: [Al Brooks](https://www.brookstradingcourse.com/) cho price action có bối cảnh; [John Bollinger](https://www.bollingerbands.com/) cho volatility bands và xác nhận; [Thomas Bulkowski](https://www.thepatternsite.com/about.html) cho thống kê mẫu chart; [Joseph Wang](https://fedguy.com/about/) cho money markets; [Brett Steenbarger](https://traderfeed.blogspot.com/?m=1) cho review, nhật ký và quy trình phát triển trader. Việc chọn các nguồn này là suy luận nghiên cứu từ nội dung/phương pháp, chưa phải đánh giá chiến lược Binance sau backtest.

## Cách dùng trong bước đối chiếu trước AI signal

Nên tách hai loại kiến thức. Kiến thức phương pháp được biên soạn thành quy tắc có thể thử: định nghĩa setup, điều kiện xác nhận, điểm vô hiệu, regime, sizing và bằng chứng thống kê. Nhận định theo ngày chỉ được dùng khi có thời gian công bố, thị trường, timeframe và nguồn gốc rõ ràng. Không chuyển nguyên câu nói của người nổi tiếng thành lệnh.

Mỗi nhận định nên lưu `source_id`, `author`, `source_group`, URL, `published_at`, `observed_at`, symbol được tác giả nói đến, timeframe, hướng/kịch bản, mức giá nếu có, điểm vô hiệu và trích đoạn ngắn đủ ngữ cảnh. Nội dung không có timestamp hoặc mức giá cụ thể có thể phục vụ học phương pháp nhưng cần loại khỏi xác nhận tín hiệu thời gian thực. Chỉ lấy nội dung xuất bản trước thời điểm quyết định để tránh dùng thông tin tương lai.

Đối chiếu độc lập nên dựa trên phương pháp và dữ liệu, không đơn thuần đếm số người đồng ý. Các tác giả có thể đọc cùng chart, cùng tin hoặc đăng lại nhau. Kathy Lien và Boris Schlossberg dùng chung BK Traders; Chris Tate và Louise Bedford dùng chung Trading Game; Mike Bellafiore, Steven Spencer, Lance Breitstein và Seth Freudberg dùng chung SMB. Nhiều chuyên gia kỹ thuật cùng xuất bản ở StockCharts. Cần gom nhóm quan hệ nguồn và loại bài trích dẫn/đăng lại để không tạo nhiều phiếu từ một thông tin.

Nhóm COT có nhịp dữ liệu futures truyền thống khác funding/open interest của crypto. Market Profile theo phiên, cổ phiếu có giờ mở/đóng, options có payoff và đáo hạn, trong khi Binance perpetual hoạt động 24/7 và có funding. Phương pháp có thể gợi ý feature, nhưng điều kiện giao dịch, phiên, spread, phí, slippage, funding và liquidation phải được định nghĩa lại bằng dữ liệu Binance.

Các tác giả định lượng hỗ trợ kiểm định ý tưởng; người nói về macro hỗ trợ hiểu regime; nguồn chart hỗ trợ mô tả cấu trúc. Một thiết kế AI hợp lý cần trả lại “thiếu bằng chứng” khi dữ liệu gốc và nguồn không đủ, thay vì buộc tất cả quan điểm về một lệnh long/short.

## Giới hạn và những việc cần làm trước khi vận hành

- Tất cả 71 cá nhân có bằng chứng nguồn; không phải cả 71 đều có kênh tín hiệu công khai, cập nhật mỗi ngày hoặc dữ liệu có thể thu thập tự động.
- Một phần tài liệu là sách, archive, khóa học, paywall, newsletter hoặc sự kiện. Danh mục không xác nhận đã mua/đọc toàn bộ tài liệu hay có quyền sao chép/huấn luyện mô hình.
- Các URL xuất bản là điểm khởi đầu nghiên cứu. Cần thiết kế connector, kiểm tra điều khoản nguồn, attribution và lịch cập nhật trước ingest; chưa thực hiện thu thập hàng loạt.
- Không kiểm toán track record và không gắn nhãn “top 100 thế giới”. Không sử dụng follower count hoặc lợi nhuận tự công bố làm trọng số AI mặc định.
- Kết quả cuộc thi cần phân biệt giải năm, giải quý và thứ hạng. Trang Kevin Davey có thể dùng từ “three-time winner” trong marketing, nhưng chính phần chi tiết ghi 2005/2007 hạng nhì và 2006 hạng nhất. CSV đã ghi rõ khác biệt này.
- Tổng danh mục dự án cần khử trùng người và nhóm nguồn khi ghép với danh mục crypto. Các tên dễ trùng gồm Peter Brandt, Lyn Alden, Cem Karsan, Darius Dale và Ray Dalio.
