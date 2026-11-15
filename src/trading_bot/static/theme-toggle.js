// Nút chuyển chủ đề sáng/tối; phát sự kiện `themechange` cho biểu đồ.
(function() {
  const toggleBtn = document.getElementById("theme-toggle");
  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") || "dark";
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("crypidian_theme", next);
      window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: next } }));
    });
  }
})();
