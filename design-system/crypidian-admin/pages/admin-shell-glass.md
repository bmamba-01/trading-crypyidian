# Ghi đè cho Admin Web: khung ứng dụng kính mờ (2026-11-14)

Trang này ghi đè `../MASTER.md` cho mọi trang Admin Web. Nguồn: yêu cầu của owner ngày
14/11/2026 ("revamp toàn bộ dashboard theo phong cách glassmorphism mới nhất, tối giản, tiết
kiệm không gian; sidebar thay navbar; dùng tab; section thu gọn") và kết quả skill
`ui-ux-pro-max` (`--design-system ... --density 8 --variance 4 --motion 3`, style
`glassmorphism`).

## Quyết định

| Hạng mục | MASTER đề xuất | Admin Web dùng | Lý do |
| --- | --- | --- | --- |
| Phong cách | Dark Mode (OLED) | Kính mờ trên nền tối xanh navy có quầng sáng xanh lá/xanh dương; có chế độ sáng | Owner yêu cầu glassmorphism; giữ nền tối làm mặc định theo MASTER |
| Font | Fira Code / Fira Sans (Google Fonts) | Be Vietnam Pro tự host; số dùng `ui-monospace` + `tabular-nums` | CSP `default-src 'self'` chặn Google Fonts; Be Vietnam Pro hỗ trợ dấu tiếng Việt |
| Mật độ | Dense (8–32px) | Khoảng cách 4/8/12/16/24px | Tiết kiệm không gian |
| Điều hướng | — | Sidebar trái nhóm theo chức năng, thu thành dải biểu tượng trên desktop, ngăn kéo trên di động | Owner yêu cầu sidebar; 14 mục không vừa thanh ngang |
| Nội dung dài | — | Tab (WAI-ARIA tabs) cho dashboard; `details` thu gọn cho khối phụ | Owner yêu cầu tab và gom gọn |
| Chuyển động | Subtle | 150–200ms, tắt theo `prefers-reduced-motion`; không dùng GSAP | Không thêm thư viện ngoài |

## Token kính mờ

- Nền trang: gradient navy `#020617 → #0b1224` + hai quầng `radial-gradient` (xanh lá `#22c55e` 10–14%, xanh dương `#3b82f6` 10–12%).
- Bề mặt kính: `rgba(15, 23, 42, 0.55)` (tối) / `rgba(255, 255, 255, 0.68)` (sáng), `backdrop-filter: blur(14px) saturate(140%)`, viền 1px `rgba(148, 163, 184, 0.18)`.
- Văn bản trên kính đạt tương phản ≥ 4,5:1 (nền đã đủ tối/sáng sau khi trộn); trình duyệt không hỗ trợ `backdrop-filter` dùng nền đặc.

## Kiểm tra trước khi giao

- Không emoji làm biểu tượng; SVG nét 1.75px.
- Mọi điều khiển ≥ 44px, focus nhìn thấy (outline thật).
- Không tràn ngang ở 360/390/768/1024/1440px.
- Không vi phạm CSP (không script/style nội tuyến, không `data:`).
