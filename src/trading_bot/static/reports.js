"use strict";
// Bộ lọc nhanh, đổi phạm vi và thao tác hậu kiểm trên trang báo cáo.
function toggleBotSelect(scopeValue) {
  const wrap = document.getElementById("bot-select-wrap");
  if (wrap) {
    wrap.style.display = scopeValue === "workspace" ? "none" : "block";
  }
}

function setQuickRange(type) {
  const now = new Date();
  let startDate = new Date();
  if (type === "today") {
    startDate.setHours(0, 0, 0, 0);
  } else if (type === "7d") {
    startDate.setDate(now.getDate() - 7);
  } else if (type === "30d") {
    startDate.setDate(now.getDate() - 30);
  } else if (type === "month") {
    startDate = new Date(now.getFullYear(), now.getMonth(), 1);
  }

  const formatISOWithTZ = (d) => {
    const pad = (n) => String(n).padStart(2, "0");
    const offsetMinutes = -d.getTimezoneOffset();
    const sign = offsetMinutes >= 0 ? "+" : "-";
    const tzHours = pad(Math.floor(Math.abs(offsetMinutes) / 60));
    const tzMins = pad(Math.abs(offsetMinutes) % 60);
    return (
      d.getFullYear() +
      "-" +
      pad(d.getMonth() + 1) +
      "-" +
      pad(d.getDate()) +
      "T" +
      pad(d.getHours()) +
      ":" +
      pad(d.getMinutes()) +
      ":" +
      pad(d.getSeconds()) +
      sign +
      tzHours +
      ":" +
      tzMins
    );
  };

  document.getElementById("start").value = formatISOWithTZ(startDate);
  document.getElementById("end").value = formatISOWithTZ(now);
}

async function ingestTradeToAi(tradeId, botId, symbol, side, reason) {
  const btn = document.getElementById("btn-ingest-ai");
  const feedback = document.getElementById("ai-ingest-feedback");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(`/admin/api/trades/${tradeId}/ingest-negative-sample`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": btn ? btn.dataset.csrfToken || "" : "",
      },
      credentials: "same-origin",
      body: JSON.stringify({ bot_id: botId, symbol: symbol, side: side, reason: reason }),
    });
    if (res.ok) {
      if (btn) btn.style.display = "none";
      if (feedback) feedback.style.display = "inline-block";
    } else {
      alert("Không thể đưa ca sai lệch vào dữ liệu phạt AI lúc này.");
      if (btn) btn.disabled = false;
    }
  } catch (e) {
    alert("Lỗi kết nối khi gửi yêu cầu.");
    if (btn) btn.disabled = false;
  }
}

// Gắn sự kiện bằng addEventListener vì CSP chặn thuộc tính onclick/onchange nội tuyến.
(() => {
  const scope = document.getElementById("scope");
  if (scope) scope.addEventListener("change", () => toggleBotSelect(scope.value));
  document.querySelectorAll(".quick-filter-btn[data-quick-range]").forEach((button) => {
    button.addEventListener("click", () => setQuickRange(button.dataset.quickRange));
  });
  const ingest = document.getElementById("btn-ingest-ai");
  if (ingest) {
    ingest.addEventListener("click", () => {
      const data = ingest.dataset;
      ingestTradeToAi(data.tradeId, data.botId, data.symbol, data.side, data.reason || "");
    });
  }
})();
