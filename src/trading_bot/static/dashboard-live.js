"use strict";

(() => {
  const root = document.getElementById("dashboard-live");
  if (!root) return;
  const buttons = [...root.querySelectorAll("button[data-dashboard-bot]")];
  const panel = root.querySelector("#dashboard-live-panel");
  const status = panel.querySelector("[data-snapshot-status]");
  const observed = panel.querySelector("[data-observed-at]");
  const title = panel.querySelector("[data-selected-bot-name]");
  const runtime = panel.querySelector("[data-runtime]");
  const totals = panel.querySelector("[data-totals]");
  const sessions = panel.querySelector("#dashboard-open-trade");
  const detail = panel.querySelector("[data-open-trade-detail]");
  const closed = panel.querySelector("[data-closed-trades]");
  const link = panel.querySelector("[data-selected-bot-link]");
  const metrics = JSON.parse(document.getElementById("dashboard-live-metrics").textContent);
  const snapshots = new Map();
  const labels = {
    ACTIVE: "Đang hoạt động", ENABLED: "Đã bật", DISABLED: "Đã tắt",
    READY: "Sẵn sàng", NOT_READY: "Chưa sẵn sàng", UNKNOWN: "Chưa xác định",
    STARTING: "Đang khởi động", STANDBY: "Đang chờ tiếp quản",
    RECOVERY_REQUIRED: "Cần khôi phục", PAUSED: "Tạm dừng",
    KILLED: "Đã chặn mở vị thế", RUNNING: "Đang xử lý", OPEN: "Đang mở",
    LONG: "Mua lên", SHORT: "Bán xuống", BLOCKED: "Bị chặn",
    DEGRADED: "Suy giảm", STOPPED: "Đã dừng",
  };
  let selected = null;
  let generation = 0;
  let controller = null;
  let timer = null;
  let stopped = false;
  let disposed = false;

  // Biểu tượng nét 1.75px (phong cách Lucide), dựng bằng DOM để tuân thủ CSP.
  const ICONS = {
    status: "M12 2v10M5.6 5.6a9 9 0 1 0 12.8 0",
    entry: "M5 12h14M13 6l6 6-6 6",
    ready: "M20 6 9 17l-5-5",
    reason: "M4 7h16M4 12h10M4 17h7",
    allow: "M12 3 4 6v6c0 4.4 3.4 8.4 8 9 4.6-.6 8-4.6 8-9V6l-8-3Z",
    revision: "M4 4v6h6M20 20v-6h-6M5 13a7 7 0 0 0 13 3M19 11A7 7 0 0 0 6 8",
    kill: "M12 3 2 20h20L12 3ZM12 10v4M12 17h.01",
    pnl: "M3 17l6-6 4 4 8-8M15 7h6v6",
    count: "M4 6h16M4 12h16M4 18h10",
    winrate: "M12 3a9 9 0 1 0 9 9h-9V3Z",
    factor: "M4 20 20 4M7 7h.01M17 17h.01",
    drawdown: "M3 7l6 6 4-4 8 8M15 17h6v-6",
    win: "M7 4h10v5a5 5 0 0 1-10 0V4ZM12 14v4M8 20h8",
    loss: "M12 5v14M5 12l7 7 7-7",
    streak: "M13 2 4 14h7l-1 8 9-12h-7l1-8Z",
    time: "M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z",
    fee: "M6 3h12v18l-3-2-3 2-3-2-3 2V3ZM9 8h6M9 12h6",
    funding: "M12 3v18M17 7H9.5a2.5 2.5 0 0 0 0 5h5a2.5 2.5 0 0 1 0 5H7",
    slippage: "M3 12c3-4 6 4 9 0s6-4 9 0",
    symbol: "M4 7h16M4 12h16M4 17h16",
    side: "M7 17 17 7M9 7h8v8",
    asset: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM9 9h6M9 15h6M12 6v12",
    id: "M5 9h14M5 15h14M10 3 8 21M16 3l-2 18",
  };
  const SVG_NS = "http://www.w3.org/2000/svg";
  const icon = (name) => {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("class", "ico");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", ICONS[name] || ICONS.reason);
    svg.append(path);
    return svg;
  };
  // Chỉ số chính hiện trong dải số lớn; còn lại là hàng nhãn–giá trị.
  const HERO_METRICS = {
    net_pnl: "pnl", closed_trade_count: "count", win_rate_percent: "winrate",
    profit_factor: "factor", max_drawdown: "drawdown",
  };
  const METRIC_ICONS = {
    expectancy_per_trade: "pnl", average_winning_trade: "win", average_losing_trade: "loss",
    max_consecutive_wins: "streak", max_consecutive_losses: "streak",
    average_holding_seconds: "time", gross_pnl: "pnl", fee_total: "fee",
    funding_total: "funding", slippage_total: "slippage",
  };

  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const text = (value) => value === null || value === undefined ? "Chưa có dữ liệu" : String(value);
  const label = (value) => labels[value] || "Chưa xác định";
  const utc = (value) => {
    const date = new Date(value);
    return value && Number.isFinite(date.getTime())
      ? date.toISOString().replace("T", " ").replace("Z", " UTC")
      : "Chưa xác định thời gian";
  };
  const number = (value) => {
    if (value === null || value === undefined) return "Không xác định";
    const raw = String(value);
    if (!/^[+-]?\d+(\.\d+)?$/.test(raw)) return raw;
    const [integer, fraction] = raw.split(".");
    return integer.replace(/\B(?=(\d{3})+(?!\d))/g, ".") + (fraction ? `,${fraction}` : "");
  };
  const money = (value, asset, signed = false) => {
    const raw = String(value ?? "");
    const valid = /^[+-]?\d+(\.\d+)?$/.test(raw);
    const zero = valid && !/[1-9]/.test(raw);
    const tone = !valid ? "unknown" : zero ? "flat" : raw.startsWith("-") ? "negative" : "positive";
    const prefix = signed && tone === "positive" && !raw.startsWith("+") ? "+" : "";
    const element = node("span", `${prefix}${number(value)} ${asset}`,
      signed ? `pnl-value pnl-value--${tone}` : "");
    if (signed) element.dataset.pnlTone = tone;
    return element;
  };
  const sideBadge = (side) => {
    const long = side === "LONG";
    const badge = node("span", undefined, `side-badge side-badge--${long ? "long" : side === "SHORT" ? "short" : "unknown"}`);
    badge.append(icon(long ? "side" : "loss"), document.createTextNode(label(side)));
    return badge;
  };
  // Mã đầy đủ vẫn nằm trong văn bản (tìm/sao chép được); CSS cắt bớt bằng dấu ba chấm.
  const tradeId = (value) => {
    const code = node("code", text(value), "trade-id");
    code.title = text(value);
    return code;
  };
  const pair = (list, heading, value, iconName) => {
    const wrapper = node("div", undefined, "kv");
    const term = node("dt");
    if (iconName) term.append(icon(iconName));
    term.append(document.createTextNode(heading));
    wrapper.append(term);
    const content = node("dd");
    content.append(value instanceof Node ? value : document.createTextNode(text(value)));
    wrapper.append(content);
    list.append(wrapper);
  };
  const renderTrade = () => {
    detail.replaceChildren();
    const trade = snapshots.get(selected)?.open_trades.find(item => item.trade_id === sessions.value);
    if (!trade) {
      detail.append(node("p", "Chưa có giao dịch PAPER đang mở."));
      return;
    }
    pair(detail, "Cặp giao dịch", trade.symbol, "symbol");
    pair(detail, "Hướng vị thế", sideBadge(trade.side), "side");
    pair(detail, "Tài sản định giá", trade.quote_asset, "asset");
    pair(detail, "Mở lúc", utc(trade.opened_at), "time");
    pair(detail, "Mã giao dịch", tradeId(trade.trade_id), "id");
  };
  const render = (snapshot) => {
    title.textContent = text(snapshot.bot.name);
    observed.textContent = utc(snapshot.observed_at);
    observed.dateTime = snapshot.observed_at;
    runtime.replaceChildren();
    pair(runtime, "Trạng thái bot", label(snapshot.bot.status), "status");
    pair(runtime, "Trạng thái mở vị thế", label(snapshot.runtime.entry_state), "entry");
    pair(runtime, "Sẵn sàng", label(snapshot.runtime.readiness), "ready");
    pair(runtime, "Mã lý do sẵn sàng", snapshot.runtime.readiness_reason_code, "reason");
    pair(runtime, "Cho phép mở vị thế", snapshot.runtime.entry_allowed ? "Có" : "Không", "allow");
    pair(runtime, "Phiên bản điều khiển bot", snapshot.runtime.control_revision, "revision");
    pair(runtime, "Điều khiển không gian làm việc", label(snapshot.runtime.workspace_kill_state), "kill");
    pair(runtime, "Phiên bản điều khiển không gian làm việc", snapshot.runtime.workspace_control_revision, "revision");
    totals.replaceChildren(node("p", `Khoảng báo cáo: ${utc(snapshot.window.start)} đến ${utc(snapshot.window.end)}; không gồm mốc kết thúc.`, "dashboard-window"));
    for (const [asset, values] of Object.entries(snapshot.totals)) {
      const section = node("section", undefined, "dashboard-live-asset");
      section.append(node("h4", `Thống kê ${asset}`));
      const hero = node("div", undefined, "stat-strip");
      const list = node("dl", undefined, "dashboard-runtime kv-grid");
      for (const [key, heading, monetary] of metrics) {
        if (!(key in values)) continue;
        const signed = ["net_pnl", "gross_pnl", "expectancy_per_trade", "average_winning_trade", "average_losing_trade"].includes(key);
        const value = monetary ? money(values[key], asset, signed) : number(values[key]);
        if (key in HERO_METRICS) {
          const stat = node("div", undefined, `stat stat--${key}`);
          stat.append(icon(HERO_METRICS[key]));
          const body = node("div", undefined, "stat__body");
          const figure = node("span", undefined, "stat__value");
          figure.append(value instanceof Node ? value : document.createTextNode(value));
          body.append(figure, node("span", heading, "stat__label"));
          stat.append(body);
          hero.append(stat);
        } else {
          pair(list, heading, value, METRIC_ICONS[key]);
        }
      }
      section.append(hero, list);
      totals.append(section);
    }
    if (Object.keys(snapshot.totals).length === 0) {
      totals.append(node("p", "Chưa có giao dịch đã đóng trong khoảng báo cáo."));
    }
    // Giữ lựa chọn phiên khi poll trả lại cùng giao dịch, không làm mất focus của select.
    const previous = sessions.value;
    const tradeIds = snapshot.open_trades.map(trade => trade.trade_id);
    const existingIds = [...sessions.options].map(option => option.value);
    if (JSON.stringify(tradeIds) !== JSON.stringify(existingIds)) {
      sessions.replaceChildren();
      for (const trade of snapshot.open_trades) {
        const option = node("option", `${trade.symbol} · ${label(trade.side)} · ${trade.trade_id}`);
        option.value = trade.trade_id;
        sessions.append(option);
      }
      if (tradeIds.includes(previous)) sessions.value = previous;
      if (tradeIds.length === 0) sessions.append(node("option", "Chưa có phiên đang mở"));
    }
    sessions.disabled = tradeIds.length === 0;
    renderTrade();
    closed.replaceChildren();
    if (snapshot.recent_closed_trades.length) {
      const table = node("table", undefined, "trade-table");
      const head = node("thead");
      const headRow = node("tr");
      for (const heading of ["Đóng lúc (UTC)", "Cặp · hướng", "Lãi/lỗ ròng", "Lãi/lỗ gộp", "Tổng chi phí", "Mã giao dịch"]) {
        const cell = node("th", heading);
        cell.scope = "col";
        headRow.append(cell);
      }
      head.append(headRow);
      const body = node("tbody");
      for (const trade of snapshot.recent_closed_trades) {
        const row = node("tr", undefined, "trade-list-card");
        const cell = (labelText, content, className) => {
          const td = node("td", undefined, className);
          td.dataset.label = labelText;
          td.append(content instanceof Node ? content : document.createTextNode(content));
          row.append(td);
        };
        const when = node("time", utc(trade.closed_at));
        when.dateTime = trade.closed_at;
        when.title = `Mở lúc ${utc(trade.opened_at)}`;
        cell("Đóng lúc", when, "num");
        const instrument = node("span", undefined, "trade-instrument");
        instrument.append(node("strong", trade.symbol), sideBadge(trade.side));
        cell("Cặp · hướng", instrument);
        cell("Lãi/lỗ ròng", money(trade.net_pnl, trade.quote_asset, true), "num num--strong");
        cell("Lãi/lỗ gộp", money(trade.gross_pnl, trade.quote_asset, true), "num");
        const costs = node("span", `${number(trade.total_cost)} ${trade.quote_asset}`);
        costs.title = `Phí ${number(trade.fee_total)} · Tài trợ ${number(trade.funding_total)} · Trượt giá ${number(trade.slippage_total)}`;
        cell("Tổng chi phí", costs, "num");
        cell("Mã giao dịch", tradeId(trade.trade_id));
        body.append(row);
      }
      table.append(head, body);
      closed.append(table);
    }
    if (snapshot.recent_closed_trades.length === 0) closed.append(node("p", "Chưa có giao dịch PAPER đã đóng trong khoảng báo cáo."));
  };
  const cancel = () => {
    clearTimeout(timer);
    timer = null;
    generation += 1;
    controller?.abort();
    controller = null;
    panel.setAttribute("aria-busy", "false");
  };
  const poll = async () => {
    if (!selected || stopped || disposed || document.hidden) return;
    const botId = selected;
    const token = generation;
    const request = new AbortController();
    controller = request;
    panel.setAttribute("aria-busy", "true");
    const timeout = setTimeout(() => request.abort(), 10000);
    try {
      const response = await fetch(`/admin/dashboard/bots/${encodeURIComponent(botId)}/snapshot`, {
        credentials: "same-origin", cache: "no-store", signal: request.signal,
      });
      if (token !== generation || document.hidden || disposed) return;
      if ([401, 403, 404].includes(response.status)) {
        stopped = true;
        throw new Error("access");
      }
      if (!response.ok) throw new Error("unavailable");
      const snapshot = await response.json();
      if (token !== generation || selected !== botId || document.hidden || disposed) return;
      if (snapshot.bot.id !== botId || snapshot.source !== "PAPER_LEDGER") throw new Error("scope");
      snapshots.set(botId, snapshot);
      render(snapshot);
      status.textContent = "Đã cập nhật. Tự làm mới mỗi 5 giây.";
      status.dataset.state = "current";
    } catch {
      if (token !== generation || document.hidden || disposed) return;
      const previous = snapshots.has(botId) ? "Dữ liệu cũ: giữ ảnh chụp gần nhất. " : "Chưa tải được ảnh chụp. ";
      status.textContent = previous + (stopped
        ? "Đã dừng cập nhật vì phiên hoặc quyền truy cập không còn hợp lệ. Hãy tải lại trang để kiểm tra quyền."
        : "Kết nối chưa sẵn sàng; sẽ thử lại sau 5 giây.");
      status.dataset.state = "stale";
    } finally {
      clearTimeout(timeout);
      // Phản hồi lỗi không được đọc thân; hủy để trình duyệt đóng request thay vì treo chờ.
      request.abort();
      if (token === generation && !disposed) {
        controller = null;
        panel.setAttribute("aria-busy", "false");
        if (!stopped && !document.hidden) timer = setTimeout(poll, 5000);
      }
    }
  };
  const select = (button) => {
    if (selected === button.dataset.dashboardBot) return;
    cancel();
    selected = button.dataset.dashboardBot;
    stopped = false;
    for (const item of buttons) item.setAttribute("aria-pressed", String(item === button));
    link.href = `/admin/bots/${encodeURIComponent(selected)}`;
    link.hidden = false;
    if (snapshots.has(selected)) render(snapshots.get(selected));
    else {
      title.textContent = button.querySelector("span").textContent;
      observed.textContent = "Chưa có ảnh chụp";
      observed.removeAttribute("datetime");
      for (const element of [runtime, totals, detail, closed]) element.replaceChildren();
      sessions.replaceChildren(node("option", "Đang tải phiên PAPER…"));
      sessions.disabled = true;
    }
    status.textContent = "Đang tải ảnh chụp bot…";
    status.dataset.state = "loading";
    poll();
  };
  for (const button of buttons) button.addEventListener("click", () => select(button));
  sessions.addEventListener("change", renderTrade);
  document.addEventListener("visibilitychange", () => {
    cancel();
    if (document.hidden) status.textContent = "Tạm dừng cập nhật khi trang đang ẩn.";
    else poll();
  });
  window.addEventListener("pagehide", () => { disposed = true; cancel(); });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) { disposed = false; poll(); }
  });
  if (buttons.length) select(buttons[0]);
})();
