"use strict";

// SIG-7 — Biểu đồ thị trường kiểu TradingView.
// Máy chủ tính mọi lớp phủ (SIG-1); tệp này chỉ vẽ bằng Lightweight Charts v5:
// series chuẩn + "primitive" tự vẽ (hộp phiên, mức thanh khoản, trendline, volume profile).
// Không chèn HTML từ dữ liệu (chỉ textContent); không dùng style nội tuyến trong markup.
(() => {
  const LWC = window.LightweightCharts;
  const container = document.getElementById("market-chart");
  const dataElement = document.getElementById("market-chart-data");
  if (!container || !dataElement) return;

  const DISPLAY_TIME_ZONE = "Asia/Ho_Chi_Minh";
  const POLL_MS = 5000;
  const dateTime = new Intl.DateTimeFormat("vi-VN", {
    timeZone: DISPLAY_TIME_ZONE,
    hourCycle: "h23",
    hour: "2-digit",
    minute: "2-digit",
    day: "2-digit",
    month: "2-digit",
  });
  const tickFormats = [
    { year: "numeric" },
    { month: "2-digit", year: "numeric" },
    { day: "2-digit", month: "2-digit" },
    { hour: "2-digit", minute: "2-digit" },
    { hour: "2-digit", minute: "2-digit", second: "2-digit" },
  ].map(
    (options) =>
      new Intl.DateTimeFormat("vi-VN", { timeZone: DISPLAY_TIME_ZONE, hourCycle: "h23", ...options })
  );
  // Ghép thủ công "HH:mm dd/MM" để không phụ thuộc dấu phân cách của từng trình duyệt.
  const fmtTime = (seconds) => {
    const parts = Object.fromEntries(
      dateTime.formatToParts(new Date(seconds * 1000)).map((part) => [part.type, part.value])
    );
    return `${parts.hour}:${parts.minute} ${parts.day}/${parts.month}`;
  };
  const fmtNumber = (value, digits = 8) =>
    Number(value).toLocaleString("vi-VN", { maximumFractionDigits: digits });

  const showFallback = (message) => {
    container.hidden = true;
    const notice = document.createElement("p");
    notice.setAttribute("role", "status");
    notice.className = "chart-fallback-notice";
    notice.textContent = message;
    container.insertAdjacentElement("afterend", notice);
  };

  let payload;
  try {
    payload = JSON.parse(dataElement.textContent || "{}");
  } catch {
    payload = null;
  }
  if (!LWC || !payload) {
    showFallback("Biểu đồ chưa tải được. Hãy tải lại trang.");
    return;
  }

  const isDark = () => {
    const theme = document.documentElement.getAttribute("data-theme");
    return theme ? theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  };
  const cssVar = (name, fallback) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  const palette = () => {
    const dark = isDark();
    return {
      background: dark ? "#151b26" : "#ffffff",
      text: dark ? "#94a3b8" : "#475569",
      grid: dark ? "#1e2638" : "#eef2f7",
      border: dark ? "#242f42" : "#cbd5e1",
      up: cssVar("--market-up", "#0ECB81"),
      down: cssVar("--market-down", "#F6465D"),
      flat: cssVar("--market-flat", "#8B98A5"),
      ema20: "#F0B90B",
      ema50: "#3B82F6",
      vwap: dark ? "#E2E8F0" : "#334155",
      sessions: {
        asia: dark ? "rgba(240,185,11,0.10)" : "rgba(240,185,11,0.12)",
        london: dark ? "rgba(59,130,246,0.10)" : "rgba(59,130,246,0.10)",
        new_york: dark ? "rgba(168,85,247,0.10)" : "rgba(168,85,247,0.09)",
      },
      sessionText: dark ? "rgba(226,232,240,0.75)" : "rgba(51,65,85,0.8)",
      levelHigh: "#F6465D",
      levelLow: "#0ECB81",
      trendResistance: "#F97316",
      trendSupport: "#22C55E",
      profileNeutral: dark ? "rgba(148,163,184,0.35)" : "rgba(100,116,139,0.30)",
      profileBuy: "rgba(14,203,129,0.45)",
      profileSell: "rgba(246,70,93,0.45)",
      poc: "#F0B90B",
      valueArea: dark ? "rgba(240,185,11,0.07)" : "rgba(240,185,11,0.08)",
      // CK-3: vùng ICT dùng màu tăng/giảm nhạt; killzone là dải dọc rất nhạt.
      zoneBull: dark ? "rgba(14,203,129,0.16)" : "rgba(14,203,129,0.14)",
      zoneBear: dark ? "rgba(246,70,93,0.16)" : "rgba(246,70,93,0.13)",
      zoneBullLine: "rgba(14,203,129,0.85)",
      zoneBearLine: "rgba(246,70,93,0.85)",
      killzone: {
        killzone: dark ? "rgba(148,163,184,0.07)" : "rgba(100,116,139,0.07)",
        silver_bullet: dark ? "rgba(56,189,248,0.10)" : "rgba(14,165,233,0.09)",
        macro: dark ? "rgba(244,114,182,0.12)" : "rgba(236,72,153,0.10)",
      },
      equalLevel: dark ? "#E2E8F0" : "#334155",
    };
  };

  const chartOptions = (height) => {
    const colors = palette();
    return {
      autoSize: true,
      height,
      layout: {
        // Logo ghi công chèn thẻ <style> bị CSP chặn và gửi hostname qua utm_source;
        // trang có dòng ghi công + NOTICE ở chân nên tắt logo (LayoutOptions.attributionLogo).
        attributionLogo: false,
        background: { color: colors.background },
        textColor: colors.text,
        fontFamily: '"Be Vietnam Pro", system-ui, sans-serif',
      },
      grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
      localization: { locale: "vi-VN", priceFormatter: (v) => fmtNumber(v), timeFormatter: fmtTime },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 8,
        borderColor: colors.border,
        tickMarkFormatter: (time, type) =>
          typeof time === "number" && tickFormats[type]
            ? tickFormats[type].format(new Date(time * 1000))
            : null,
      },
      rightPriceScale: { borderColor: colors.border },
      crosshair: { mode: LWC.CrosshairMode?.Normal ?? 0 },
    };
  };

  // ---- Ánh xạ thời gian → toạ độ, kể cả thời điểm không trùng nến hoặc ngoài dữ liệu ----
  let barTimes = [];
  let intervalSeconds = 60;
  const logicalForTime = (time) => {
    if (barTimes.length === 0) return null;
    let lo = 0;
    let hi = barTimes.length - 1;
    if (time <= barTimes[0]) return (time - barTimes[0]) / intervalSeconds;
    if (time >= barTimes[hi]) return hi + (time - barTimes[hi]) / intervalSeconds;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (barTimes[mid] <= time) lo = mid;
      else hi = mid;
    }
    return lo + (time - barTimes[lo]) / (barTimes[hi] - barTimes[lo]);
  };

  // ---- Primitive cơ sở: vẽ trên canvas của pane theo toạ độ media (px CSS) ----
  class CanvasPrimitive {
    constructor(zOrder = "bottom") {
      this.data = null;
      this.visible = true;
      this.chart = null;
      this.series = null;
      this.requestUpdate = null;
      this._views = [
        {
          zOrder: () => zOrder,
          renderer: () => ({
            draw: (target) => {
              if (!this.visible || !this.data || !this.chart || !this.series) return;
              target.useMediaCoordinateSpace(({ context, mediaSize }) =>
                this.draw(context, mediaSize)
              );
            },
          }),
        },
      ];
    }
    attached({ chart, series, requestUpdate }) {
      this.chart = chart;
      this.series = series;
      this.requestUpdate = requestUpdate;
    }
    detached() {
      this.chart = null;
      this.series = null;
    }
    updateAllViews() {}
    paneViews() {
      return this._views;
    }
    set(data, visible) {
      this.data = data;
      if (visible !== undefined) this.visible = visible;
      if (this.requestUpdate) this.requestUpdate();
    }
    x(time) {
      const logical = logicalForTime(time);
      return logical === null ? null : this.chart.timeScale().logicalToCoordinate(logical);
    }
    y(price) {
      return this.series.priceToCoordinate(price);
    }
  }

  class SessionBoxes extends CanvasPrimitive {
    draw(ctx) {
      const colors = palette();
      ctx.font = '600 10px "Be Vietnam Pro", system-ui, sans-serif';
      for (const item of this.data) {
        const x1 = this.x(item.start);
        const x2 = this.x(item.end);
        const yTop = this.y(item.high);
        const yBottom = this.y(item.low);
        if ([x1, x2, yTop, yBottom].some((v) => v === null)) continue;
        ctx.fillStyle = colors.sessions[item.key] || colors.sessions.asia;
        ctx.fillRect(x1, yTop, Math.max(x2 - x1, 1), Math.max(yBottom - yTop, 1));
        if (x2 - Math.max(x1, 0) > 46) {
          ctx.fillStyle = colors.sessionText;
          ctx.fillText(item.label.replace("Phiên ", ""), Math.max(x1, 0) + 4, yTop - 4);
        }
      }
    }
  }

  class LiquidityLevels extends CanvasPrimitive {
    draw(ctx) {
      const colors = palette();
      ctx.font = '600 10px "Be Vietnam Pro", system-ui, sans-serif';
      for (const level of this.data) {
        const x1 = this.x(level.armed_time);
        const x2 = this.x(level.end_time);
        const y = this.y(level.price);
        if ([x1, x2, y].some((v) => v === null)) continue;
        const color = level.side === "high" ? colors.levelHigh : colors.levelLow;
        ctx.strokeStyle = color;
        ctx.globalAlpha = level.state === "expired" ? 0.35 : 0.9;
        ctx.lineWidth = 1;
        ctx.setLineDash(level.state === "armed" ? [5, 4] : []);
        ctx.beginPath();
        ctx.moveTo(x1, y);
        ctx.lineTo(Math.max(x2, x1 + 1), y);
        ctx.stroke();
        ctx.setLineDash([]);
        if (level.state === "rejected" || level.state === "accepted") {
          // Dấu ✕ tại nến quét: râu vượt mức (rejected = đóng cửa quay lại → quét thanh khoản).
          ctx.beginPath();
          ctx.moveTo(x2 - 4, y - 4);
          ctx.lineTo(x2 + 4, y + 4);
          ctx.moveTo(x2 + 4, y - 4);
          ctx.lineTo(x2 - 4, y + 4);
          ctx.stroke();
        }
        ctx.fillStyle = color;
        ctx.fillText(level.label, x1 + 3, y + (level.side === "high" ? -3 : 11));
        ctx.globalAlpha = 1;
      }
    }
  }

  class Trendlines extends CanvasPrimitive {
    draw(ctx) {
      const colors = palette();
      for (const line of this.data) {
        const x1 = this.x(line.p1.time);
        const y1 = this.y(line.p1.price);
        const x2 = this.x(line.end.time);
        const y2 = this.y(line.end.price);
        if ([x1, y1, x2, y2].some((v) => v === null)) continue;
        ctx.strokeStyle = line.side === "resistance" ? colors.trendResistance : colors.trendSupport;
        ctx.globalAlpha = line.active ? 0.95 : 0.35;
        ctx.lineWidth = line.active ? 1.6 : 1;
        ctx.setLineDash(line.active ? [] : [4, 4]);
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
      }
    }
  }

  class VolumeProfiles extends CanvasPrimitive {
    draw(ctx, size) {
      const colors = palette();
      const maxWidth = Math.min(size.width * 0.22, 180);
      for (const profile of this.data) {
        const xStart = this.x(profile.start);
        const xEnd = this.x(profile.end + intervalSeconds);
        if (xStart === null || xEnd === null) continue;
        const span = Math.max(xEnd - xStart, 8);
        const width = Math.min(span * 0.65, maxWidth);
        const peak = Math.max(...profile.bins.map((b) => b.volume), 0);
        if (peak <= 0) continue;
        const yHigh = this.y(profile.vah);
        const yLow = this.y(profile.val);
        if (yHigh !== null && yLow !== null) {
          ctx.fillStyle = colors.valueArea;
          ctx.fillRect(xStart, yHigh, span, yLow - yHigh);
        }
        for (const bin of profile.bins) {
          if (bin.volume <= 0) continue;
          const yTop = this.y(bin.high);
          const yBottom = this.y(bin.low);
          if (yTop === null || yBottom === null) continue;
          const h = Math.max(yBottom - yTop - 1, 1);
          const total = (bin.volume / peak) * width;
          if (profile.has_split && bin.buy !== null && bin.sell !== null && bin.volume > 0) {
            const buyWidth = total * (bin.buy / (bin.buy + bin.sell));
            ctx.fillStyle = colors.profileBuy;
            ctx.fillRect(xStart, yTop, buyWidth, h);
            ctx.fillStyle = colors.profileSell;
            ctx.fillRect(xStart + buyWidth, yTop, total - buyWidth, h);
            if (bin.category === "high_imbalance") {
              ctx.strokeStyle = colors.poc;
              ctx.strokeRect(xStart, yTop, total, h);
            }
          } else {
            ctx.fillStyle = colors.profileNeutral;
            ctx.fillRect(xStart, yTop, total, h);
          }
        }
        const yPoc = this.y(profile.poc);
        if (yPoc !== null) {
          ctx.strokeStyle = colors.poc;
          ctx.lineWidth = 1.5;
          ctx.setLineDash(profile.complete ? [] : [3, 3]);
          ctx.beginPath();
          ctx.moveTo(xStart, yPoc);
          ctx.lineTo(xStart + span, yPoc);
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }
    }
  }

  // CK-3: dải killzone theo giờ New York — phủ toàn bộ chiều cao pane, nhãn ở mép trên.
  class KillzoneBands extends CanvasPrimitive {
    draw(ctx, size) {
      const colors = palette();
      ctx.font = '600 9px "Be Vietnam Pro", system-ui, sans-serif';
      for (const item of this.data) {
        const x1 = this.x(item.start);
        const x2 = this.x(item.end);
        if (x1 === null || x2 === null || x2 < 0 || x1 > size.width) continue;
        ctx.fillStyle = colors.killzone[item.kind] || colors.killzone.killzone;
        ctx.fillRect(x1, 0, Math.max(x2 - x1, 1), size.height);
        if (x2 - Math.max(x1, 0) > 60 && item.kind !== "macro") {
          ctx.fillStyle = colors.sessionText;
          ctx.fillText(item.label, Math.max(x1, 0) + 3, 10);
        }
      }
    }
  }

  // CK-3: FVG / IFVG / BPR / order block. Vùng còn hiệu lực kéo tới nến cuối; vùng đã lấp hoặc
  // mất hiệu lực vẽ mờ và dừng ở nến kết thúc. IFVG viền nét đứt (đã đảo vai).
  class IctZones extends CanvasPrimitive {
    draw(ctx) {
      const colors = palette();
      ctx.font = '600 9px "Be Vietnam Pro", system-ui, sans-serif';
      for (const zone of this.data) {
        const x1 = this.x(zone.start_time);
        const x2 = this.x(zone.end_time);
        const yTop = this.y(zone.top);
        const yBottom = this.y(zone.bottom);
        if ([x1, x2, yTop, yBottom].some((v) => v === null)) continue;
        // Sau khi đảo vai, màu theo vai trò mới (FVG tăng bị xuyên xuống → kháng cự).
        const bullish = zone.state === "inverted" ? zone.direction === "bearish" : zone.direction === "bullish";
        const live = ["active", "partial", "inverted"].includes(zone.state);
        ctx.globalAlpha = live ? 1 : 0.4;
        ctx.fillStyle = bullish ? colors.zoneBull : colors.zoneBear;
        const width = Math.max(x2 - x1, 2);
        const height = Math.max(yBottom - yTop, 1);
        ctx.fillRect(x1, yTop, width, height);
        ctx.strokeStyle = bullish ? colors.zoneBullLine : colors.zoneBearLine;
        ctx.lineWidth = zone.kind === "order_block" ? 1.2 : 0.8;
        ctx.setLineDash(zone.state === "inverted" ? [4, 3] : zone.kind === "bpr" ? [2, 2] : []);
        ctx.strokeRect(x1, yTop, width, height);
        ctx.setLineDash([]);
        if (live && width > 34) {
          ctx.fillStyle = ctx.strokeStyle;
          ctx.fillText(zone.label, x1 + 3, yTop + Math.min(height - 2, 10));
        }
        ctx.globalAlpha = 1;
      }
    }
  }

  // CK-3: đỉnh/đáy bằng nhau (EQH/EQL) — đường chấm, kết thúc ở nến quét.
  class EqualLevels extends CanvasPrimitive {
    draw(ctx) {
      const colors = palette();
      ctx.font = '600 9px "Be Vietnam Pro", system-ui, sans-serif';
      for (const level of this.data) {
        const x1 = this.x(level.start_time);
        const x2 = this.x(level.end_time);
        const y = this.y(level.price);
        if ([x1, x2, y].some((v) => v === null)) continue;
        ctx.strokeStyle = colors.equalLevel;
        ctx.fillStyle = colors.equalLevel;
        ctx.globalAlpha = level.swept ? 0.4 : 0.85;
        ctx.lineWidth = 1;
        ctx.setLineDash([1, 3]);
        ctx.beginPath();
        ctx.moveTo(x1, y);
        ctx.lineTo(Math.max(x2, x1 + 1), y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillText(level.side === "high" ? "EQH" : "EQL", x1 + 2, y + (level.side === "high" ? -3 : 10));
        ctx.globalAlpha = 1;
      }
    }
  }

  // ---- Dựng chart ----
  const colors = palette();
  const chart = LWC.createChart(container, chartOptions(460));
  const candles = chart.addSeries(LWC.CandlestickSeries, {
    upColor: colors.up,
    downColor: colors.down,
    borderUpColor: colors.up,
    borderDownColor: colors.down,
    wickUpColor: colors.up,
    wickDownColor: colors.down,
  });
  const volume = chart.addSeries(LWC.HistogramSeries, {
    priceScaleId: "volume",
    priceFormat: { type: "volume" },
    lastValueVisible: false,
    priceLineVisible: false,
  });
  chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  const lineOptions = (color, title) => ({
    color,
    lineWidth: 1,
    title,
    lastValueVisible: false,
    priceLineVisible: false,
    crosshairMarkerVisible: false,
  });
  const ema20 = chart.addSeries(LWC.LineSeries, lineOptions(colors.ema20, "EMA20"));
  const ema50 = chart.addSeries(LWC.LineSeries, lineOptions(colors.ema50, "EMA50"));
  const vwap = chart.addSeries(LWC.LineSeries, {
    ...lineOptions(colors.vwap, "VWAP"),
    lineStyle: LWC.LineStyle?.Dashed ?? 2,
  });
  const markers = LWC.createSeriesMarkers(candles, []);
  const sessions = new SessionBoxes("bottom");
  const levels = new LiquidityLevels("top");
  const trendlines = new Trendlines("top");
  const profiles = new VolumeProfiles("bottom");
  const killzones = new KillzoneBands("bottom");
  const ictZones = new IctZones("bottom");
  const equalLevels = new EqualLevels("top");
  for (const primitive of [killzones, sessions, profiles, ictZones, levels, equalLevels, trendlines]) {
    candles.attachPrimitive(primitive);
  }

  // Pane phụ RSI/MACD là chart riêng, đồng bộ vùng nhìn theo chỉ số logic.
  const subCharts = [];
  const makeSubChart = (elementId, build) => {
    const element = document.getElementById(elementId);
    if (!element) return null;
    const sub = LWC.createChart(element, {
      ...chartOptions(140),
      timeScale: { ...chartOptions(140).timeScale, visible: false },
    });
    const api = build(sub);
    const entry = { element, chart: sub, ...api };
    subCharts.push(entry);
    return entry;
  };
  const rsiPane = makeSubChart("market-chart-rsi", (sub) => {
    const series = sub.addSeries(LWC.LineSeries, lineOptions("#A855F7", "RSI14"));
    series.createPriceLine({ price: 70, color: colors.flat, lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
    series.createPriceLine({ price: 30, color: colors.flat, lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
    return { update: (p) => series.setData(p.panes.rsi_14 || []) };
  });
  const macdPane = makeSubChart("market-chart-macd", (sub) => {
    const histogram = sub.addSeries(LWC.HistogramSeries, { lastValueVisible: false, priceLineVisible: false });
    const line = sub.addSeries(LWC.LineSeries, lineOptions("#3B82F6", "MACD"));
    const signal = sub.addSeries(LWC.LineSeries, lineOptions("#F97316", "Tín hiệu"));
    return {
      update: (p) => {
        const c = palette();
        const macd = p.panes.macd || {};
        histogram.setData(
          (macd.histogram || []).map((point) => ({
            ...point,
            color: point.value >= 0 ? `${c.up}99` : `${c.down}99`,
          }))
        );
        line.setData(macd.macd || []);
        signal.setData(macd.signal || []);
      },
    };
  });
  chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    if (!range) return;
    for (const sub of subCharts) {
      if (!sub.element.hidden) sub.chart.timeScale().setVisibleLogicalRange(range);
    }
  });

  // ---- Lớp bật/tắt ----
  const layerInputs = Array.from(document.querySelectorAll("[data-layer]"));
  const layerOn = (key) => {
    const input = layerInputs.find((element) => element.dataset.layer === key);
    return input ? input.checked : true;
  };
  const STORAGE_KEY = "crypidian.marketChart.layers";
  try {
    const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}");
    for (const input of layerInputs) {
      if (typeof saved[input.dataset.layer] === "boolean") input.checked = saved[input.dataset.layer];
    }
  } catch {
    /* localStorage có thể bị chặn; dùng mặc định. */
  }

  const buildMarkers = (p) => {
    const c = palette();
    const list = [];
    if (layerOn("structure")) {
      for (const swing of p.structure.swings) {
        if (!swing.label) continue;
        list.push({
          time: swing.time,
          position: swing.kind === "high" ? "aboveBar" : "belowBar",
          color: c.flat,
          shape: "circle",
          size: 0.4,
          text: swing.label,
        });
      }
      for (const item of p.structure.breaks) {
        list.push({
          time: item.time,
          position: item.direction === "bullish" ? "belowBar" : "aboveBar",
          color: item.direction === "bullish" ? c.up : c.down,
          shape: item.direction === "bullish" ? "arrowUp" : "arrowDown",
          text: item.kind === "CHOCH" ? "CHoCH" : "BOS",
        });
      }
    }
    if (layerOn("equal_levels") && p.ict) {
      for (const swing of p.ict.intermediate_swings) {
        list.push({
          time: swing.time,
          position: swing.kind === "high" ? "aboveBar" : "belowBar",
          color: c.flat,
          shape: "square",
          size: 0.3,
          text: swing.label,
        });
      }
    }
    if (layerOn("levels")) {
      for (const level of p.levels) {
        if (level.state !== "rejected" || level.event_time === null) continue;
        list.push({
          time: level.event_time,
          position: level.side === "high" ? "aboveBar" : "belowBar",
          color: level.side === "high" ? c.down : c.up,
          shape: "square",
          size: 0.6,
          text: "Quét",
        });
      }
    }
    return list.sort((a, b) => a.time - b.time);
  };

  const TREND_VI = { up: "Tăng (theo BOS)", down: "Giảm (theo BOS)" };
  const STATE_VI = {
    uptrend: "Hai line cùng dốc lên",
    downtrend: "Hai line cùng dốc xuống",
    expanding: "Mở rộng (nới biên độ)",
    contracting: "Thu hẹp (nén giá)",
    range: "Đi ngang",
    unknown: "Chưa đủ swing",
  };
  const setText = (selector, text) => {
    const element = document.querySelector(selector);
    if (element) element.textContent = text;
  };

  const renderEvents = (p) => {
    const list = document.querySelector("[data-mc-events]");
    if (!list) return;
    const events = [
      ...p.structure.breaks.map((item) => ({
        time: item.time,
        text: `${item.kind === "CHOCH" ? "CHoCH" : "BOS"} ${item.direction === "bullish" ? "tăng" : "giảm"}: giá đóng cửa vượt swing ${fmtNumber(item.level)}`,
      })),
      ...p.levels
        .filter((level) => level.event_time !== null)
        .map((level) => ({
          time: level.event_time,
          text:
            level.state === "rejected"
              ? `Quét thanh khoản ${level.label} ${fmtNumber(level.price)} rồi đóng cửa quay lại`
              : `Giá đóng cửa vượt và giữ ngoài ${level.label} ${fmtNumber(level.price)}`,
        })),
      ...((p.ict && p.ict.zones) || []).map((zone) => ({
        time: zone.confirmed_time,
        text: `${zone.kind === "fvg" ? (zone.direction === "bullish" ? "FVG tăng" : "FVG giảm") : zone.label} xác nhận: vùng ${fmtNumber(zone.bottom)} – ${fmtNumber(zone.top)}`,
      })),
      ...((p.ict && p.ict.equal_levels) || [])
        .filter((level) => level.swept)
        .map((level) => ({
          time: level.end_time,
          text: `Quét ${level.label} ${fmtNumber(level.price)}`,
        })),
    ]
      .sort((a, b) => b.time - a.time)
      .slice(0, 12);
    const items = events.map((event) => {
      const li = document.createElement("li");
      const time = document.createElement("time");
      time.dateTime = new Date(event.time * 1000).toISOString();
      time.textContent = fmtTime(event.time);
      li.append(time, document.createTextNode(` — ${event.text}`));
      return li;
    });
    if (items.length === 0) {
      const li = document.createElement("li");
      li.textContent = "Chưa có sự kiện cấu trúc hoặc quét thanh khoản trong vùng dữ liệu.";
      items.push(li);
    }
    list.replaceChildren(...items);
  };

  const renderNotices = (p) => {
    const list = document.querySelector("[data-mc-notices]");
    if (!list) return;
    list.replaceChildren(
      ...p.notices.map((text) => {
        const li = document.createElement("li");
        li.textContent = text;
        return li;
      })
    );
  };

  // Thẻ tín hiệu (SIG-3): đường giá SL/mục tiêu và dải vùng vào; xoá rồi vẽ lại mỗi lần render.
  let signalLines = [];
  const drawSignal = (p) => {
    for (const line of signalLines) candles.removePriceLine(line);
    signalLines = [];
    const card = p.signal_card;
    if (!card || !layerOn("signal")) return;
    const c = palette();
    const add = (price, color, title, style) =>
      signalLines.push(
        candles.createPriceLine({ price, color, title, lineWidth: 1, lineStyle: style, axisLabelVisible: true })
      );
    add(card.stop, c.down, "SL", 0);
    add(card.entry_zone.low, c.flat, "Vào", 2);
    if (card.entry_zone.high !== card.entry_zone.low) add(card.entry_zone.high, c.flat, "Vào", 2);
    for (const target of card.targets) {
      add(target.price, target.source === "recipe" ? c.up : `${c.up}99`, target.source === "recipe" ? "TP" : `${target.r_multiple}R`, target.source === "recipe" ? 0 : 3);
    }
  };

  // CK-1: đường bid/ask trực tiếp và giá hiện tại kèm đếm ngược đóng nến (market-overview.js
  // phát sự kiện). Chỉ vẽ khi giá còn mới; giá cũ/thiếu thì không vẽ để tránh hiểu nhầm.
  let quoteLines = [];
  let liveLine = null;
  let liveOverview = null;
  let clockText = "";
  const drawQuote = () => {
    for (const line of quoteLines) candles.removePriceLine(line);
    quoteLines = [];
    if (liveLine) candles.removePriceLine(liveLine);
    liveLine = null;
    const o = liveOverview;
    if (!o || !layerOn("quote")) return;
    const c = palette();
    if (o.quote && !o.quote.stale) {
      for (const [value, color, title] of [
        [o.quote.bid, c.up, "Bid"],
        [o.quote.ask, c.down, "Ask"],
      ]) {
        quoteLines.push(
          candles.createPriceLine({ price: value, color, title, lineWidth: 1, lineStyle: 1, axisLabelVisible: true })
        );
      }
    }
    if (o.candle && o.candle.price !== null && o.candle.price_source !== "last_close") {
      liveLine = candles.createPriceLine({
        price: o.candle.price,
        color: c.flat,
        title: clockText,
        lineWidth: 1,
        lineStyle: 3,
        axisLabelVisible: true,
      });
    }
  };
  document.addEventListener("mc:overview", (event) => {
    liveOverview = event.detail;
    drawQuote();
  });
  document.addEventListener("mc:clock", (event) => {
    clockText = event.detail.text || "";
    if (liveLine) liveLine.applyOptions({ title: clockText });
  });

  const applyLayers = () => {
    const p = payload;
    drawSignal(p);
    drawQuote();
    ema20.applyOptions({ visible: layerOn("ema") });
    ema50.applyOptions({ visible: layerOn("ema") });
    vwap.applyOptions({ visible: layerOn("vwap") });
    sessions.set(p.sessions, layerOn("sessions"));
    levels.set(p.levels, layerOn("levels"));
    trendlines.set(p.trendlines, layerOn("trendlines"));
    profiles.set(p.volume_profiles, layerOn("profile"));
    const ict = p.ict || { killzones: [], zones: [], equal_levels: [] };
    killzones.set(ict.killzones, layerOn("killzones"));
    ictZones.set(ict.zones, layerOn("ict_zones"));
    equalLevels.set(ict.equal_levels, layerOn("equal_levels"));
    markers.setMarkers(buildMarkers(p));
    if (rsiPane) rsiPane.element.hidden = !layerOn("rsi");
    if (macdPane) macdPane.element.hidden = !layerOn("macd");
    const range = chart.timeScale().getVisibleLogicalRange();
    if (range) {
      for (const sub of subCharts) {
        if (!sub.element.hidden) sub.chart.timeScale().setVisibleLogicalRange(range);
      }
    }
  };

  let firstRender = true;
  const render = (p) => {
    payload = p;
    barTimes = p.bars.map((bar) => bar.time);
    intervalSeconds = barTimes.length > 1 ? barTimes[1] - barTimes[0] : 60;
    const c = palette();
    candles.setData(p.bars);
    volume.setData(
      p.volume.map((item) => ({
        time: item.time,
        value: item.value,
        color:
          item.tone === "up" ? `${c.up}66` : item.tone === "down" ? `${c.down}66` : `${c.flat}66`,
      }))
    );
    ema20.setData(p.lines.ema_20 || []);
    ema50.setData(p.lines.ema_50 || []);
    vwap.setData(p.lines.session_vwap || []);
    for (const sub of subCharts) sub.update(p);
    applyLayers();
    if (firstRender && p.bars.length > 0) {
      const visible = Math.min(p.bars.length, window.innerWidth < 640 ? 90 : 180);
      chart.timeScale().setVisibleLogicalRange({ from: p.bars.length - visible, to: p.bars.length + 6 });
      firstRender = false;
    }
    const lastSweep = [...p.levels]
      .filter((level) => level.state === "rejected")
      .sort((a, b) => b.event_time - a.event_time)[0];
    setText("[data-mc-source]", p.source_label || "—");
    setText("[data-mc-trend]", TREND_VI[p.structure.trend] || "Chưa xác định");
    setText("[data-mc-state]", STATE_VI[p.structure.state] || "—");
    setText(
      "[data-mc-sweep]",
      lastSweep ? `${lastSweep.label} · ${fmtTime(lastSweep.event_time)}` : "Chưa có"
    );
    setText("[data-mc-updated]", fmtTime(Date.parse(p.generated_at) / 1000));
    setText("[data-mc-ict-count]", p.ict ? String(p.ict.counts.active) : "—");
    renderEvents(p);
    renderNotices(p);
  };

  for (const input of layerInputs) {
    input.addEventListener("change", () => {
      try {
        const state = Object.fromEntries(layerInputs.map((el) => [el.dataset.layer, el.checked]));
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
      } catch {
        /* bỏ qua khi không ghi được localStorage */
      }
      applyLayers();
    });
  }

  // Chú giải OHLC + khối lượng theo con trỏ.
  const legend = document.getElementById("mc-legend");
  chart.subscribeCrosshairMove((param) => {
    if (!legend || !param || param.time === undefined) return;
    const bar = param.seriesData.get(candles);
    const vol = payload.volume.find((item) => item.time === param.time);
    if (!bar) return;
    const parts = [
      fmtTime(param.time),
      `Mở ${fmtNumber(bar.open)}`,
      `Cao ${fmtNumber(bar.high)}`,
      `Thấp ${fmtNumber(bar.low)}`,
      `Đóng ${fmtNumber(bar.close)}`,
    ];
    if (vol) parts.push(`KL ${fmtNumber(vol.value, 3)}`);
    if (vol && vol.delta !== null && vol.delta !== undefined) parts.push(`Delta ${fmtNumber(vol.delta, 3)}`);
    legend.textContent = parts.join(" · ");
  });

  // Đổi theme: cập nhật màu chart.
  new MutationObserver(() => {
    const opts = chartOptions(460);
    for (const target of [chart, ...subCharts.map((sub) => sub.chart)]) {
      target.applyOptions({ layout: opts.layout, grid: opts.grid });
    }
    render(payload);
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  try {
    render(payload);
  } catch (error) {
    showFallback("Biểu đồ gặp lỗi khi vẽ; dữ liệu sự kiện bên dưới vẫn dùng được.");
    return;
  }

  // Polling 5 giây khi tab hiển thị; dừng hẳn khi mất quyền.
  const botId = container.dataset.botId;
  const symbol = container.dataset.symbol;
  const timeframe = container.dataset.timeframe;
  let stopped = false;
  const poll = async () => {
    if (stopped || document.visibilityState !== "visible") return;
    const params = new URLSearchParams({
      bot_id: botId,
      symbol,
      timeframe,
      fp: payload.fingerprint || "",
    });
    const url = `/admin/api/chart?${params.toString()}`;
    try {
      const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if ([401, 403, 404].includes(response.status)) {
        stopped = true;
        return;
      }
      if (!response.ok) return;
      const next = await response.json();
      if (next.status === "ok") render(next);
    } catch {
      /* giữ dữ liệu cũ khi lỗi mạng */
    }
  };
  window.setInterval(poll, POLL_MS);
})();
