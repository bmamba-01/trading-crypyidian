// Toast thông báo dùng chung cho Admin Web (CSP: không script/style nội tuyến).
//
// - `window.crypidianToast.show({ tone, title, message, timeout })` hiện một toast.
//   tone: "error" | "warning" | "success" | "info". timeout = 0 nghĩa là không tự đóng.
// - Phần tử máy chủ dựng có `data-toast-tone`, `data-toast-title`, `data-toast-message` được
//   chuyển thành toast khi trang tải. Thông báo lỗi chính vẫn nằm trong khung `role="alert"`
//   cạnh form, nên toast mirror được đánh dấu `aria-hidden` để trình đọc màn hình không đọc lặp.
(() => {
  "use strict";

  const TONES = new Set(["error", "warning", "success", "info"]);
  const ICONS = {
    error: "M12 8v5M12 16h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z",
    warning: "M12 8v5M12 16h.01M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z",
    success: "m8 12 3 3 5-6M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z",
    info: "M12 11v5M12 8h.01M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z",
  };
  const SVG_NS = "http://www.w3.org/2000/svg";

  const region = () => {
    let element = document.getElementById("toast-region");
    if (!element) {
      element = document.createElement("div");
      element.id = "toast-region";
      element.className = "toast-region";
      element.setAttribute("role", "status");
      element.setAttribute("aria-live", "polite");
      element.setAttribute("aria-label", "Thông báo");
      document.body.appendChild(element);
    }
    return element;
  };

  const icon = (tone) => {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("class", "app-toast__icon");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", ICONS[tone]);
    svg.appendChild(path);
    return svg;
  };

  const dismiss = (toast) => {
    if (!toast.isConnected || toast.dataset.leaving) return;
    toast.dataset.leaving = "1";
    toast.classList.add("app-toast--leaving");
    window.setTimeout(() => toast.remove(), 180);
  };

  const show = ({ tone = "info", title = "", message = "", timeout, mirrored = false } = {}) => {
    const safeTone = TONES.has(tone) ? tone : "info";
    const toast = document.createElement("div");
    toast.className = `app-toast app-toast--${safeTone}`;
    toast.dataset.toastTone = safeTone;
    if (mirrored) toast.setAttribute("aria-hidden", "true");

    toast.appendChild(icon(safeTone));
    const body = document.createElement("div");
    body.className = "app-toast__body";
    if (title) {
      const heading = document.createElement("p");
      heading.className = "app-toast__title";
      heading.textContent = title;
      body.appendChild(heading);
    }
    if (message) {
      const text = document.createElement("p");
      text.className = "app-toast__message";
      text.textContent = message;
      body.appendChild(text);
    }
    toast.appendChild(body);

    const close = document.createElement("button");
    close.type = "button";
    close.className = "app-toast__close";
    close.textContent = "Đóng";
    if (mirrored) close.tabIndex = -1;
    close.addEventListener("click", () => dismiss(toast));
    toast.appendChild(close);

    region().appendChild(toast);
    // Lỗi giữ lâu hơn để kịp đọc; 0 = chỉ đóng bằng tay.
    const delay = timeout ?? (safeTone === "error" ? 12000 : 6000);
    if (delay > 0) {
      let timer = window.setTimeout(() => dismiss(toast), delay);
      // Rê chuột hoặc focus vào toast thì tạm dừng đếm giờ (WCAG 2.2.1).
      const pause = () => window.clearTimeout(timer);
      const resume = () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => dismiss(toast), 4000);
      };
      toast.addEventListener("mouseenter", pause);
      toast.addEventListener("mouseleave", resume);
      toast.addEventListener("focusin", pause);
      toast.addEventListener("focusout", resume);
    }
    return toast;
  };

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const latest = document.querySelector("#toast-region .app-toast:last-child");
    if (latest) dismiss(latest);
  });

  window.crypidianToast = { show, dismiss };

  const fromServer = () => {
    for (const source of document.querySelectorAll("[data-toast-message]")) {
      show({
        tone: source.dataset.toastTone,
        title: source.dataset.toastTitle,
        message: source.dataset.toastMessage,
        mirrored: source.hasAttribute("data-toast-mirrored"),
      });
    }
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fromServer);
  else fromServer();
})();
