# Khảo sát dashboard bot và CSS framework — 2026-11-02

## Mục tiêu

Đối chiếu cách các sản phẩm dashboard bot/quant trình bày bot đang hoạt động và hiệu suất, rồi chọn hướng giao diện phù hợp Admin Web hiện tại. Đây là khảo sát giao diện và kiến trúc; số liệu dashboard phải lấy từ ledger có nguồn gốc rõ, không sao chép con số hoặc giả định lợi nhuận từ sản phẩm tham khảo.

## Sản phẩm tham khảo

- **Entrade — Dashboard**: tài liệu tiếng Việt mô tả danh sách chiến thuật đang chạy, lọc môi trường thật/ảo, thông tin bot và biểu đồ tài sản. Học cách đặt filter môi trường và làm nổi danh sách bot hoạt động; không dùng mô hình nạp/rút hoặc order control của họ làm mặc định vì khác phạm vi/safety của Crypidian. [Tài liệu Entrade](https://hdsd.entrade.com.vn/chuc-nang-avatar/dashboard), kiểm tra 2026-11-02.
- **Binance Trading Bot PnL**: dashboard PnL của Binance có PnL tích lũy/theo ngày, lợi nhuận/lỗ đã đóng, win rate, duration và phân bổ; Binance cảnh báo dữ liệu có thể trễ/không nhất quán và PnL không bao gồm một số unrealized Futures. Học cách định nghĩa KPI và ghi rõ giới hạn/UTC thay vì chỉ đưa một con số “profit”. [Binance Support tiếng Việt](https://www.binance.com/vi/support/faq/detail/07d9c7a2f2524e8eb4c3baf947826faa), kiểm tra 2026-11-02.
- **FreqUI**: UI mã nguồn mở của Freqtrade có dashboard tổng quan nhiều bot, trạng thái, profit/balance chart và responsive layout. Tài liệu ghi rõ một số lịch sử balance được dựng lại best-effort, không phải hoàn toàn chính xác. Học cách đặt trạng thái, bộ chọn bot và caveat cạnh chart; không phụ thuộc Freqtrade runtime hoặc frontend của họ. [FreqUI docs](https://www.freqtrade.io/en/latest/freq-ui/), kiểm tra 2026-11-02.
- **Binance Trading Bot open-source dashboard**: tài liệu sản phẩm tự công bố bố cục ưu tiên điện thoại: trạng thái worker/halt, PnL đã thực hiện, vị thế/lệnh mở và roster profile; phần demo dùng dữ liệu seed. Đây là reference layout, không phải bằng chứng hiệu suất. [Dashboard guide](https://chrisleekr.github.io/binance-trading-bot/user-guide/dashboard/), kiểm tra 2026-11-02.

## CSS framework

Kết quả “phổ biến nhất” tùy cách đo. State of CSS 2025 có nhiều lượt trả lời Tailwind CSS (2.041) hơn Bootstrap (1.194), trong nhóm respondent tự chọn của khảo sát. W3Techs ngày 2026-08-07 phát hiện Bootstrap chiếm 74,6% trong số site mà họ nhận diện được CSS framework; họ lưu ý một site có thể dùng nhiều framework. Hai số liệu trả lời hai câu hỏi khác nhau, không thể quy thành cùng một thị phần. [State of CSS 2025](https://2025.stateofcss.com/en-US/other-tools/), [W3Techs framework usage](https://w3techs.com/technologies/cross/css_framework/ranking).

**Đề xuất cho repo này: Bootstrap 5.3, tự host CSS/JS tĩnh đã pin.** Admin đang là FastAPI + Jinja SSR, không có frontend build pipeline. Bootstrap cho grid/breakpoint và component dùng trực tiếp trong template; không cần cài Node hoặc đổi kiến trúc sang SPA. Chỉ thêm JS bundle đã pin nếu cần menu mobile; không tải CSS/JS từ CDN lúc chạy. Giữ `admin.css` làm lớp branding và điều chỉnh component, không dùng hai framework chồng lấn. Bootstrap có mobile-first responsive grid theo [tài liệu chính thức 5.3](https://getbootstrap.com/docs/5.3/layout/grid/), kiểm tra 2026-11-02.

Tailwind dẫn đầu survey developer nhưng sẽ cần thêm bước compile/asset vào build Docker nếu dùng trong production; Vue/React/FreqUI tạo tương tác phong phú hơn nhưng thay đổi ranh giới SSR/auth, cần toolchain JS và không phù hợp với yêu cầu “dễ” trong vòng này. Không chọn các giải pháp đó cho đợt dashboard đầu.

## Hiện trạng Crypidian và hướng sản phẩm

`/admin` hiện chỉ hiển thị số lượng bot, số bot readiness READY, workspace kill state và danh sách bot. `/admin/reports` nhận khoảng thời gian/phạm vi và render JSON. `BotReportingService` có snapshot ledger `REPEATABLE READ, READ ONLY`, nhưng report domain hiện pin `mode=PAPER`; totals nhóm theo quote asset, có realized gross/net PnL, fee, funding, slippage, số trade và max drawdown trên realized PnL. Chưa có API equity time-series, chưa có thống kê Demo, và dashboard chưa hiển thị PnL/unrealized position.

Đợt đầu nên giữ Jinja và tái sử dụng reporting/runtime service hiện có; không tạo số liệu mới ở template. Desktop workspace overview có KPI cards + bảng/roster bot; trên mobile chuyển roster thành cards, xếp KPI một cột hoặc hai cột, thu gọn navigation, cho filter xuống dòng và không để bảng rộng làm tràn viewport. Với demo hiện tại, hiển thị “Mô phỏng (PAPER)”; chỉ hiển thị “Binance Futures Demo” sau khi đã kết nối venue thật. Luôn hiện mode, quote asset, khoảng thời gian, thời điểm chụp và trạng thái freshness; stale/unknown/incomplete không được đổi thành zero. Không cộng số giữa mode hoặc quote asset. Chỉ vẽ equity curve sau khi có series authoritative và cùng currency; không nội suy từ report aggregate hoặc khẳng định lợi nhuận khi fee/funding thiếu.

### Nguồn kiểm tra lần này

- [Entrade — Dashboard](https://hdsd.entrade.com.vn/chuc-nang-avatar/dashboard)
- [Binance Support — xem phân tích PnL tài khoản Bot](https://www.binance.com/vi/support/faq/detail/07d9c7a2f2524e8eb4c3baf947826faa)
- [FreqUI — tài liệu dashboard responsive](https://www.freqtrade.io/en/latest/freq-ui/)
- [Binance Trading Bot — dashboard guide](https://chrisleekr.github.io/binance-trading-bot/user-guide/dashboard/)
- [State of CSS 2025 — CSS frameworks](https://2025.stateofcss.com/en-US/other-tools/)
- [W3Techs — CSS framework usage](https://w3techs.com/technologies/cross/css_framework/ranking)
- [Bootstrap 5.3 — responsive grid](https://getbootstrap.com/docs/5.3/layout/grid/)
