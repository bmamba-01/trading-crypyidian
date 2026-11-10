# Tài sản giao diện được tự lưu

Kiểm tra nguồn chính thức ngày **2026-11-02**. Ứng dụng chỉ tải CSS/font từ cùng máy chủ, không gọi CDN. Không đóng gói JavaScript Bootstrap.

- Bootstrap **5.3.8**, bản CSS biên dịch chính thức: https://raw.githubusercontent.com/twbs/bootstrap/v5.3.8/dist/css/bootstrap.min.css ; tài liệu phiên bản: https://getbootstrap.com/docs/5.3/getting-started/download/ ; thiết kế ưu tiên điện thoại: https://getbootstrap.com/docs/5.3/layout/breakpoints/ . Giấy phép MIT, Copyright 2011–2025 The Bootstrap Authors; giữ nguyên `vendor/bootstrap-LICENSE.txt` và phần ghi công đầu CSS.
- TradingView Lightweight Charts **5.2.1**, standalone production build từ npm package chính thức: https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-5.2.1.tgz ; nguồn tài liệu: https://tradingview.github.io/lightweight-charts/ . Apache-2.0; giữ `vendor/lightweight-charts-LICENSE.txt` và `vendor/lightweight-charts-NOTICE.txt`; trang chart có liên kết ghi công TradingView. Không dùng CDN hay feed chart bên thứ ba.
- Be Vietnam Pro: tác giả Lâm Bảo, Tony Le, ViệtAnh Nguyễn; Copyright 2021 The Be Vietnam Pro Project Authors. Giữ nguyên SIL Open Font License 1.1 trong `fonts/OFL.txt` và thông tin nguồn `fonts/METADATA.pb`. Nguồn chính thức Google Fonts: https://github.com/google/fonts/tree/main/ofl/bevietnampro ; chỉ dùng ba kiểu thẳng Regular 400, SemiBold 600, Bold 700, toàn bộ bộ ký tự gốc (459 mã Unicode mỗi kiểu, gồm tiếng Việt và Latin). Không cắt tập ký tự. Chuyển TTF sang WOFF2 để tự lưu; không đổi nét chữ, tên font hoặc bảng ký tự. Font dự phòng: system-ui, sans-serif.

Phiên bản thư mục nguồn Google Fonts được đối chiếu: `c13390277dfb1fcc7415d84ef7ef9cfc1e52f8c1`. Tệp TTF gốc tương ứng: `BeVietnamPro-Regular.ttf`, `BeVietnamPro-SemiBold.ttf`, `BeVietnamPro-Bold.ttf` tại `https://raw.githubusercontent.com/google/fonts/c13390277dfb1fcc7415d84ef7ef9cfc1e52f8c1/ofl/bevietnampro/`. Công cụ chuyển đổi: FontTools với hỗ trợ WOFF2/Brotli (lệnh và phiên bản trong báo cáo UI).

## SHA-256 các tệp được đóng gói

- `fonts/METADATA.pb`: `2ef4a8f6b3a18698c70d2e3adf0cf45debc71a4220941ccdf8280db7d1d567f4`.
- `fonts/OFL.txt`: `6b7f8f73609a25ea78c891e34cf37b06f8a676b7ea986e941e43b009110f2a85`.
- `fonts/be-vietnam-pro-400.woff2`: `f22708ed91724aee5e14e342a1099f3ae47d108757fd8bd67bf98d73a360fbdc`.
- `fonts/be-vietnam-pro-600.woff2`: `dc1023d5f8fff0acbfda95614690aa2a697a1e46593e186d0a1eef706e0fbd24`.
- `fonts/be-vietnam-pro-700.woff2`: `efa50136caa95f0ca33ce9c6ba09ddd4115206b775e568a8df36a0a9e0dd05e1`.
- `vendor/bootstrap-5.3.8.min.css`: `d85327d99c7a3ee1f9b5d0500d1370acea3ad2db39c163c2f51f232baedbdede`.
- `vendor/bootstrap-LICENSE.txt`: `4620c84ad5ce8602ff65640ed6b7c8b78ebb9e036584f0ebc1ccc88206a4bb51`.
- `vendor/lightweight-charts-5.2.1.standalone.production.js`: `e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf`.
- `vendor/lightweight-charts-LICENSE.txt`: `70c9d5382506dd184465425c08a99ad9bd6d9ac1313c252968ba0b585e5ef823`.
- `vendor/lightweight-charts-NOTICE.txt`: `f76c6afab94884448f0426e30d6e9d555ca7247894cd3484e477d2f87513036e`.
