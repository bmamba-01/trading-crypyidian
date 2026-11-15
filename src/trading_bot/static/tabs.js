// Tab theo mẫu WAI-ARIA (https://www.w3.org/WAI/ARIA/apg/patterns/tabs/), kích hoạt thủ công.
//
// Cấu trúc: `[data-tabs]` chứa `[role=tablist]` với các `button[role=tab][aria-controls]` và
// các `[role=tabpanel]`. Không có JS thì mọi panel đều hiện (không gắn `hidden` sẵn trong HTML).
// Tab đang chọn được ghi vào hash (#tab-<id>) để chia sẻ link và giữ khi tải lại.
// Khi panel hiện ra, phát sự kiện `tabs:shown` để biểu đồ trong panel tự tính lại kích thước.
(() => {
  "use strict";

  // Nhớ tab theo từng trang trong phiên trình duyệt: sau khi gửi form (máy chủ chuyển hướng
  // về trang gốc, mất hash) người dùng vẫn ở đúng tab vừa thao tác.
  const storageKey = () => `crypidian.tabs.${window.location.pathname}`;
  const remembered = () => {
    try {
      return window.sessionStorage.getItem(storageKey());
    } catch {
      return null;
    }
  };

  const activate = (container, tab, { focus = false, updateHash = true } = {}) => {
    const tabs = [...container.querySelectorAll('[role="tab"]')];
    for (const item of tabs) {
      const selected = item === tab;
      item.setAttribute("aria-selected", selected ? "true" : "false");
      item.tabIndex = selected ? 0 : -1;
      const panel = document.getElementById(item.getAttribute("aria-controls"));
      if (panel) panel.hidden = !selected;
    }
    if (focus) tab.focus();
    const panel = document.getElementById(tab.getAttribute("aria-controls"));
    if (panel) panel.dispatchEvent(new CustomEvent("tabs:shown", { bubbles: true }));
    window.dispatchEvent(new Event("resize"));
    if (tab.id) {
      try {
        window.sessionStorage.setItem(storageKey(), tab.id);
      } catch {
        // Bộ nhớ phiên bị chặn: bỏ qua, chỉ mất khả năng nhớ tab.
      }
    }
    if (updateHash && tab.id) {
      try {
        window.history.replaceState(null, "", `#${tab.id}`);
      } catch {
        // replaceState bị chặn (ví dụ file://): bỏ qua.
      }
    }
  };

  for (const container of document.querySelectorAll("[data-tabs]")) {
    const tabs = [...container.querySelectorAll('[role="tab"]')];
    if (!tabs.length) continue;
    container.classList.add("tabs--ready");

    for (const tab of tabs) {
      tab.addEventListener("click", () => activate(container, tab));
      tab.addEventListener("keydown", (event) => {
        const index = tabs.indexOf(tab);
        let next = null;
        if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
        else if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
        else if (event.key === "Home") next = tabs[0];
        else if (event.key === "End") next = tabs[tabs.length - 1];
        if (next) {
          event.preventDefault();
          next.focus();
        }
      });
    }

    const fromHash = tabs.find((tab) => `#${tab.id}` === window.location.hash);
    const preselected = tabs.find((tab) => tab.getAttribute("aria-selected") === "true");
    const fromSession = tabs.find((tab) => tab.id && tab.id === remembered());
    activate(container, fromHash || fromSession || preselected || tabs[0], { updateHash: false });

    // Phần tử trong panel ẩn được nhắm tới bằng hash (#kill-reason…) thì mở đúng tab.
    const revealTarget = () => {
      const id = window.location.hash.slice(1);
      if (!id) return;
      const target = document.getElementById(id);
      const panel = target && target.closest('[role="tabpanel"]');
      const owner = panel && tabs.find((tab) => tab.getAttribute("aria-controls") === panel.id);
      if (owner && panel.hidden) activate(container, owner, { updateHash: false });
    };
    revealTarget();
    window.addEventListener("hashchange", revealTarget);
  }
})();
