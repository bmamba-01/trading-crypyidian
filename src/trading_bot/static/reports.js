"use strict";
// Trang báo cáo: đổi phạm vi, chọn nhanh khoảng thời gian, sao chép mã và gắn nhãn hậu kiểm.
// Mọi sự kiện gắn bằng addEventListener vì CSP chặn thuộc tính onclick/onchange nội tuyến.
(() => {
  const toast = (options) => window.crypidianToast?.show(options);

  // Phạm vi "toàn không gian làm việc" không dùng bot: ẩn ô chọn bot (máy chủ cũng ẩn sẵn).
  const scope = document.getElementById("scope");
  const botWrap = document.getElementById("bot-select-wrap");
  const syncScope = () => {
    if (scope && botWrap) botWrap.hidden = scope.value === "workspace";
  };
  if (scope) {
    scope.addEventListener("change", syncScope);
    syncScope();
  }

  // Mốc ISO-8601 theo giờ máy người dùng, có offset múi giờ để máy chủ không phải đoán.
  const isoWithOffset = (date) => {
    const pad = (n) => String(n).padStart(2, "0");
    const offset = -date.getTimezoneOffset();
    const sign = offset >= 0 ? "+" : "-";
    return (
      `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
      `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
      `${sign}${pad(Math.floor(Math.abs(offset) / 60))}:${pad(Math.abs(offset) % 60)}`
    );
  };
  const setQuickRange = (type) => {
    const now = new Date();
    let start = new Date(now);
    if (type === "today") start.setHours(0, 0, 0, 0);
    else if (type === "7d") start.setDate(now.getDate() - 7);
    else if (type === "30d") start.setDate(now.getDate() - 30);
    else if (type === "month") start = new Date(now.getFullYear(), now.getMonth(), 1);
    const startInput = document.getElementById("start");
    const endInput = document.getElementById("end");
    if (startInput) startInput.value = isoWithOffset(start);
    if (endInput) endInput.value = isoWithOffset(now);
    document.querySelectorAll(".quick-filter-btn[data-quick-range]").forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.quickRange === type ? "true" : "false");
    });
  };
  document.querySelectorAll(".quick-filter-btn[data-quick-range]").forEach((button) => {
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => setQuickRange(button.dataset.quickRange));
  });

  // Nút sao chép mã (ID giao dịch, mã băm báo cáo); mã đầy đủ vẫn có trong văn bản trang.
  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".report-page .copy-btn[data-copy]");
    if (!button) return;
    try {
      await navigator.clipboard.writeText(button.dataset.copy || "");
      toast({ tone: "success", title: "Đã sao chép", message: button.dataset.copy, timeout: 2500 });
    } catch {
      toast({
        tone: "warning",
        title: "Không sao chép được",
        message: "Trình duyệt chặn bộ nhớ tạm; chọn mã và sao chép thủ công.",
      });
    }
  });

  // Gắn nhãn ca hậu kiểm: gửi kèm số liệu thật của lệnh đang xem (không điền mặc định).
  const ingest = document.getElementById("btn-ingest-ai");
  const feedback = document.getElementById("ai-ingest-feedback");
  const readPayload = () => {
    const source = document.getElementById("ingest-trade-payload");
    if (!source) return {};
    try {
      const parsed = JSON.parse(source.textContent || "{}");
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return {};
    }
  };
  if (ingest) {
    ingest.addEventListener("click", async () => {
      const data = ingest.dataset;
      const body = { bot_id: data.botId, symbol: data.symbol, side: data.side, reason: data.reason || "" };
      for (const [key, value] of Object.entries(readPayload())) {
        if (value !== null && value !== undefined && value !== "") body[key] = value;
      }
      ingest.disabled = true;
      ingest.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(`/admin/api/trades/${encodeURIComponent(data.tradeId)}/ingest-negative-sample`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": data.csrfToken || "" },
          credentials: "same-origin",
          body: JSON.stringify(body),
        });
        if (response.ok) {
          ingest.hidden = true;
          if (feedback) feedback.hidden = false;
          toast({ tone: "success", title: "Đã ghi nhận ca để rà soát", message: "Nhãn chỉ lưu tạm trong phiên chạy." });
          return;
        }
        let detail = "Không thể đưa ca sai lệch vào dữ liệu phạt AI lúc này.";
        try {
          const payload = await response.json();
          if (payload && typeof payload.detail === "string") detail = payload.detail;
        } catch {
          // Phản hồi không phải JSON: giữ thông báo chung.
        }
        toast({ tone: "error", title: `Không gắn nhãn được (HTTP ${response.status})`, message: detail });
        ingest.disabled = false;
      } catch {
        toast({ tone: "error", title: "Lỗi kết nối", message: "Không gửi được yêu cầu gắn nhãn; thử lại sau." });
        ingest.disabled = false;
      } finally {
        ingest.removeAttribute("aria-busy");
      }
    });
  }
})();
