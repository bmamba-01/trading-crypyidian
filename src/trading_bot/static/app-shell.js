// Khung ứng dụng Admin Web: sidebar thu gọn (desktop) và ngăn kéo điều hướng (di động).
//
// - Desktop (≥ 1024px): nút "Thu gọn" chuyển sidebar thành dải biểu tượng; lựa chọn được nhớ
//   trong localStorage (chỉ là tùy chọn hiển thị, không phải dữ liệu nghiệp vụ).
// - Di động: nút "Menu" mở sidebar dạng ngăn kéo, đóng bằng Escape, nút đóng, bấm nền mờ hoặc
//   chọn một mục. Focus quay về nút mở khi đóng.
(() => {
  "use strict";

  const shell = document.querySelector("[data-app-shell]");
  const sidebar = document.getElementById("app-sidebar");
  if (!shell || !sidebar) return;

  const STORAGE_KEY = "crypidian.sidebar.collapsed";
  const desktop = window.matchMedia("(min-width: 1024px)");
  const collapseButton = document.getElementById("sidebar-collapse");
  const openButton = document.getElementById("sidebar-open");
  const closeButton = document.getElementById("sidebar-close");
  const backdrop = document.getElementById("sidebar-backdrop");

  const readCollapsed = () => {
    try {
      return window.localStorage.getItem(STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  };
  const writeCollapsed = (value) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, value ? "1" : "0");
    } catch {
      // Bộ nhớ bị chặn: vẫn đổi được trong phiên hiện tại.
    }
  };

  const setCollapsed = (collapsed) => {
    shell.classList.toggle("app-shell--collapsed", collapsed);
    if (collapseButton) {
      collapseButton.setAttribute("aria-pressed", collapsed ? "true" : "false");
      const label = collapseButton.querySelector("[data-collapse-label]");
      if (label) label.textContent = collapsed ? "Mở rộng" : "Thu gọn";
    }
  };

  const isDrawerOpen = () => shell.classList.contains("app-shell--drawer-open");
  const openDrawer = () => {
    shell.classList.add("app-shell--drawer-open");
    if (openButton) openButton.setAttribute("aria-expanded", "true");
    sidebar.removeAttribute("inert");
    const firstLink = sidebar.querySelector("a, button");
    if (firstLink) firstLink.focus();
  };
  const closeDrawer = ({ restoreFocus = true } = {}) => {
    if (!isDrawerOpen()) return;
    shell.classList.remove("app-shell--drawer-open");
    if (openButton) openButton.setAttribute("aria-expanded", "false");
    if (!desktop.matches) sidebar.setAttribute("inert", "");
    if (restoreFocus && openButton) openButton.focus();
  };

  const applyViewport = () => {
    if (desktop.matches) {
      shell.classList.remove("app-shell--drawer-open");
      sidebar.removeAttribute("inert");
      setCollapsed(readCollapsed());
    } else {
      setCollapsed(false);
      if (!isDrawerOpen()) sidebar.setAttribute("inert", "");
    }
  };

  if (collapseButton) {
    collapseButton.addEventListener("click", () => {
      const next = !shell.classList.contains("app-shell--collapsed");
      setCollapsed(next);
      writeCollapsed(next);
    });
  }
  if (openButton) openButton.addEventListener("click", openDrawer);
  if (closeButton) closeButton.addEventListener("click", () => closeDrawer());
  if (backdrop) backdrop.addEventListener("click", () => closeDrawer());
  sidebar.addEventListener("click", (event) => {
    if (!desktop.matches && event.target.closest("a")) closeDrawer({ restoreFocus: false });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isDrawerOpen()) closeDrawer();
  });
  desktop.addEventListener("change", applyViewport);
  applyViewport();
  shell.classList.add("app-shell--ready");
})();
