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
  const pair = (list, heading, value) => {
    const wrapper = node("div");
    wrapper.append(node("dt", heading));
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
    pair(detail, "Mã giao dịch", trade.trade_id);
    pair(detail, "Cặp giao dịch", trade.symbol);
    pair(detail, "Hướng vị thế", label(trade.side));
    pair(detail, "Tài sản định giá", trade.quote_asset);
    pair(detail, "Mở lúc", utc(trade.opened_at));
  };
  const render = (snapshot) => {
    title.textContent = text(snapshot.bot.name);
    observed.textContent = utc(snapshot.observed_at);
    observed.dateTime = snapshot.observed_at;
    runtime.replaceChildren();
    pair(runtime, "Trạng thái bot", label(snapshot.bot.status));
    pair(runtime, "Trạng thái mở vị thế", label(snapshot.runtime.entry_state));
    pair(runtime, "Sẵn sàng", label(snapshot.runtime.readiness));
    pair(runtime, "Mã lý do sẵn sàng", snapshot.runtime.readiness_reason_code);
    pair(runtime, "Cho phép mở vị thế", snapshot.runtime.entry_allowed ? "Có" : "Không");
    pair(runtime, "Phiên bản điều khiển bot", snapshot.runtime.control_revision);
    pair(runtime, "Điều khiển không gian làm việc", label(snapshot.runtime.workspace_kill_state));
    pair(runtime, "Phiên bản điều khiển không gian làm việc", snapshot.runtime.workspace_control_revision);
    totals.replaceChildren(node("p", `Khoảng báo cáo: ${utc(snapshot.window.start)} đến ${utc(snapshot.window.end)}; không gồm mốc kết thúc.`));
    for (const [asset, values] of Object.entries(snapshot.totals)) {
      const section = node("section", undefined, "dashboard-live-asset");
      section.append(node("h4", `Thống kê ${asset}`));
      const list = node("dl", undefined, "dashboard-runtime");
      for (const [key, heading, monetary] of metrics) {
        if (!(key in values)) continue;
        const signed = ["net_pnl", "gross_pnl", "expectancy_per_trade", "average_winning_trade", "average_losing_trade"].includes(key);
        pair(list, heading, monetary ? money(values[key], asset, signed) : number(values[key]));
      }
      section.append(list);
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
    for (const trade of snapshot.recent_closed_trades) {
      const article = node("article", undefined, "trade-list-card");
      article.append(node("h5", `${trade.symbol} · ${label(trade.side)} · ${trade.quote_asset}`));
      const list = node("dl");
      pair(list, "Mã giao dịch", trade.trade_id);
      pair(list, "Mở lúc", utc(trade.opened_at));
      pair(list, "Đóng lúc", utc(trade.closed_at));
      for (const [key, heading] of [
        ["net_pnl", "Lãi/lỗ ròng"], ["gross_pnl", "Lãi/lỗ gộp"],
        ["fee_total", "Phí giao dịch"], ["funding_total", "Phí tài trợ"],
        ["slippage_total", "Chi phí trượt giá"], ["total_cost", "Tổng chi phí"],
      ]) pair(list, heading, money(trade[key], trade.quote_asset, key.endsWith("pnl")));
      article.append(list);
      closed.append(article);
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
