"use strict";

// CK-10 — Cột phân tích của /admin/chart: tab (mẫu ARIA), bấm mức giá để cuộn chart tới mức,
// đếm ngược funding theo overview, trạng thái scorer (chỉ đọc, API sẵn có). Chỉ textContent.
(() => {
  const api = window.CrypidianChart;
  const panel = document.querySelector("[data-insights]");
  if (!panel) return;

  const tabs = Array.from(panel.querySelectorAll("[data-insight-tab]"));
  const loaders = {};
  const activate = (tab, focus = false) => {
    for (const other of tabs) {
      const selected = other === tab;
      other.setAttribute("aria-selected", String(selected));
      other.tabIndex = selected ? 0 : -1;
      panel.querySelector(`[data-insight-panel="${other.dataset.insightTab}"]`).hidden = !selected;
    }
    if (focus) tab.focus();
    loaders[tab.dataset.insightTab]?.();
  };
  for (const tab of tabs) {
    tab.addEventListener("click", () => activate(tab));
    tab.addEventListener("keydown", (event) => {
      const index = tabs.indexOf(tab);
      if (event.key === "ArrowRight") activate(tabs[(index + 1) % tabs.length], true);
      else if (event.key === "ArrowLeft") activate(tabs[(index - 1 + tabs.length) % tabs.length], true);
      else return;
      event.preventDefault();
    });
  }

  // ---- Mức giá: cuộn chart tới thời điểm mức xuất hiện, giữ độ rộng vùng nhìn hiện tại ----
  for (const button of panel.querySelectorAll("[data-level-time]")) {
    button.addEventListener("click", () => {
      const time = Number(button.dataset.levelTime);
      if (!api || !Number.isFinite(time) || time <= 0) return;
      const timeScale = api.chart.timeScale();
      const x = api.xForTime(time);
      const range = timeScale.getVisibleLogicalRange();
      const logical = x === null ? null : timeScale.coordinateToLogical(x);
      if (!range || logical === null) return;
      const span = range.to - range.from;
      timeScale.setVisibleLogicalRange({ from: logical - span * 0.25, to: logical + span * 0.75 });
      api.container.focus();
    });
  }

  // ---- Đếm ngược funding (đồng bộ theo overview mỗi 2 giây, chạy cục bộ mỗi giây) ----
  const funding = panel.querySelector("[data-insight-funding]");
  let fundingDeadline = null;
  const clock = (seconds) => {
    const s = Math.max(0, Math.floor(seconds));
    const pad = (n) => String(n).padStart(2, "0");
    const h = Math.floor(s / 3600);
    return h ? `${h}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}` : `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
  };
  document.addEventListener("mc:overview", (event) => {
    const left = event.detail?.mark?.funding_seconds_left;
    fundingDeadline = left === null || left === undefined ? null : Date.now() + left * 1000;
  });
  window.setInterval(() => {
    if (funding && fundingDeadline !== null) funding.textContent = clock((fundingDeadline - Date.now()) / 1000);
  }, 1000);

  // ---- AI: trạng thái scorer của workspace (chỉ đọc) ----
  const aiStatus = panel.querySelector("[data-ai-status]");
  const MODE_VI = { DISABLED: "Tắt", SHADOW: "Chạy bóng (không ảnh hưởng lệnh)", PAPER_GATE: "Cổng PAPER" };
  let aiLoaded = false;
  loaders.ai = async () => {
    if (!aiStatus || aiLoaded) return;
    const row = (label, value) => {
      const div = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      div.append(dt, dd);
      return div;
    };
    try {
      const response = await fetch("/api/v1/learning-scorer/deployment", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (response.status === 403) {
        aiStatus.replaceChildren(row("Trạng thái scorer", "Bạn không có quyền xem mô hình."));
        return;
      }
      if (!response.ok) {
        aiStatus.replaceChildren(row("Trạng thái scorer", "Chưa đọc được trạng thái (dịch vụ chưa sẵn sàng)."));
        return;
      }
      const body = await response.json();
      aiLoaded = true;
      aiStatus.replaceChildren(
        row("Chế độ", MODE_VI[body.mode] || body.mode),
        row("Sức khỏe", body.health),
        row("Dùng được khi chạy", body.runtime_usable ? "Có" : "Không — bot giữ quyết định theo rule"),
        row("Phiên bản triển khai", String(body.deployment_revision))
      );
    } catch {
      aiStatus.replaceChildren(row("Trạng thái scorer", "Lỗi mạng khi tải trạng thái."));
    }
  };
})();
