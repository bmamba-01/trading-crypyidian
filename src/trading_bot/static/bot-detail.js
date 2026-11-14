// Cập nhật 5 giây và thao tác trên trang chi tiết bot.
"use strict";
const pageConfig = JSON.parse(document.getElementById("bot-detail-config").textContent);
(() => {
  let pollingInterval = null;
  let isPolling = true;
  let currentFp = null;
  const botId = pageConfig.bot_id;
  const pollTime = 5000;

  const pulseDot = document.getElementById("live-pulse-dot");
  const updatedAt = document.getElementById("live-updated-at");
  const toggleBtn = document.getElementById("toggle-realtime-btn");
  const statusMsg = document.getElementById("live-status-msg");

  const fetchRealtime = async () => {
    try {
      const url = currentFp ? `/admin/api/bots/${botId}/realtime?fp=${encodeURIComponent(currentFp)}` : `/admin/api/bots/${botId}/realtime`;
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) throw new Error("Mã lỗi: " + res.status);
      const data = await res.json();

      if (updatedAt) {
        updatedAt.textContent = data.server_time.vn_time || data.server_time.vn;
      }

      if (data.status === "unchanged") {
        if (pulseDot) pulseDot.className = "pulse-dot";
        if (statusMsg) statusMsg.textContent = "";
        return;
      }
      if (data.fingerprint) {
        currentFp = data.fingerprint;
      }

      // Chỉ cập nhật số đếm từ ledger; các chỉ số tiền tệ được dựng phía máy chủ khi tải trang
      // để giữ nguyên định dạng số và màu lãi/lỗ.
      if (data.metrics) {
        const kpiOpen = document.getElementById("kpi-open-positions");
        if (kpiOpen) kpiOpen.textContent = String(data.metrics.open_positions_count ?? "—");
        const kpiClosed = document.getElementById("kpi-closed-trades");
        if (kpiClosed) {
          kpiClosed.textContent = "Tổng số lệnh đóng: " + String(data.metrics.closed_trade_count ?? "—");
        }
      }
      const positionsBadge = document.getElementById("positions-count-badge");
      if (positionsBadge && Array.isArray(data.positions)) {
        positionsBadge.textContent = String(data.positions.length);
      }

      if (pulseDot) {
        pulseDot.className = "pulse-dot";
      }
      if (statusMsg) statusMsg.textContent = "";
    } catch (err) {
      if (pulseDot) pulseDot.className = "pulse-dot pulse-warning";
      if (statusMsg) statusMsg.textContent = "Mất kết nối 5s";
    }
  };

  const startPolling = () => {
    if (pollingInterval) clearInterval(pollingInterval);
    pollingInterval = setInterval(fetchRealtime, pollTime);
    isPolling = true;
    if (toggleBtn) {
      toggleBtn.textContent = "Tạm dừng";
      toggleBtn.setAttribute("aria-pressed", "true");
    }
    if (pulseDot) pulseDot.classList.remove("pulse-paused");
  };

  const stopPolling = () => {
    if (pollingInterval) clearInterval(pollingInterval);
    pollingInterval = null;
    isPolling = false;
    if (toggleBtn) {
      toggleBtn.textContent = "Tiếp tục";
      toggleBtn.setAttribute("aria-pressed", "false");
    }
    if (pulseDot) pulseDot.classList.add("pulse-paused");
  };

  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      if (isPolling) stopPolling();
      else {
        fetchRealtime();
        startPolling();
      }
    });
  }

  // Tự động tạm dừng khi chuyển tab (Page Visibility API)
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (pollingInterval) clearInterval(pollingInterval);
    } else if (isPolling) {
      fetchRealtime();
      startPolling();
    }
  });

  // Kích hoạt lần đầu
  fetchRealtime();
  startPolling();
})();

// Hàm tiếp nhận ca giao dịch vào tập dữ liệu phạt AI
const ingestNegativeSample = async (tradeId, btn) => {
  if (!btn) return;
  const origText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = "⏳ Đang chuyển giao...";

  try {
    const res = await fetch(`/admin/api/trades/${tradeId}/ingest-negative-sample`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": pageConfig.csrf_token },
      credentials: "same-origin",
      body: JSON.stringify({
        bot_id: pageConfig.bot_id,
        symbol: btn.dataset.symbol || pageConfig.symbol,
        side: btn.dataset.side || undefined,
        reason: btn.dataset.reason || undefined,
      }),
    });

    if (!res.ok) throw new Error("Lỗi HTTP: " + res.status);
    const data = await res.json();

    btn.style.background = "#0ecb81";
    btn.style.color = "#ffffff";
    btn.innerHTML = "✅ Đã ghi nhận ca sai lệch (lưu tạm trong phiên chạy)";
  } catch (err) {
    btn.disabled = false;
    btn.innerHTML = "❌ Lỗi: " + err.message;
    setTimeout(() => {
      btn.innerHTML = origText;
    }, 3000);
  }
};

// Gắn sự kiện bằng addEventListener vì CSP chặn thuộc tính onclick nội tuyến.
document.querySelectorAll(".btn-ingest-ai[data-trade-id]").forEach((button) => {
  button.addEventListener("click", () => ingestNegativeSample(button.dataset.tradeId, button));
});
