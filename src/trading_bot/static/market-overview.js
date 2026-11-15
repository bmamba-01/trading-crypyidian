/*
 * CK-1/CK-2: dải thông số trực tiếp và ma trận xu hướng đa khung trên /admin/chart.
 *
 * - Dữ liệu ban đầu nhúng trong #market-overview-data (CSP không cho script nội tuyến).
 * - Polling /admin/api/chart/overview mỗi 2 giây khi tab hiển thị; dừng hẳn khi mất quyền.
 * - Đếm ngược nến/funding chạy cục bộ mỗi giây, đồng bộ lại theo máy chủ ở mỗi lần polling.
 * - Phát sự kiện "mc:overview" và "mc:clock" để market-chart.js vẽ đường bid/ask và giờ đóng nến.
 * Chỉ đọc; không gửi lệnh.
 */
(() => {
  "use strict";

  const strip = document.getElementById("mc-live-strip");
  const dataNode = document.getElementById("market-overview-data");
  if (!strip || !dataNode) return;

  const POLL_MS = 2000;
  const SOURCE_VI = { quote_mid: "giữa bid/ask", mark: "mark price", last_close: "giá đóng nến gần nhất" };
  const ZONE_VI = { premium: "Vùng đắt (premium)", discount: "Vùng rẻ (discount)", equilibrium: "Cân bằng" };
  const ARROW = { up: "▲", down: "▼", range: "◆", unknown: "·" };

  let overview;
  try {
    overview = JSON.parse(dataNode.textContent || "{}");
  } catch {
    return;
  }

  const q = (selector) => strip.querySelector(selector);
  const setText = (selector, text, root = strip) => {
    const node = root.querySelector(selector);
    if (node) node.textContent = text;
  };
  const price = (value) => {
    if (value === null || value === undefined) return "—";
    const digits = Math.abs(value) >= 1 ? 2 : 8;
    return Number(value).toLocaleString("vi-VN", { maximumFractionDigits: digits });
  };
  const signed = (value, digits) =>
    `${value > 0 ? "+" : ""}${Number(value).toLocaleString("vi-VN", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    })}%`;
  const clock = (seconds) => {
    if (seconds === null || seconds === undefined) return "—";
    const s = Math.max(0, Math.floor(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${pad(m)}:${pad(s % 60)}`;
  };
  const tone = (node, value) => {
    if (!node) return;
    node.classList.toggle("is-up", value !== null && value > 0);
    node.classList.toggle("is-down", value !== null && value < 0);
  };

  // Mốc thời gian tuyệt đối theo đồng hồ trình duyệt, tính từ số giây còn lại máy chủ báo.
  let candleDeadline = null;
  let fundingDeadline = null;
  let candleSeconds = null;

  const renderStrip = (o) => {
    const c = o.candle;
    const quote = o.quote;
    const mark = o.mark;
    const day = o.day;
    setText("[data-ov-price]", price(c.price));
    const change = q("[data-ov-change]");
    if (change) {
      change.textContent = c.change_pct === null || c.change_pct === undefined ? "—" : signed(c.change_pct, 2);
      tone(change, c.change_pct ?? null);
    }
    setText("[data-ov-price-source]", SOURCE_VI[c.price_source] || c.price_source);
    setText("[data-ov-bid]", quote ? price(quote.bid) : "—");
    setText("[data-ov-ask]", quote ? price(quote.ask) : "—");
    setText("[data-ov-spread]", quote ? price(quote.spread) : "—");
    setText(
      "[data-ov-spread-bps]",
      quote && quote.spread_bps !== null ? Number(quote.spread_bps).toLocaleString("vi-VN", { maximumFractionDigits: 2 }) : "—"
    );
    const age = q("[data-ov-quote-age]");
    if (age) {
      age.textContent = quote ? `${quote.age_seconds} giây trước` : "chưa có stream";
      age.classList.toggle("is-stale", Boolean(quote && quote.stale));
    }
    const now = Date.now();
    candleDeadline = now + c.seconds_left * 1000;
    candleSeconds = c.seconds_left;
    fundingDeadline =
      mark && mark.funding_seconds_left !== null && mark.funding_seconds_left !== undefined
        ? now + mark.funding_seconds_left * 1000
        : null;

    const arrows = q("[data-ov-arrows]");
    if (arrows) {
      arrows.replaceChildren(
        ...o.mtf.cells.map((cell) => {
          const span = document.createElement("span");
          span.className = `mc-arrow mc-arrow--${cell.trend}`;
          span.title = `${cell.timeframe.toUpperCase()}: ${cell.label}`;
          const abbr = document.createElement("abbr");
          abbr.textContent = cell.timeframe.toUpperCase();
          span.append(abbr, ARROW[cell.trend] || "·");
          return span;
        })
      );
    }
    setText("[data-ov-consensus]", o.mtf.consensus.label);

    const sessions = q("[data-ov-sessions]");
    if (sessions) {
      if (o.sessions.length === 0) {
        sessions.textContent = "Ngoài phiên chính";
      } else {
        sessions.replaceChildren(
          ...o.sessions.map((item) => {
            const chip = document.createElement("span");
            chip.className = `mc-session-chip mc-session-chip--${item.key}`;
            chip.textContent = item.label;
            return chip;
          })
        );
      }
    }
    const kz = q("[data-ov-killzones]");
    if (kz) {
      const open = o.killzones || [];
      if (open.length === 0) {
        kz.textContent = "Ngoài killzone ICT";
      } else {
        kz.replaceChildren(
          ...open.map((item) => {
            const chip = document.createElement("span");
            chip.className = `mc-kz-chip mc-kz-chip--${item.kind}`;
            chip.textContent = item.label;
            return chip;
          })
        );
      }
    }
    setText("[data-ov-day-high]", day ? price(day.high) : "—");
    setText("[data-ov-day-low]", day ? price(day.low) : "—");
    setText("[data-ov-pdh]", day ? price(day.previous_high) : "—");
    setText("[data-ov-pdl]", day ? price(day.previous_low) : "—");
    setText("[data-ov-mark]", mark ? price(mark.mark_price) : "—");
    setText(
      "[data-ov-funding]",
      mark && mark.funding_rate_pct !== null && mark.funding_rate_pct !== undefined
        ? `${Number(mark.funding_rate_pct).toLocaleString("vi-VN", { minimumFractionDigits: 4, maximumFractionDigits: 4 })}%`
        : "—"
    );
    const notices = q("[data-ov-notices]");
    if (notices) {
      notices.replaceChildren(
        ...o.notices.map((text) => {
          const li = document.createElement("li");
          li.textContent = text;
          return li;
        })
      );
    }
  };

  const renderMatrix = (o) => {
    const body = document.querySelector("[data-ov-mtf-rows]");
    if (!body) return;
    const primary = o.mtf.consensus.primary;
    body.replaceChildren(
      ...o.mtf.cells.map((cell) => {
        const row = document.createElement("tr");
        row.className = `mc-mtf__row mc-mtf__row--${cell.trend}${cell.timeframe === primary ? " is-primary" : ""}`;
        const head = document.createElement("th");
        head.scope = "row";
        head.textContent = cell.timeframe.toUpperCase();
        if (cell.timeframe === primary) {
          const badge = document.createElement("span");
          badge.className = "mc-mtf__primary";
          badge.textContent = "chính";
          head.append(" ", badge);
        }
        const cellNode = (text) => {
          const td = document.createElement("td");
          td.textContent = text;
          return td;
        };
        const trend = document.createElement("td");
        const arrow = document.createElement("span");
        arrow.className = `mc-arrow mc-arrow--${cell.trend}`;
        arrow.textContent = ARROW[cell.trend] || "·";
        trend.append(arrow, ` ${cell.label}`);
        const position =
          cell.position_pct === null || cell.position_pct === undefined
            ? "—"
            : `${Math.round(cell.position_pct)}% · ${ZONE_VI[cell.zone] || "—"}`;
        const lastBreak = cell.last_break
          ? `${cell.last_break.kind.toUpperCase()} ${cell.last_break.direction === "bullish" ? "tăng" : "giảm"} tại ${price(cell.last_break.level)}`
          : "—";
        row.append(head, cellNode(cell.role), trend, cellNode(position), cellNode(lastBreak), cellNode(cell.statement));
        return row;
      })
    );
    const note = document.querySelector("[data-ov-consensus-note]");
    if (note) {
      note.textContent = `${o.mtf.consensus.label}. Xu hướng tính từ swing đã xác nhận (3 nến mỗi bên) trên nến đã đóng; không phải khuyến nghị đầu tư.`;
    }
  };

  const publish = (o) => document.dispatchEvent(new CustomEvent("mc:overview", { detail: o }));

  const tick = () => {
    const now = Date.now();
    if (candleDeadline !== null) {
      let left = Math.ceil((candleDeadline - now) / 1000);
      // Nến đã đóng: cuộn sang nến kế tiếp cùng khung cho tới khi máy chủ đồng bộ lại.
      const interval = overview.candle ? Date.parse(overview.candle.close_at) - Date.parse(overview.candle.open_at) : 0;
      while (left < 0 && interval > 0) {
        candleDeadline += interval;
        left = Math.ceil((candleDeadline - now) / 1000);
      }
      candleSeconds = Math.max(0, left);
      setText("[data-ov-candle-left]", clock(candleSeconds));
    }
    if (fundingDeadline !== null) {
      setText("[data-ov-funding-left]", clock(Math.max(0, Math.ceil((fundingDeadline - now) / 1000))));
    }
    document.dispatchEvent(new CustomEvent("mc:clock", { detail: { seconds_left: candleSeconds, text: clock(candleSeconds) } }));
  };

  const apply = (o) => {
    overview = o;
    renderStrip(o);
    renderMatrix(o);
    publish(o);
    tick();
  };

  apply(overview);
  window.setInterval(tick, 1000);

  const url = strip.dataset.overviewUrl;
  let stopped = false;
  let inFlight = false;
  const poll = async () => {
    if (stopped || inFlight || !url || document.visibilityState !== "visible") return;
    inFlight = true;
    try {
      const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if ([401, 403, 404].includes(response.status)) {
        stopped = true;
        return;
      }
      if (!response.ok) return;
      const next = await response.json();
      if (next.status === "ok") apply(next);
    } catch {
      /* giữ số liệu cũ khi lỗi mạng; nhãn tuổi giá cho biết dữ liệu đã cũ */
    } finally {
      inFlight = false;
    }
  };
  window.setInterval(poll, POLL_MS);
})();
