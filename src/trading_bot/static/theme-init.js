// Đặt chủ đề sáng/tối trước khi vẽ trang để tránh nháy màu; tệp tĩnh vì CSP chặn script nội tuyến.
(function() {
  const KEY = "crypidian_theme";
  const mq = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const sync = () => {
    let saved = null;
    try {
      saved = localStorage.getItem(KEY);
    } catch {
      saved = null;
    }
    const theme = saved || (mq && mq.matches ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
    return theme;
  };
  sync();
  const handleMediaChange = () => {
    const theme = sync();
    window.dispatchEvent(new CustomEvent("themechange", { detail: { theme } }));
  };
  if (mq && typeof mq.addEventListener === "function") {
    mq.addEventListener("change", handleMediaChange);
  } else if (mq && typeof mq.addListener === "function") {
    mq.addListener(handleMediaChange);
  }
})();
