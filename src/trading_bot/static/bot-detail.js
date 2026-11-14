// Trang chi tiết bot: cập nhật trạng thái mỗi 5 giây, sao chép mã và gắn nhãn ca sai lệch.
//
// Máy chủ dựng toàn bộ số liệu khi tải trang. Vòng cập nhật chỉ thay các trường trạng thái
// vận hành (nhãn tiếng Việt từ `#bot-status-labels`) và số đếm; khi phát hiện lệnh/vị thế mới
// hoặc phiên bản điều khiển đổi thì báo người dùng tải lại thay vì tự dựng bảng ở trình duyệt.
"use strict";

(() => {
  const readJson = (id, fallback) => {
    const element = document.getElementById(id);
    if (!element) return fallback;
    try {
      return JSON.parse(element.textContent);
    } catch {
      return fallback;
    }
  };
  const pageConfig = readJson("bot-detail-config", null);
  if (!pageConfig) return;
  const labels = readJson("bot-status-labels", {});
  const label = (code) => labels[code] || "Chưa xác định";
  const botId = pageConfig.bot_id;
  const POLL_MS = 5000;

  const pulseDot = document.getElementById("live-pulse-dot");
  const updatedAt = document.getElementById("live-updated-at");
  const toggleBtn = document.getElementById("toggle-realtime-btn");
  const statusMsg = document.getElementById("live-status-msg");
  const staleNotice = document.getElementById("bot-live-stale");

  let timer = null;
  let isPolling = true;
  let stoppedByError = false;
  let inFlight = false;
  let currentFp = null;

  const setText = (id, value) => {
    const element = document.getElementById(id);
    if (element && element.textContent !== value) element.textContent = value;
  };

  // Báo dữ liệu trên trang đã cũ; không tự sửa biểu mẫu (giữ kiểm tra phiên bản lạc quan).
  const markStale = (message) => {
    if (!staleNotice) return;
    const text = staleNotice.querySelector("[data-stale-text]");
    if (text) text.textContent = message;
    staleNotice.hidden = false;
  };

  const applyBot = (bot) => {
    if (!bot) return;
    setText("bot-state-summary", `${label(bot.status)} / ${label(bot.account_status)} / ${label(bot.account_mode)}`);
    setText("bot-readiness", label(bot.readiness));
    setText("bot-entry-state", label(bot.entry_state));
    setText("bot-entry-allowed", bot.entry_allowed ? "Có" : "Không");
    setText("bot-control-revision", String(bot.control_revision));
    if (Number(bot.control_revision) !== Number(pageConfig.control_revision)) {
      markStale(`Trạng thái điều khiển đã đổi (phiên bản ${bot.control_revision}). Tải lại trang trước khi gửi thao tác.`);
    }
  };

  const applyMetrics = (metrics, positions) => {
    if (metrics) {
      setText("kpi-open-positions", String(metrics.open_positions_count ?? "—"));
      setText("kpi-closed-trades", String(metrics.closed_trade_count ?? "—"));
      const changed =
        (pageConfig.open_positions_count !== null && metrics.open_positions_count !== pageConfig.open_positions_count) ||
        (pageConfig.closed_trade_count !== null && metrics.closed_trade_count !== pageConfig.closed_trade_count);
      if (changed) markStale("Có lệnh hoặc vị thế PAPER mới. Tải lại trang để xem bảng và số liệu cập nhật.");
    }
    if (Array.isArray(positions)) setText("positions-count-badge", String(positions.length));
  };

  const stopWithMessage = (message) => {
    stoppedByError = true;
    clearTimeout(timer);
    timer = null;
    if (pulseDot) pulseDot.className = "pulse-dot pulse-warning pulse-paused";
    if (statusMsg) statusMsg.textContent = message;
    if (toggleBtn) toggleBtn.disabled = true;
  };

  const fetchRealtime = async () => {
    if (inFlight || stoppedByError) return;
    inFlight = true;
    try {
      const url = currentFp
        ? `/admin/api/bots/${encodeURIComponent(botId)}/realtime?fp=${encodeURIComponent(currentFp)}`
        : `/admin/api/bots/${encodeURIComponent(botId)}/realtime`;
      const res = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (res.status === 401) {
        stopWithMessage("Phiên đăng nhập đã hết hạn; tải lại trang để đăng nhập.");
        return;
      }
      if (res.status === 403 || res.status === 404) {
        stopWithMessage("Không còn quyền xem bot này; đã dừng cập nhật.");
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      if (updatedAt && data.server_time) {
        updatedAt.textContent = data.server_time.vn_time || data.server_time.vn || "";
        if (data.server_time.iso) updatedAt.dateTime = data.server_time.iso;
      }
      if (data.fingerprint) currentFp = data.fingerprint;
      if (data.status !== "unchanged") {
        applyBot(data.bot);
        applyMetrics(data.metrics, data.positions);
      }
      if (pulseDot) pulseDot.className = "pulse-dot";
      if (statusMsg) statusMsg.textContent = "";
    } catch {
      if (pulseDot) pulseDot.className = "pulse-dot pulse-warning";
      if (statusMsg) statusMsg.textContent = "Mất kết nối, sẽ thử lại sau 5 giây";
    } finally {
      inFlight = false;
    }
  };

  // Hẹn giờ nối tiếp (không dùng setInterval) để yêu cầu chậm không chồng lên nhau.
  const schedule = () => {
    clearTimeout(timer);
    if (!isPolling || stoppedByError || document.hidden) return;
    timer = setTimeout(async () => {
      await fetchRealtime();
      schedule();
    }, POLL_MS);
  };

  const setToggle = (polling) => {
    isPolling = polling;
    if (toggleBtn) {
      toggleBtn.textContent = polling ? "Tạm dừng" : "Tiếp tục";
      toggleBtn.setAttribute("aria-pressed", polling ? "true" : "false");
    }
    if (pulseDot) pulseDot.classList.toggle("pulse-paused", !polling);
  };

  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      if (isPolling) {
        setToggle(false);
        clearTimeout(timer);
      } else {
        setToggle(true);
        fetchRealtime().then(schedule);
      }
    });
  }

  // Tạm dừng khi tab trình duyệt bị ẩn (Page Visibility API), chạy lại khi hiện.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(timer);
    else if (isPolling) fetchRealtime().then(schedule);
  });

  // Sao chép mã đầy đủ (bot, giao dịch, bản nháp).
  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".bot-page .copy-btn[data-copy]");
    if (!button) return;
    try {
      await navigator.clipboard.writeText(button.dataset.copy || "");
      window.crypidianToast?.show({ tone: "success", title: "Đã sao chép mã", message: button.dataset.copy, timeout: 2500 });
    } catch {
      window.crypidianToast?.show({ tone: "warning", title: "Không sao chép được", message: "Trình duyệt chặn bộ nhớ tạm; chọn mã và sao chép thủ công." });
    }
  });

  // Gắn nhãn ca sai lệch để rà soát huấn luyện lại (máy chủ yêu cầu CSRF và vai trò phù hợp).
  const ingestNegativeSample = async (button) => {
    const text = button.querySelector("[data-label]") || button;
    const original = text.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    text.textContent = "Đang gửi…";
    try {
      const res = await fetch(`/admin/api/trades/${encodeURIComponent(button.dataset.tradeId)}/ingest-negative-sample`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": pageConfig.csrf_token },
        credentials: "same-origin",
        body: JSON.stringify({
          bot_id: botId,
          symbol: button.dataset.symbol || undefined,
          side: button.dataset.side || undefined,
          reason: button.dataset.reason || undefined,
        }),
      });
      if (!res.ok) {
        const reason =
          res.status === 403 ? "Vai trò hiện tại không được gắn nhãn ca hoặc phiên đã hết hạn."
          : res.status === 422 ? "Dữ liệu ca giao dịch không hợp lệ."
          : `Máy chủ trả mã ${res.status}.`;
        throw new Error(reason);
      }
      text.textContent = "Đã ghi nhận (lưu tạm trong phiên chạy)";
      button.classList.add("btn-ingest-ai--done");
      window.crypidianToast?.show({ tone: "success", title: "Đã gắn nhãn ca", message: "Ca được lưu tạm để rà soát; chưa vào tập huấn luyện chính thức.", timeout: 4000 });
    } catch (error) {
      button.disabled = false;
      text.textContent = original;
      window.crypidianToast?.show({ tone: "error", title: "Không gắn nhãn được", message: error instanceof Error && error.message ? error.message : "Lỗi kết nối." });
    } finally {
      button.removeAttribute("aria-busy");
    }
  };

  // Gắn sự kiện bằng addEventListener vì CSP chặn thuộc tính onclick nội tuyến.
  document.querySelectorAll(".btn-ingest-ai[data-trade-id]").forEach((button) => {
    button.addEventListener("click", () => ingestNegativeSample(button));
  });

  fetchRealtime().then(schedule);
})();
