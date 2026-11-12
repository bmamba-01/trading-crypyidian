# Nghiên cứu quyền dữ liệu cho WP06

**Kiểm tra:** 2026-11-12 (UTC)
**Mục tiêu:** xác định nguồn lịch sử có thể cấp quyền rõ ràng cho backtest, TRAINING, lưu artifact/report và PAPER scorer nội bộ. Đây là ghi nhận phạm vi tài liệu nguồn, không thay thế tư vấn pháp lý hoặc license đã ký.

## Kết luận hiện tại

- Có thể truy cập một endpoint hoặc tải archive công khai không tự chứng minh quyền dùng dữ liệu cho huấn luyện, lưu model hoặc vận hành bot.
- Binance Vision công bố dữ liệu dưới CC BY-NC-SA 4.0 và các điều khoản riêng. Phạm vi nêu rõ là nghiên cứu phi thương mại, backtest cá nhân phi production và đánh giá khoa học dữ liệu nguồn mở phi thương mại; điều khoản loại trừ sản phẩm tài chính/bot thương mại và order generation thương mại nếu không có license doanh nghiệp bằng văn bản. Vì vậy, không dùng Vision làm receipt cho active scorer hoặc PAPER_GATE của sản phẩm này. Mã downloader đã giới hạn purpose ở `PERSONAL_NONCOMMERCIAL_RESEARCH` và unit test từ chối `PRODUCT_OR_DEMO`.
- CoinAPI có thể là ứng viên nếu có gói phù hợp cùng xác nhận/license bằng văn bản. Hướng dẫn use case không thay thế thỏa thuận ràng buộc và không tự xác nhận quyền của nguồn Binance Futures bên thứ ba. Chưa mua gói, chưa gọi API và chưa có receipt.
- CoinAPI quảng bá historical Futures cho Binance và mã sàn `BINANCEFTS` (USDⓈ-M), nhưng chưa xác minh symbol BTCUSDT/khung 1 phút/độ phủ ngày cụ thể. Giá Market Data API đang niêm yết theo tháng: Startup $79 (1.000 REST credit/ngày), Streamer $249 (10.000/ngày), Pro $599 (100.000/ngày), Enterprise báo giá riêng; PAYG nêu $25 credit lúc đăng ký nhưng cần API key và payment method đã xác thực. Theo pricing page tiered, 525.600 bars 1m/năm xấp xỉ 5.256 REST credits; nếu lấy hết trong một ngày, ước tính khoảng $14,42 theo tier niêm yết hiện tại. FAQ có biểu giá mô tả khác với pricing page, nên đây không phải quote hoặc cam kết hóa đơn; cần xác nhận trước khi phát sinh phí. Một năm một symbol có thể nằm trong $25 signup credits nếu đủ điều kiện, nhưng không tự cấp quyền dữ liệu.
- Kaiko cần license riêng cho tính toán/backend trading; license “Display” không đủ.
- Do chưa có license/receipt phi QA phù hợp, chưa được tạo report TRAINING đủ điều kiện, kích hoạt SHADOW/PAPER_GATE hoặc đánh dấu WP06 hoàn tất.

## Cổng quyền bắt buộc trước khi nạp hoặc huấn luyện

Receipt bất biến cho từng run phải ghim tối thiểu:

1. Provider, sản phẩm/gói, venue, loại hợp đồng (ví dụ Binance USDⓈ-M Futures), symbol, interval và khoảng thời gian.
2. Bản thỏa thuận/license có hiệu lực khi truy cập dữ liệu; URL/version/ngày kiểm tra; mã hợp đồng/order hoặc giấy phép.
3. Xác nhận bằng văn bản rằng phạm vi cho phép đọc/lưu dữ liệu một năm, backtest, TRAINING/tái TRAINING, giữ model weights/calibration/report/derived outputs và dùng trong PAPER/SHADOW nội bộ.
4. Hạn dùng, quy tắc revoke/takedown, retention/xóa dữ liệu, attribution/share-alike và quyền lưu artifact sau khi gói hết hạn.
5. Hash của bằng chứng và manifest/snapshot dữ liệu; workspace, actor, purpose và thời điểm recheck. Revoke hoặc thay đổi điều khoản phải chặn run kế tiếp và xử lý theo điều khoản lưu/xóa.

Không chấp nhận README license của mã downloader thay cho license của dữ liệu; không tự điền đồng ý điều khoản thay người sở hữu dữ liệu; không hạ ngưỡng policy để vượt qua thiếu dữ liệu hoặc quyền.

## Nguồn chính thức đã kiểm tra

- Binance Public Data README (định dạng/nguồn archive; README dẫn riêng sang điều khoản dữ liệu): <https://github.com/binance/binance-public-data/blob/master/README.md>
- Binance Vision Dataset Terms v1.0, công bố cập nhật 2026-08-26: <https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md>
- Binance USDⓈ-M developer documentation (API mô tả truy cập market data, không phải license archive): <https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures>
- CoinAPI usage policy: <https://www.coinapi.io/usage-policy>
- CoinAPI customer agreement: <https://www.coinapi.io/legal>
- CoinAPI Market Data API pricing: <https://www.coinapi.io/products/market-data-api/pricing>
- CoinAPI Market Data API FAQ (coverage, exchange ID, credits/billing): <https://www.coinapi.io/products/market-data-api/faq>
- CoinAPI OHLCV historical endpoint: <https://www.coinapi.io/products/market-data-api/docs/rest-api/ohlcv/ohlcv/symbol_id/history/get>
- CoinAPI flat-files history FAQ: <https://www.coinapi.io/products/flat-files/faq>
- Kaiko exchange agreements: <https://www.kaiko.com/exchange-agreements>
- Kaiko data products: <https://www.kaiko.com/products/l1-l2-data>
- Kaiko display-license scope: <https://www.kaiko.com/resources/display>

## Việc còn cần

- Người dùng đã chọn ledger entry-hold riêng; thiết kế chi tiết đang chờ owner duyệt. Cần license/receipt áp dụng cho đúng mục đích nội bộ trước khi tải/training. CoinAPI public pricing đã được nghiên cứu nhưng chưa có báo giá theo yêu cầu cụ thể; chưa gửi yêu cầu ra ngoài, chưa tạo account/API key và chưa phát sinh chi phí.
- Khi có bằng chứng, triển khai provider/evidence adapter nếu cần, nhập nguồn non-QA, tạo run mới với receipt theo run, chạy đủ TRAINING → TEST proof/report theo policy `wp06-scorer-test-v1`, rồi mới kiểm SHADOW, PAPER_GATE riêng, veto/abstain, rollback/restart, history retention và zero-side-effect.
- Sau journey hợp lệ, chạy independent integrated security review. Giữ active modes khóa trong mọi môi trường sản phẩm cho tới khi các cổng này đạt.
