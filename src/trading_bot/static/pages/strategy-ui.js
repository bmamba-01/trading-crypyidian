"use strict";

// Hành vi dùng chung cho nhóm trang Chiến lược và Backtest (Thẻ chiến lược, Microscope,
// Kiểm thử dữ liệu lịch sử). Không có mã nội tuyến trong template để tuân thủ CSP:
// - `[data-dialog-open="<id>"]` mở `<dialog class="glass-dialog">` bằng showModal();
//   bấm ra nền mờ thì đóng.
// - `button.copy-btn[data-copy]` sao chép giá trị đầy đủ và báo bằng toast.
// - `input[data-percent-hint="<id>"]` hiện tỷ lệ thập phân dưới dạng phần trăm cho dễ đọc.
// - `form[data-submit-guard]` khóa nút gửi sau lần bấm đầu để tránh gửi trùng.
(() => {
  const toast = (options) => {
    if (window.crypidianToast && typeof window.crypidianToast.show === "function") {
      window.crypidianToast.show(options);
    }
  };

  document.addEventListener("click", async (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;

    const opener = target.closest("[data-dialog-open]");
    if (opener) {
      const dialog = document.getElementById(opener.getAttribute("data-dialog-open") || "");
      if (dialog instanceof HTMLDialogElement && !dialog.open) dialog.showModal();
      return;
    }

    const copy = target.closest("button.copy-btn[data-copy]");
    if (copy) {
      const value = copy.getAttribute("data-copy") || "";
      try {
        await navigator.clipboard.writeText(value);
        toast({ tone: "success", title: "Đã sao chép", message: value, timeout: 2500 });
      } catch {
        toast({
          tone: "warning",
          title: "Không sao chép được",
          message: "Trình duyệt chặn bộ nhớ tạm; hãy chọn mã và sao chép thủ công.",
        });
      }
    }
  });

  // Bấm vào nền mờ (ngoài khung hộp thoại) thì đóng; bấm trong nội dung thì giữ nguyên.
  for (const dialog of document.querySelectorAll("dialog.glass-dialog")) {
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      const inside =
        event.clientX >= box.left &&
        event.clientX <= box.right &&
        event.clientY >= box.top &&
        event.clientY <= box.bottom;
      if (!inside) dialog.close();
    });
  }

  // Tỷ lệ thập phân → phần trăm theo định dạng Việt Nam (0,01 → "= 1%").
  const percentFormat = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 4 });
  for (const input of document.querySelectorAll("input[data-percent-hint]")) {
    const output = document.getElementById(input.getAttribute("data-percent-hint") || "");
    if (!output) continue;
    const update = () => {
      const raw = String(input.value || "").trim();
      const value = raw === "" ? Number.NaN : Number(raw);
      output.textContent = Number.isFinite(value) ? `= ${percentFormat.format(value * 100)}%` : "";
    };
    input.addEventListener("input", update);
    update();
  }

  for (const form of document.querySelectorAll("form[data-submit-guard]")) {
    form.addEventListener("submit", (event) => {
      const button = event.submitter instanceof HTMLButtonElement ? event.submitter : form.querySelector("button[type=submit]");
      if (!button) return;
      if (form.dataset.submitting === "true") {
        event.preventDefault();
        return;
      }
      form.dataset.submitting = "true";
      button.setAttribute("aria-busy", "true");
      // Khóa sau khi trình duyệt đã chụp dữ liệu biểu mẫu để không làm mất lượt gửi.
      window.setTimeout(() => {
        button.disabled = true;
        button.dataset.guardDisabled = "true";
      }, 0);
    });
  }
  // Quay lại trang bằng nút Back (bfcache) thì mở khóa nút gửi.
  window.addEventListener("pageshow", () => {
    for (const form of document.querySelectorAll("form[data-submit-guard]")) {
      delete form.dataset.submitting;
      // Chỉ mở lại nút do bộ khóa này khóa; nút bị máy chủ tắt sẵn (thiếu dữ liệu) giữ nguyên.
      for (const button of form.querySelectorAll("button[data-guard-disabled]")) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        delete button.dataset.guardDisabled;
      }
    }
  });
})();
