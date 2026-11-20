# Nghiên cứu quyền dữ liệu cho WP06

**Kiểm tra gần nhất:** 2026-11-20 (UTC)
**Mục tiêu:** xác định nguồn lịch sử có thể cấp quyền rõ ràng cho backtest, TRAINING, lưu artifact/report và PAPER scorer nội bộ. Đây là ghi nhận phạm vi tài liệu nguồn, không thay thế tư vấn pháp lý hoặc license đã ký.

## Kết luận hiện tại

- Có thể truy cập một endpoint hoặc tải archive công khai không tự chứng minh quyền dùng dữ liệu cho huấn luyện, lưu model hoặc vận hành bot.
- Binance Vision công bố dữ liệu dưới CC BY-NC-SA 4.0 và các điều khoản riêng. Phạm vi nêu rõ là nghiên cứu phi thương mại, backtest cá nhân phi production và đánh giá khoa học dữ liệu nguồn mở phi thương mại; điều khoản loại trừ sản phẩm tài chính/bot thương mại và order generation thương mại nếu không có license doanh nghiệp bằng văn bản. Vì vậy, không dùng Vision làm receipt cho active scorer hoặc PAPER_GATE của sản phẩm này. Mã downloader đã giới hạn purpose ở `PERSONAL_NONCOMMERCIAL_RESEARCH` và unit test từ chối `PRODUCT_OR_DEMO`.
- CoinAPI có thể là ứng viên nếu có gói phù hợp cùng xác nhận/license bằng văn bản. Hướng dẫn use case không thay thế thỏa thuận ràng buộc và không tự xác nhận quyền của nguồn Binance Futures bên thứ ba. Chưa mua gói, chưa gọi API và chưa có receipt.
- CoinAPI giới thiệu dữ liệu Futures lịch sử cho Binance và mã sàn `BINANCEFTS` (USDⓈ-M), nhưng chưa xác minh mã BTCUSDT/khung 1 phút/độ phủ ngày cụ thể. Giá Market Data API đang niêm yết theo tháng: Startup $79 (1.000 REST credits/ngày), Streamer $249 (10.000/ngày), Pro $599 (100.000/ngày), Enterprise báo giá riêng; PAYG nêu $25 credit lúc đăng ký nhưng cần API key và phương thức thanh toán đã xác thực. Theo bảng giá bậc, 525.600 nến 1 phút/năm xấp xỉ 5.256 REST credits; nếu lấy hết trong một ngày, ước tính khoảng $14,42 theo bậc niêm yết hiện tại. FAQ mô tả biểu giá khác bảng giá, nên đây không phải báo giá hoặc cam kết hóa đơn; cần xác nhận trước khi phát sinh phí. Một năm một mã có thể nằm trong $25 credit lúc đăng ký nếu đủ điều kiện, nhưng không tự cấp quyền dữ liệu.
- Kaiko cần license riêng cho tính toán/backend trading; license “Display” không đủ.
- Do chưa có license/receipt phi QA phù hợp, chưa được tạo report TRAINING đủ điều kiện, kích hoạt SHADOW/PAPER_GATE hoặc đánh dấu WP06 hoàn tất.

## Cổng quyền bắt buộc trước khi nạp hoặc huấn luyện

Receipt bất biến cho từng run phải ghim tối thiểu:

1. Provider, sản phẩm/gói, venue, loại hợp đồng (ví dụ Binance USDⓈ-M Futures), symbol, interval và khoảng thời gian.
2. Bản thỏa thuận/license có hiệu lực khi truy cập dữ liệu; URL/version/ngày kiểm tra; mã hợp đồng/order hoặc giấy phép.
3. Xác nhận bằng văn bản rằng phạm vi cho phép đọc/lưu dữ liệu một năm, backtest, `TRAINING`/tái `TRAINING`, giữ model weights/calibration/report/derived outputs và dùng trong PAPER/SHADOW nội bộ. Đây là các purpose riêng: scorer active phải có quyền hiện hành `TRAINING` và `PAPER_DEMO`, không suy quyền PAPER từ quyền TRAINING.
4. Hạn dùng, quy tắc revoke/takedown, retention/xóa dữ liệu, attribution/share-alike và quyền lưu artifact sau khi gói hết hạn.
5. Hash của bằng chứng và manifest/snapshot dữ liệu; workspace, actor, purpose và thời điểm recheck. Revoke hoặc thay đổi điều khoản phải chặn run kế tiếp và xử lý theo điều khoản lưu/xóa.

Không chấp nhận README license của mã downloader thay cho license của dữ liệu; không tự điền đồng ý điều khoản thay người sở hữu dữ liệu; không hạ ngưỡng policy để vượt qua thiếu dữ liệu hoặc quyền.

## Rà soát lại CoinAPI — 2026-11-20

- **Giá công khai:** trang Market Data API hiện niêm yết Startup **$79/tháng** với **1.000 REST credits/ngày**, Streamer **$249/tháng** với **10.000/ngày**, Pro **$599/tháng** với **100.000/ngày**; Enterprise cần hỏi bộ phận kinh doanh. Gói Pay As You Go không có phí tháng, nhưng điều kiện credit khởi tạo **$25** yêu cầu tạo API key và phương thức thanh toán đã xác minh. Bảng giá tính theo mức sử dụng trong ngày. FAQ diễn giải bậc credit hơi khác bảng giá, nên không coi phép tính tự ước lượng là báo giá.
- **Ước lượng 1 năm dữ liệu:** 365 × 24 × 60 = **525.600** nến 1 phút cho một mã; điểm cuối API OHLCV cho phép tối đa 100.000 điểm/trả lời và tính xấp xỉ một REST credit cho mỗi 100 điểm, tương đương khoảng **5.256 credits/mã** trước metadata, dữ liệu thiếu và lần thử lại. Áp bảng giá bậc hiện hành, con số một mã trong một ngày xấp xỉ **$14,42**; cần CoinAPI xác nhận báo giá và quy tắc tính đúng cho API, thời gian và số mã thật. Hai mã gần **10.512 credits**, vượt quota ngày Streamer một chút nếu tải liền trong ngày.
- **Độ phủ kỹ thuật:** CoinAPI FAQ liệt kê Binance USD-M Futures qua exchange ID `BINANCEFTS`, có BTC/USDT và ETH/USDT khi mã còn hoạt động; tài liệu hiện hành có `/v1/ohlcv/{symbol_id}/history` và giới hạn trả lời 100.000 bản ghi. Nhưng metadata API/tài khoản chưa được truy vấn, nên chưa xác minh chính xác symbol ID, giai đoạn có dữ liệu, tính liên tục, định danh venue và khả năng tải đủ 1 năm. OHLCV hiện chỉ truy cập qua Market Data API, không có sẵn trong Flat Files; dữ liệu OHLCV lịch sử có thể trễ vài giây.
- **Giới hạn hợp đồng:** Customer Agreement hiện hành (hiệu lực công bố 2025-08-18) cấp quyền dùng dịch vụ cho mục đích kinh doanh nội bộ. Mục 2.3 nói khách hàng tự chịu trách nhiệm về việc dùng các Data Source bên ngoài; nguồn đó có thể giới hạn việc sử dụng và yêu cầu license riêng từ bên thứ ba. Vì thế, mua CoinAPI hoặc nhận $25 credit **không** tự chứng minh Binance cho phép lưu nến OHLCV, backtest, TRAINING, giữ model/đầu ra phái sinh hoặc PAPER/SHADOW. Điều khoản/AUP cũng cấm bán lại dịch vụ hoặc một phần dịch vụ; phạm vi cung cấp bot cho khách hàng cần được CoinAPI trả lời riêng.
- **Hướng tiếp theo, chưa thực hiện:** xin CoinAPI xác nhận bằng văn bản (1) quyền họ có thể cấp và quyền Binance còn yêu cầu cho dữ liệu USD-M Futures BTC/ETH 1m; (2) backtest, TRAINING, lưu nến và giữ model/report/feature phái sinh trong thời gian và sau khi hủy gói; (3) PAPER/SHADOW nội bộ và việc có/không cung cấp sản phẩm cho khách hàng; (4) ghi nguồn, phân phối lại/hiển thị, thời hạn lưu/xóa/thu hồi; (5) phí thực cho hai mã, ngày bắt đầu, 1 năm và phương thức thanh toán. Chưa gửi yêu cầu, tạo tài khoản/API key, tải dữ liệu hay phát sinh chi phí.

## Khoảng trống contract cần đóng

Rà soát mã ngày 12/11/2026 phát hiện receipt hiện tại ghim fingerprint/reference của policy và metadata lần kiểm, nhưng chưa chứng minh hash của bytes thỏa thuận/license gốc hay một scope có kiểu cho provider, product, venue, contract, symbol, interval, date range và loại output được phép. `evidence_ref` tự nó không chứng minh nội dung được tham chiếu. Vì vậy, trạng thái policy `VERIFIED` và receipt hiện có chưa đủ để tuyên bố đã lưu bằng chứng license chính xác; không tạo positive rights journey dựa trên fixture. Phải giữ `SHADOW`/`PAPER_GATE` khóa cho tới khi contract và bằng chứng áp dụng thật được ghim, xác minh và có quyền hiện hành cho từng purpose.

## Nguồn chính thức đã kiểm tra

- Binance Public Data README (định dạng/nguồn archive; README dẫn riêng sang điều khoản dữ liệu): <https://github.com/binance/binance-public-data/blob/master/README.md>
- Binance Vision Dataset Terms v1.0, công bố cập nhật 2026-08-26: <https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md>
- Binance USDⓈ-M developer documentation (API mô tả truy cập market data, không phải license archive): <https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures>
- CoinAPI Customer Agreement/AUP (hiệu lực công bố 2025-08-18; kiểm tra 2026-11-20): <https://www.coinapi.io/legal>
- CoinAPI Market Data API pricing: <https://www.coinapi.io/products/market-data-api/pricing>
- CoinAPI Market Data API FAQ (coverage, exchange ID, credits/billing): <https://www.coinapi.io/products/market-data-api/faq>
- CoinAPI OHLCV historical endpoint: <https://www.coinapi.io/products/market-data-api/docs/rest-api/ohlcv/ohlcv/symbol_id/history/get>
- CoinAPI flat-files history FAQ: <https://www.coinapi.io/products/flat-files/faq>
- Kaiko exchange agreements: <https://www.kaiko.com/exchange-agreements>
- Kaiko data products: <https://www.kaiko.com/products/l1-l2-data>
- Kaiko display-license scope: <https://www.kaiko.com/resources/display>

## Việc còn cần

- Ledger entry-hold riêng đã được owner duyệt và triển khai; xem spec WP06 incident entry hold. Cần license/receipt áp dụng cho đúng mục đích nội bộ trước khi tải/training. CoinAPI public pricing và điều khoản đã được rà lại ngày 2026-11-20 nhưng chưa có báo giá theo yêu cầu cụ thể; chưa gửi yêu cầu ra ngoài, chưa tạo account/API key và chưa phát sinh chi phí.
- Khi có bằng chứng, triển khai provider/evidence adapter nếu cần, nhập nguồn non-QA, tạo run mới với receipt theo run, chạy đủ TRAINING → TEST proof/report theo policy `wp06-scorer-test-v1`, rồi mới kiểm SHADOW, PAPER_GATE riêng, veto/abstain, rollback/restart, history retention và zero-side-effect.
- Sau journey hợp lệ, chạy independent integrated security review. Giữ active modes khóa trong mọi môi trường sản phẩm cho tới khi các cổng này đạt.
