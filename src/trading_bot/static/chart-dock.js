"use strict";

// CK-9 — Dock dưới chart (Vị thế PAPER, Lịch sử, Nhật ký quyết định, Sự kiện), thanh trạng thái
// dữ liệu và nút "Kiểm dữ liệu". Chỉ đọc API admin; dựng DOM bằng textContent.
(() => {
  const api = window.CrypidianChart;
  const dock = document.querySelector("[data-dock]");
  const statusbar = document.querySelector("[data-statusbar]");
  if (!api || !dock) return;

  const fmtNumber = (value, digits = 2) =>
    value === null || value === undefined ? "—" : api.fmtNumber(value, digits);
  const fmtIso = (iso) => (iso ? api.fmtTime(Date.parse(iso) / 1000) : "—");
  const fmtAge = (seconds) => {
    if (seconds === null || seconds === undefined) return "—";
    if (seconds < 90) return `${seconds} giây`;
    if (seconds < 5400) return `${Math.round(seconds / 60)} phút`;
    return `${Math.round(seconds / 3600)} giờ`;
  };
  const cell = (text, className) => {
    const td = document.createElement("td");
    td.textContent = text;
    if (className) td.className = className;
    return td;
  };
  const emptyRow = (columns, text) => {
    const tr = document.createElement("tr");
    const td = cell(text);
    td.colSpan = columns;
    tr.append(td);
    return tr;
  };
  const getJson = async (path, params) => {
    const response = await fetch(`${path}?${new URLSearchParams(params).toString()}`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  };

  // ---- Tab theo mẫu WAI-ARIA (mũi tên trái/phải chuyển tab) ----
  const tabs = Array.from(dock.querySelectorAll("[data-dock-tab]"));
  const loaders = {};
  const activate = (tab, focus = false) => {
    for (const other of tabs) {
      const selected = other === tab;
      other.setAttribute("aria-selected", String(selected));
      other.tabIndex = selected ? 0 : -1;
      dock.querySelector(`[data-dock-panel="${other.dataset.dockTab}"]`).hidden = !selected;
    }
    if (focus) tab.focus();
    loaders[tab.dataset.dockTab]?.();
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

  // ---- Vị thế PAPER: cập nhật theo overview (2 giây) ----
  const positionsBody = dock.querySelector("[data-dock-positions]");
  const positionsCount = dock.querySelector('[data-dock-count="positions"]');
  const renderPositions = (positions) => {
    if (positionsCount) positionsCount.textContent = String(positions.length);
    if (!positionsBody) return;
    if (positions.length === 0) {
      positionsBody.replaceChildren(emptyRow(9, `Không có vị thế PAPER đang mở cho ${api.symbol}.`));
      return;
    }
    positionsBody.replaceChildren(
      ...positions.map((p) => {
        const tr = document.createElement("tr");
        const tone = p.upnl === null ? "" : p.upnl >= 0 ? "is-up" : "is-down";
        tr.append(
          cell(p.side === "LONG" ? "MUA" : "BÁN", p.side === "LONG" ? "is-up" : "is-down"),
          cell(fmtNumber(p.entry_price)),
          cell(fmtNumber(p.quantity, 6)),
          cell(fmtNumber(p.stop)),
          cell(fmtNumber(p.target)),
          cell(`${fmtNumber(p.mark_price)} (${p.mark_source || "—"})`),
          cell(`${p.upnl === null ? "—" : `${p.upnl >= 0 ? "+" : ""}${fmtNumber(p.upnl)} ${p.quote_asset || ""}`}`, tone),
          cell(p.r_multiple === null ? "—" : `${fmtNumber(p.r_multiple)}R`, tone),
          cell(fmtIso(p.filled_at))
        );
        return tr;
      })
    );
  };

  // ---- Thanh trạng thái ----
  const setText = (selector, text, className) => {
    const node = statusbar?.querySelector(selector);
    if (!node) return;
    node.textContent = text;
    if (className !== undefined) node.className = className;
  };
  const updateLayers = () => {
    const on = Array.from(document.querySelectorAll("[data-layer]")).filter((input) => input.checked);
    const node = statusbar?.querySelector("[data-sb-layers]");
    if (!node) return;
    node.textContent = String(on.length);
    node.title = on.map((input) => input.closest("label")?.textContent?.trim() || input.dataset.layer).join(", ");
  };
  for (const input of document.querySelectorAll("[data-layer]")) input.addEventListener("change", updateLayers);
  updateLayers();
  document.addEventListener("mc:overview", (event) => {
    const o = event.detail;
    renderPositions(o.positions || []);
    const ds = o.data_status;
    if (ds) {
      if (ds.source_label && ds.source_label !== "—") setText("[data-sb-source]", ds.source_label);
      setText("[data-sb-bars]", String(ds.bar_count));
      setText("[data-sb-last]", fmtIso(ds.last_close_at));
      setText("[data-sb-delay]", fmtAge(ds.delay_seconds), ds.delayed ? "is-stale" : "");
      setText("[data-sb-gaps]", String(ds.open_gaps), ds.open_gaps ? "is-stale" : "");
    }
    setText(
      "[data-sb-quote]",
      o.quote ? `${fmtAge(o.quote.age_seconds)} trước` : "chưa có stream",
      o.quote && !o.quote.stale ? "" : "is-stale"
    );
    setText(
      "[data-sb-mark]",
      o.mark ? `${fmtAge(o.mark.age_seconds)} trước` : "chưa có stream",
      o.mark && !o.mark.stale ? "" : "is-stale"
    );
  });

  // ---- Lịch sử lệnh đã đóng ----
  const historyBody = dock.querySelector("[data-dock-history]");
  const period = dock.querySelector("[data-history-period]");
  const csv = dock.querySelector("[data-history-csv]");
  const REASON_VI = { STOP_LOSS: "Cắt lỗ", TAKE_PROFIT: "Chốt lời", TIME_EXIT: "Hết thời gian", MANUAL: "Thủ công" };
  loaders.history = async () => {
    if (!historyBody) return;
    historyBody.replaceChildren(emptyRow(6, "Đang tải…"));
    try {
      const body = await getJson("/admin/api/chart/history", {
        bot_id: dock.dataset.botId,
        symbol: dock.dataset.symbol,
        period: period?.value || "7d",
      });
      if (csv) csv.href = body.csv_url;
      if (!body.trades.length) {
        historyBody.replaceChildren(emptyRow(6, `Chưa có lệnh PAPER đã đóng cho ${api.symbol} trong kỳ này.`));
        return;
      }
      historyBody.replaceChildren(
        ...body.trades.map((t) => {
          const tr = document.createElement("tr");
          const pnl = Number(t.net_pnl);
          tr.append(
            cell(t.side === "LONG" ? "MUA" : "BÁN"),
            cell(fmtIso(t.opened_at)),
            cell(fmtIso(t.closed_at)),
            cell(REASON_VI[t.exit_reason] || t.exit_reason || "—"),
            cell(`${fmtNumber(Number(t.total_cost))} ${t.quote_asset || ""}`),
            cell(`${pnl >= 0 ? "+" : ""}${fmtNumber(pnl)} ${t.quote_asset || ""}`, pnl >= 0 ? "is-up" : "is-down")
          );
          return tr;
        })
      );
    } catch {
      historyBody.replaceChildren(emptyRow(6, "Không tải được lịch sử (thiếu quyền hoặc lỗi máy chủ)."));
    }
  };
  period?.addEventListener("change", () => loaders.history());

  // ---- Nhật ký quyết định ----
  const decisionList = dock.querySelector("[data-dock-decisions]");
  const STATUS_VI = { CANDIDATE: "Ra tín hiệu", ABSTAIN: "Không vào lệnh", VETO: "Bị chặn" };
  loaders.decisions = async () => {
    if (!decisionList) return;
    const li = (text) => {
      const item = document.createElement("li");
      item.textContent = text;
      return item;
    };
    decisionList.replaceChildren(li("Đang tải…"));
    try {
      const body = await getJson("/admin/api/chart/decisions", { bot_id: dock.dataset.botId });
      const items = body.decisions.map((d) => {
        const item = document.createElement("li");
        const time = document.createElement("time");
        time.dateTime = d.evaluated_at || "";
        time.textContent = fmtIso(d.evaluated_at);
        const direction = d.direction ? ` ${d.direction === "long" ? "MUA" : "BÁN"}` : "";
        item.append(time, ` — ${STATUS_VI[d.status] || d.status}${direction} · ${d.reason_code}: ${d.detail_vi}`);
        return item;
      });
      if (body.unverified_decisions) items.push(li(`Bỏ qua ${body.unverified_decisions} quyết định không khớp hash.`));
      decisionList.replaceChildren(...(items.length ? items : [li("Bot chưa có quyết định nào.")]));
    } catch {
      decisionList.replaceChildren(li("Không tải được nhật ký (thiếu quyền hoặc lỗi máy chủ)."));
    }
  };

  // ---- Kiểm dữ liệu ----
  const validateButton = statusbar?.querySelector("[data-validate]");
  const result = document.querySelector("[data-validate-result]");
  validateButton?.addEventListener("click", async () => {
    if (!result) return;
    result.hidden = false;
    result.className = "mc-validate";
    result.textContent = "Đang kiểm…";
    try {
      const body = await getJson("/admin/api/chart/validate", {
        bot_id: api.botId,
        symbol: api.symbol,
        timeframe: api.timeframe,
      });
      result.className = `mc-validate ${body.ok ? "is-ok" : "is-bad"}`;
      const summary = document.createElement("p");
      summary.textContent = `${body.summary} Nguồn: ${body.source_label}.`;
      const details = document.createElement("ul");
      for (const gap of body.missing.slice(0, 10)) {
        const item = document.createElement("li");
        item.textContent = `Thiếu ${gap.bars} nến: ${fmtIso(gap.from)} → ${fmtIso(gap.to)}`;
        details.append(item);
      }
      for (const [label, list] of [
        ["OHLC sai", body.invalid_ohlc],
        ["Mốc trùng", body.duplicates],
        ["Khối lượng âm", body.negative_volume],
        ["Taker buy vượt khối lượng", body.taker_exceeds_volume],
      ]) {
        if (list.length) {
          const item = document.createElement("li");
          item.textContent = `${label}: ${list.slice(0, 5).map(fmtIso).join(", ")}${list.length > 5 ? "…" : ""}`;
          details.append(item);
        }
      }
      for (const gap of body.open_ingestion_gaps) {
        const item = document.createElement("li");
        item.textContent = `Khoảng trống ingest chưa lấp (${gap.source}): ${fmtIso(gap.start)} → ${fmtIso(gap.end)}`;
        details.append(item);
      }
      if (body.revised_bars) {
        const item = document.createElement("li");
        item.textContent = `${body.revised_bars} nến đã được sửa lại (có nhiều bản ghi); chart dùng bản mới nhất.`;
        details.append(item);
      }
      result.replaceChildren(summary, ...(details.children.length ? [details] : []));
    } catch {
      result.className = "mc-validate is-bad";
      result.textContent = "Không kiểm được dữ liệu (thiếu quyền hoặc lỗi máy chủ).";
    }
  });
})();
