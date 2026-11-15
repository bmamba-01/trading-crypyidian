// Nhóm trang Vận hành (đóng toàn bộ vị thế, Telegram): tăng cường tiến dần, CSP `default-src 'self'`.
//
// - Sao chép: `[data-ops-copy]` với `data-copy` (giá trị) hoặc `data-copy-from` (CSS selector, lấy `href`).
//   `data-copy-secret` = giá trị bí mật (deep link có mã một lần): không đưa vào toast, không ghi log,
//   không lưu storage.
// - Đếm ngược: `[data-ops-countdown="<ISO>"]`; hết hạn thì hiện `[data-ops-expired-notice]`.
//   Dùng đồng hồ trình duyệt nên chỉ để cảnh báo; máy chủ vẫn là nơi quyết định hết hạn.
// - Cửa sổ MFA: `[data-ops-mfa-at]` + `data-ops-mfa-window` (giây) → chip còn/hết hiệu lực.
// - Trạng thái thao tác: `[data-ops-live]` chưa kết thúc thì tải lại vùng này mỗi 5 giây (fetch
//   cùng URL, thay nút DOM; không điều hướng trang).
// - Chống bấm đúp: form `[data-ops-submit-once]` khóa nút gửi sau lần gửi đầu.
(() => {
  "use strict";

  const toast = (options) => window.crypidianToast?.show(options);

  // ---- Sao chép -------------------------------------------------------------------------------
  const copyValue = (button) => {
    const selector = button.dataset.copyFrom;
    if (selector) {
      const source = document.querySelector(selector);
      return source ? source.getAttribute("href") || source.textContent || "" : "";
    }
    return button.dataset.copy || "";
  };
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-ops-copy]");
    if (!button) return;
    const value = copyValue(button).trim();
    if (!value) return;
    const secret = button.hasAttribute("data-copy-secret");
    try {
      await navigator.clipboard.writeText(value);
      toast({
        tone: "success",
        title: button.dataset.copyDone || "Đã sao chép",
        message: secret ? "Chỉ dán vào Telegram của chính bạn; liên kết dùng được một lần." : value,
        timeout: 3000,
      });
    } catch {
      toast({
        tone: "warning",
        title: "Không sao chép được",
        message: secret
          ? "Trình duyệt chặn bộ nhớ tạm; hãy bấm “Mở Telegram” trên thiết bị này."
          : "Trình duyệt chặn bộ nhớ tạm; chọn mã và sao chép thủ công.",
      });
    }
  });

  // ---- Đếm ngược hết hạn --------------------------------------------------------------------
  const duration = (milliseconds) => {
    const seconds = Math.max(0, Math.ceil(milliseconds / 1000));
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return minutes > 0 ? `${minutes} phút ${String(rest).padStart(2, "0")} giây` : `${rest} giây`;
  };
  for (const element of document.querySelectorAll("[data-ops-countdown]")) {
    const deadline = Date.parse(element.dataset.opsCountdown || "");
    if (!Number.isFinite(deadline)) continue;
    let timer = null;
    const tick = () => {
      const left = deadline - Date.now();
      if (left <= 0) {
        element.textContent = element.dataset.expiredText || "Đã hết hạn";
        element.classList.add("is-expired");
        element.classList.remove("is-urgent");
        for (const notice of document.querySelectorAll("[data-ops-expired-notice]")) notice.hidden = false;
        if (timer !== null) {
          window.clearInterval(timer);
          toast({ tone: "warning", title: "Đã hết hạn", message: element.dataset.expiredText || "Hãy tạo lại để tiếp tục." });
        }
        return false;
      }
      element.textContent = `Còn ${duration(left)}`;
      element.classList.toggle("is-urgent", left < 20000);
      return true;
    };
    if (tick()) timer = window.setInterval(tick, 1000);
  }

  // ---- Cửa sổ MFA gần đây -------------------------------------------------------------------
  for (const chip of document.querySelectorAll("[data-ops-mfa-at]")) {
    const verifiedAt = Date.parse(chip.dataset.opsMfaAt || "");
    const windowMs = Number(chip.dataset.opsMfaWindow || "0") * 1000;
    if (!Number.isFinite(verifiedAt) || windowMs <= 0) continue;
    const render = () => {
      const left = verifiedAt + windowMs - Date.now();
      chip.classList.remove("badge-neutral", "badge-success", "badge-warning", "badge-danger");
      if (left <= 0) {
        chip.classList.add("badge-danger");
        chip.textContent = "Đã quá hạn — cần đăng nhập lại";
        return false;
      }
      const minutes = Math.ceil(left / 60000);
      chip.classList.add(left < 180000 ? "badge-warning" : "badge-success");
      chip.textContent = `Còn khoảng ${minutes} phút`;
      return true;
    };
    if (render()) {
      const timer = window.setInterval(() => {
        if (!render()) window.clearInterval(timer);
      }, 15000);
    }
  }

  // ---- Chống bấm đúp -------------------------------------------------------------------------
  for (const form of document.querySelectorAll("form[data-ops-submit-once]")) {
    form.addEventListener("submit", (event) => {
      const button = event.submitter || form.querySelector("button[type=submit]");
      if (form.dataset.opsSubmitted === "1") {
        event.preventDefault();
        return;
      }
      form.dataset.opsSubmitted = "1";
      if (button) {
        button.setAttribute("aria-disabled", "true");
        button.setAttribute("aria-busy", "true");
      }
    });
  }
  // Quay lại bằng nút Back (bfcache) thì mở khóa để người dùng thao tác tiếp.
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    for (const form of document.querySelectorAll("form[data-ops-submit-once]")) {
      delete form.dataset.opsSubmitted;
      for (const button of form.querySelectorAll("[aria-busy]")) {
        button.removeAttribute("aria-busy");
        button.removeAttribute("aria-disabled");
      }
    }
  });

  // ---- Trạng thái thao tác đóng toàn bộ ----------------------------------------------------
  const upgradeCoins = (root) => {
    const coins = window.crypidianCoins;
    if (!coins) return;
    for (const tag of root.querySelectorAll("[data-coin]")) tag.replaceWith(coins.chip(tag.dataset.coin));
  };
  const POLL_MS = 5000;
  const MAX_POLLS = 120; // tối đa ~10 phút rồi dừng, tránh tải vô hạn khi worker không chạy.
  const live = document.querySelector("[data-ops-live]");
  const note = document.querySelector("[data-ops-poll-text]");
  if (live && live.dataset.opsTerminal !== "true") {
    let polls = 0;
    let failures = 0;
    const say = (text) => {
      if (note) note.textContent = text;
    };
    const finish = (region) => {
      const tone = region.dataset.opsTone;
      const status = region.querySelector("#operation-status strong")?.textContent?.trim() || "";
      say("Thao tác đã kết thúc; ngừng tự cập nhật.");
      document.querySelector("[data-ops-poll-note] .pulse-dot")?.remove();
      toast({
        tone: tone === "success" ? "success" : tone === "danger" ? "error" : "warning",
        title: "Thao tác đã kết thúc",
        message: status,
        mirrored: true,
      });
    };
    const poll = async () => {
      if (document.hidden) {
        window.setTimeout(poll, POLL_MS);
        return;
      }
      polls += 1;
      try {
        const response = await fetch(window.location.href, {
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "text/html" },
        });
        if (!response.ok) throw new Error(String(response.status));
        const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
        const next = parsed.querySelector("[data-ops-live]");
        const current = document.querySelector("[data-ops-live]");
        if (!next || !current) throw new Error("missing");
        const region = document.importNode(next, true);
        current.replaceWith(region);
        upgradeCoins(region);
        failures = 0;
        if (region.dataset.opsTerminal === "true") {
          finish(region);
          return;
        }
      } catch {
        failures += 1;
        say("Không tải được trạng thái mới; bấm “Làm mới ngay” để thử lại.");
        if (failures >= 3) return;
      }
      if (polls >= MAX_POLLS) {
        say("Đã ngừng tự cập nhật sau 10 phút; bấm “Làm mới ngay” để xem trạng thái mới nhất.");
        return;
      }
      window.setTimeout(poll, POLL_MS);
    };
    window.setTimeout(poll, POLL_MS);
  }
})();
