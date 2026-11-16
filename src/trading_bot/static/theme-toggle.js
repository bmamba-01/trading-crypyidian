// Công tắc sáng/tối kiểu iOS: bật = giao diện tối; phát sự kiện `themechange` cho biểu đồ.
(function() {
  const toggleBtn = document.getElementById("theme-toggle");
  if (!toggleBtn) return;
  const sync = () => {
    const current = document.documentElement.getAttribute("data-theme") || "dark";
    toggleBtn.setAttribute("aria-checked", current === "dark" ? "true" : "false");
  };
  sync();
  window.addEventListener("themechange", sync);
  toggleBtn.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme") || "dark";
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("crypidian_theme", next);
    } catch (error) {
      // Trình duyệt chặn lưu trữ: vẫn đổi giao diện cho phiên hiện tại.
    }
    sync();
    window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: next } }));
  });
})();
