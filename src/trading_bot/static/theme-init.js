// Đặt chủ đề sáng/tối trước khi vẽ trang để tránh nháy màu; tệp tĩnh vì CSP chặn script nội tuyến.
(function() {
  const saved = localStorage.getItem("crypidian_theme");
  const theme = saved || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", theme);
})();
