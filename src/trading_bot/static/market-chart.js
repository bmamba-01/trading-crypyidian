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
      // CK-5: ZigZag và nhóm VSA (không dùng xanh/đỏ để khỏi lẫn với nến tăng/giảm).
      zigzag: dark ? "#C4B5FD" : "#6D28D9",
      vsaStrength: "#2563EB",
      vsaWeakness: "#EA580C",
      // CK-4: TPO dùng màu lam để phân biệt với volume profile (xám/xanh-đỏ).
      tpo: dark ? "rgba(96,165,250,0.40)" : "rgba(37,99,235,0.28)",
      tpoPoc: dark ? "#93C5FD" : "#1D4ED8",
      npoc: "#F0B90B",
      valueLine: dark ? "rgba(226,232,240,0.7)" : "rgba(51,65,85,0.7)",
      // CK-7: mục tiêu TP đã chạm dùng màu xanh dịu + nét đứt để phân biệt với TP đang chờ.
      tpHit: dark ? "rgba(14,203,129,0.52)" : "rgba(11,138,90,0.52)",
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

  // CK-4: TPO — cột phản chiếu bám mép phải của ngày, song song với volume profile bên trái.
  class TpoProfiles extends CanvasPrimitive {
    draw(ctx, size) {
      const colors = palette();
      const maxWidth = Math.min(size.width * 0.18, 150);
      for (const profile of this.data) {
        const tpo = profile.tpo;
        if (!tpo) continue;
        const xStart = this.x(profile.start);
        const xEnd = this.x(profile.end + intervalSeconds);
        if (xStart === null || xEnd === null) continue;
        const span = Math.max(xEnd - xStart, 8);
        const width = Math.min(span * 0.45, maxWidth);
        const peak = Math.max(...tpo.rows.map((row) => row.count), 0);
        if (peak <= 0) continue;
        ctx.fillStyle = colors.tpo;
        for (const row of tpo.rows) {
          if (row.count <= 0) continue;
          const yTop = this.y(row.high);
          const yBottom = this.y(row.low);
          if (yTop === null || yBottom === null) continue;
          const w = (row.count / peak) * width;
          ctx.fillRect(xEnd - w, yTop, w, Math.max(yBottom - yTop - 1, 1));
        }
        const yPoc = this.y(tpo.poc);
        if (yPoc !== null) {
          ctx.strokeStyle = colors.tpoPoc;
          ctx.lineWidth = 1.5;
          ctx.setLineDash([2, 2]);
          ctx.beginPath();
          ctx.moveTo(xEnd - width, yPoc);
          ctx.lineTo(xEnd, yPoc);
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }
    }
  }

  // CK-4: POC "trần" (vàng, nét đứt) và VAH/VAL ngày trước (xám) kéo tới nến cuối.
  class ProfileLevels extends CanvasPrimitive {
    draw(ctx) {
      const colors = palette();
      ctx.font = '600 9px "Be Vietnam Pro", system-ui, sans-serif';
      for (const level of this.data) {
        const x1 = this.x(level.start_time);
        const x2 = this.x(level.end_time);
        const y = this.y(level.price);
        if ([x1, x2, y].some((v) => v === null)) continue;
        const naked = level.kind.startsWith("npoc");
        const color = naked ? (level.kind === "npoc_tpo" ? colors.tpoPoc : colors.npoc) : colors.valueLine;
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 1;
        ctx.setLineDash(naked ? [6, 3] : [3, 3]);
        ctx.beginPath();
        ctx.moveTo(x1, y);
        ctx.lineTo(Math.max(x2, x1 + 1), y);
        ctx.stroke();
        ctx.setLineDash([]);
        // Nhãn bám mép trái vùng nhìn để vẫn đọc được khi điểm đầu đã cuộn khỏi khung.
        ctx.fillText(level.label, Math.max(x1, 0) + 3, y - 3);
      }
    }
  }

  // CK-5: ZigZag — nét liền qua các điểm đã chốt; nét đứt từ điểm chốt cuối tới pivot chưa
  // chốt và tới giá đóng nến cuối, kèm chữ "chưa chốt".
  class ZigZagLine extends CanvasPrimitive {
    draw(ctx) {
      const zig = this.data;
      if (!zig) return;
      const colors = palette();
      const toXY = (point) => {
        const x = this.x(point.time);
        const y = this.y(point.price);
        return x === null || y === null ? null : [x, y];
      };
      const finals = zig.points.map(toXY).filter((v) => v !== null);
      ctx.strokeStyle = colors.zigzag;
      ctx.lineWidth = 1.6;
      if (finals.length > 1) {
        ctx.beginPath();
        ctx.moveTo(finals[0][0], finals[0][1]);
        for (const [x, y] of finals.slice(1)) ctx.lineTo(x, y);
        ctx.stroke();
      }
      const tail = [zig.points[zig.points.length - 1], zig.pending, zig.last]
        .filter(Boolean)
        .map(toXY)
        .filter((v) => v !== null);
      if (tail.length > 1) {
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.moveTo(tail[0][0], tail[0][1]);
        for (const [x, y] of tail.slice(1)) ctx.lineTo(x, y);
        ctx.stroke();
        ctx.setLineDash([]);
        const [lx, ly] = tail[tail.length - 2];
        ctx.fillStyle = colors.zigzag;
        ctx.font = '600 9px "Be Vietnam Pro", system-ui, sans-serif';
        ctx.fillText("chưa chốt", lx + 4, ly - 4);
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
  const tpoProfiles = new TpoProfiles("bottom");
  const profileLevels = new ProfileLevels("top");
  const zigzagLine = new ZigZagLine("top");
  for (const primitive of [
    killzones,
    sessions,
    profiles,
    tpoProfiles,
    ictZones,
    levels,
    profileLevels,
    equalLevels,
    trendlines,
    zigzagLine,
  ]) {
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
  const settingsDialog = document.querySelector("[data-chart-settings]");
  const settingsOpenButton = document.querySelector("[data-chart-settings-open]");
  const settingsCloseButton = document.querySelector("[data-chart-settings-close]");
  settingsOpenButton?.addEventListener("click", () => {
    if (!settingsDialog || typeof settingsDialog.showModal !== "function") return;
    settingsDialog.showModal();
    settingsOpenButton.setAttribute("aria-expanded", "true");
    settingsCloseButton?.focus();
  });
  settingsCloseButton?.addEventListener("click", () => settingsDialog?.close());
  settingsDialog?.addEventListener("close", () => {
    settingsOpenButton?.setAttribute("aria-expanded", "false");
  });
  settingsDialog?.addEventListener("click", (event) => {
    if (event.target === settingsDialog) settingsDialog.close();
  });
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
    if (layerOn("paper") && liveOverview && liveOverview.positions) {
      // CK-7: chú thích "PAPER đã khớp · giá" tại nến chứa thời điểm khớp đầu tiên.
      for (const position of liveOverview.positions) {
        const filled = Date.parse(position.filled_at) / 1000;
        const barTime = [...barTimes].reverse().find((time) => time <= filled);
        if (barTime === undefined) continue;
        list.push({
          time: barTime,
          position: position.side === "LONG" ? "belowBar" : "aboveBar",
          color: position.side === "LONG" ? c.up : c.down,
          shape: position.side === "LONG" ? "arrowUp" : "arrowDown",
          text: `PAPER đã khớp · ${fmtNumber(position.fill_price)}`,
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
  // CK-6: kịch bản được ghim từ bảng Entry thay cho thẻ của bot đang chọn.
  let pinnedCard = null;
  const pinSelect = document.querySelector("[data-setup-pin]");
  const setupCardsNode = document.getElementById("market-setups-data");
  if (pinSelect && setupCardsNode) {
    let setupCards = [];
    try {
      setupCards = JSON.parse(setupCardsNode.textContent || "[]");
    } catch {
      setupCards = [];
    }
    pinSelect.addEventListener("change", () => {
      const chosen = setupCards[Number(pinSelect.value)];
      pinnedCard = pinSelect.value === "" || !chosen ? null : chosen.card;
      drawSignal(payload);
    });
  }
  const drawSignal = (p) => {
    for (const line of signalLines) candles.removePriceLine(line);
    signalLines = [];
    const card = pinnedCard || p.signal_card;
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
      const isLadder = target.source === "plan_ladder" || target.source === "candidate_ladder";
      const isPrimary = target.source === "recipe" || isLadder;
      const title = isLadder ? target.label || "TP" : target.source === "recipe" ? "TP" : `${target.r_multiple}R`;
      add(target.price, isPrimary ? c.up : `${c.up}99`, title, isPrimary ? 0 : 3);
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
  // CK-7: vị thế PAPER đang mở — đường entry/SL/TP (gồm thang TP1–3 kèm trạng thái đã chạm), nhãn UPnL theo mark price.
  let positionLines = [];
  let positionLineMeta = [];
  const fmtTargetFraction = (fraction) => {
    if (typeof fraction !== "number" || !Number.isFinite(fraction)) return "";
    return `${fmtNumber(Math.round(fraction * 1000) / 10, 1)}%`;
  };
  const drawPositions = () => {
    for (const line of positionLines) candles.removePriceLine(line);
    positionLines = [];
    positionLineMeta = [];
    const positions = (liveOverview && liveOverview.positions) || [];
    if (!layerOn("paper")) return;
    const c = palette();
    for (const position of positions) {
      const pnl =
        position.upnl === null
          ? "UPnL chưa có giá"
          : `UPnL ${position.upnl >= 0 ? "+" : ""}${fmtNumber(position.upnl, 2)} ${position.quote_asset || ""}${
              position.r_multiple === null ? "" : ` (${fmtNumber(position.r_multiple, 2)}R)`
            }`;
      const add = (price, color, title, style, lineWidth = 2, hit = null) => {
        if (price === null || price === undefined) return;
        positionLines.push(
          candles.createPriceLine({ price, color, title, lineWidth, lineStyle: style, axisLabelVisible: true })
        );
        positionLineMeta.push({ price, title, hit });
      };
      add(position.entry_price, c.flat, `PAPER ${position.side} · ${pnl}`, 0);
      add(position.stop, c.down, "SL PAPER", 0);
      if (Array.isArray(position.targets) && position.targets.length > 0) {
        for (const target of position.targets) {
          const idx = target.index || 1;
          const pct = fmtTargetFraction(target.close_fraction);
          if (target.hit) {
            add(target.price, c.tpHit, `TP${idx} ✓ (đã chạm)`, 2, 1, true);
          } else {
            const label = pct ? `TP${idx} (${pct})` : `TP${idx}`;
            add(target.price, c.up, label, 0, 2, false);
          }
        }
      } else {
        add(position.target, c.up, "TP PAPER", 0);
      }
    }
  };
  document.addEventListener("mc:overview", (event) => {
    const before = JSON.stringify(((liveOverview && liveOverview.positions) || []).map((p) => p.trade_id));
    liveOverview = event.detail;
    drawQuote();
    drawPositions();
    const after = JSON.stringify((liveOverview.positions || []).map((p) => p.trade_id));
    if (before !== after) markers.setMarkers(buildMarkers(payload));
  });
  document.addEventListener("mc:clock", (event) => {
    clockText = event.detail.text || "";
    if (liveLine) liveLine.applyOptions({ title: clockText });
  });

  // Volume mặc định tô theo delta (hoặc chiều nến); bật lớp VSA thì nến có nhãn VSA tô theo
  // nhóm: lam = dấu hiệu sức mạnh (phía cầu), cam = dấu hiệu yếu (phía cung), nến khác mờ đi.
  const paintVolume = (p) => {
    const c = palette();
    const vsaOn = layerOn("vsa");
    const groups = new Map((p.vsa || []).map((item) => [item.time, item.group]));
    volume.setData(
      p.volume.map((item) => {
        const group = vsaOn ? groups.get(item.time) : undefined;
        let color =
          item.tone === "up" ? `${c.up}66` : item.tone === "down" ? `${c.down}66` : `${c.flat}66`;
        if (group === "strength") color = c.vsaStrength;
        else if (group === "weakness") color = c.vsaWeakness;
        else if (vsaOn) color = `${c.flat}33`;
        return { time: item.time, value: item.value, color };
      })
    );
  };

  const applyLayers = () => {
    const p = payload;
    paintVolume(p);
    zigzagLine.set(p.zigzag || null, layerOn("zigzag"));
    drawSignal(p);
    drawQuote();
    drawPositions();
    ema20.applyOptions({ visible: layerOn("ema") });
    ema50.applyOptions({ visible: layerOn("ema") });
    vwap.applyOptions({ visible: layerOn("vwap") });
    sessions.set(p.sessions, layerOn("sessions"));
    levels.set(p.levels, layerOn("levels"));
    trendlines.set(p.trendlines, layerOn("trendlines"));
    profiles.set(p.volume_profiles, layerOn("profile"));
    tpoProfiles.set(p.volume_profiles || [], layerOn("tpo"));
    profileLevels.set(p.profile_levels || [], layerOn("profile_levels"));
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
    paintVolume(p);
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
      updateLayerCount();
    });
  }

  // CK-8: đếm "Lớp a/b" và bộ lớp dựng sẵn (preset); chọn preset rồi đổi tay → "Tự chọn".
  const LAYER_PRESETS = {
    ict: ["structure", "levels", "sessions", "ict_zones", "equal_levels", "killzones", "signal", "paper", "quote"],
    volume: ["vwap", "profile", "tpo", "profile_levels", "vsa", "signal", "paper", "quote"],
    minimal: ["ema", "signal", "paper", "quote"],
  };
  const layerCounts = Array.from(document.querySelectorAll("[data-layer-count]"));
  const presetSelect = document.querySelector("[data-layer-preset]");
  const updateLayerCount = () => {
    const enabledCount = layerInputs.filter((el) => el.checked).length;
    const label = `Lớp ${enabledCount}/${layerInputs.length}`;
    for (const layerCount of layerCounts) {
      layerCount.textContent = layerCount.hasAttribute("data-layer-count-compact")
        ? `${enabledCount}/${layerInputs.length}`
        : label;
    }
    settingsOpenButton?.setAttribute("aria-label", `Mở cài đặt lớp hiển thị, ${label}`);
  };
  presetSelect?.addEventListener("change", () => {
    const preset = presetSelect.value;
    if (!preset) return;
    for (const input of layerInputs) {
      const on =
        preset === "default" ? input.dataset.default === "on" : LAYER_PRESETS[preset].includes(input.dataset.layer);
      if (input.checked !== on) {
        input.checked = on;
        input.dispatchEvent(new Event("change"));
      }
    }
    presetSelect.value = preset;
    updateLayerCount();
  });
  for (const input of layerInputs) {
    input.addEventListener("input", () => {
      if (presetSelect) presetSelect.value = "";
    });
  }
  updateLayerCount();

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
    // Nhãn VSA bằng chữ để không chỉ dựa vào màu (CK-5).
    const vsa = (payload.vsa || []).find((item) => item.time === param.time);
    if (vsa) parts.push(`VSA: ${vsa.label}`);
    // CK-8: giá trị chỉ báo đang bật tại con trỏ.
    for (const [series, label, key] of [
      [ema20, "EMA20", "ema"],
      [ema50, "EMA50", "ema"],
      [vwap, "VWAP", "vwap"],
    ]) {
      const point = param.seriesData.get(series);
      if (layerOn(key) && point && point.value !== undefined) parts.push(`${label} ${fmtNumber(point.value, 2)}`);
    }
    legend.textContent = parts.join(" · ");
  });

  // ---- CK-7: điều khiển vùng nhìn, phím tắt, cỡ chart ----
  const timeScale = chart.timeScale();
  const visibleBars = () => Math.min(barTimes.length, window.innerWidth < 640 ? 90 : 180);
  const actions = {
    "zoom-in": () => {
      const range = timeScale.getVisibleLogicalRange();
      if (!range) return;
      const span = (range.to - range.from) * 0.8;
      timeScale.setVisibleLogicalRange({ from: range.to - Math.max(span, 10), to: range.to });
    },
    "zoom-out": () => {
      const range = timeScale.getVisibleLogicalRange();
      if (!range) return;
      const span = (range.to - range.from) * 1.25;
      timeScale.setVisibleLogicalRange({ from: range.to - span, to: range.to });
    },
    home: () => {
      const range = timeScale.getVisibleLogicalRange();
      const span = range ? range.to - range.from : visibleBars();
      timeScale.setVisibleLogicalRange({ from: -2, to: span - 2 });
    },
    live: () => timeScale.scrollToRealTime(),
    reset: () =>
      timeScale.setVisibleLogicalRange({
        from: barTimes.length - visibleBars(),
        to: barTimes.length + 6,
      }),
  };
  for (const button of document.querySelectorAll("[data-chart-action]")) {
    button.addEventListener("click", () => actions[button.dataset.chartAction]?.());
  }
  container.addEventListener("dblclick", () => {
    // Đang dùng công cụ vẽ thì nhấp đúp là đặt điểm, không đặt lại vùng nhìn (CK-8).
    if (window.CrypidianChart?.isDrawing?.()) return;
    actions.reset();
  });
  container.addEventListener("keydown", (event) => {
    // Không chặn phím có tổ hợp (trợ năng, phím tắt trình duyệt) và phím Tab.
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const key = { "+": "zoom-in", "=": "zoom-in", "-": "zoom-out", Home: "home", End: "live", 0: "reset" }[event.key];
    if (!key) return;
    event.preventDefault();
    actions[key]();
  });

  // CK-7: kéo chart bằng chuột giữa (chặn autoscroll mặc định ở pha capture để canvas của
  // Lightweight Charts không nuốt sự kiện; giữ nguyên chuột trái và zoom cuộn).
  let middleDrag = null;
  const stopMiddleDrag = () => {
    if (!middleDrag) return;
    middleDrag = null;
    container.classList.remove("is-middle-panning");
  };
  container.addEventListener(
    "mousedown",
    (event) => {
      if (event.button !== 1) return;
      event.preventDefault();
      event.stopPropagation();
      const startRange = timeScale.getVisibleLogicalRange();
      const width = container.clientWidth;
      if (!startRange || width <= 0) return;
      middleDrag = { startX: event.clientX, startRange, width };
      container.classList.add("is-middle-panning");
    },
    true
  );
  container.addEventListener(
    "auxclick",
    (event) => {
      if (event.button === 1) {
        event.preventDefault();
        event.stopPropagation();
      }
    },
    true
  );
  window.addEventListener(
    "mousemove",
    (event) => {
      if (!middleDrag) return;
      const dx = event.clientX - middleDrag.startX;
      const span = middleDrag.startRange.to - middleDrag.startRange.from;
      const deltaBars = -(dx / middleDrag.width) * span;
      timeScale.setVisibleLogicalRange({
        from: middleDrag.startRange.from + deltaBars,
        to: middleDrag.startRange.to + deltaBars,
      });
    },
    true
  );
  window.addEventListener("mouseup", stopMiddleDrag, true);
  window.addEventListener("blur", stopMiddleDrag);

  const SIZE_KEY = "crypidian.marketChart.size";
  const sizeSelect = document.querySelector("[data-chart-size]");
  const applySize = (size) => {
    container.classList.toggle("mc-canvas--s", size === "s");
    container.classList.toggle("mc-canvas--l", size === "l");
  };
  if (sizeSelect) {
    try {
      const saved = window.localStorage.getItem(SIZE_KEY);
      if (saved && ["s", "m", "l"].includes(saved)) sizeSelect.value = saved;
    } catch {
      /* bỏ qua khi không đọc được localStorage */
    }
    applySize(sizeSelect.value);
    sizeSelect.addEventListener("change", () => {
      applySize(sizeSelect.value);
      try {
        window.localStorage.setItem(SIZE_KEY, sizeSelect.value);
      } catch {
        /* bỏ qua khi không ghi được localStorage */
      }
    });
  }

  // ---- CK-7: phân tích tại nến (chuột phải / nhấn giữ) ----
  const analysis = document.getElementById("mc-bar-analysis");
  const analysisBody = analysis?.querySelector("[data-bar-analysis-body]");
  const STATUS_VI = { CANDIDATE: "Ra tín hiệu", ABSTAIN: "Không vào lệnh", VETO: "Bị chặn" };
  const renderAnalysis = (time, decision, message, analysisLink = null) => {
    if (!analysis || !analysisBody) return;
    analysis.hidden = false;
    setText("[data-bar-analysis-time]", fmtTime(time));
    const nodes = [];
    const para = (text, className) => {
      const p = document.createElement("p");
      if (className) p.className = className;
      p.textContent = text;
      return p;
    };
    if (message) {
      nodes.push(para(message));
    } else if (!decision) {
      nodes.push(para("Bot không có quyết định nào được đánh giá khi nến này đóng (hoặc dữ liệu không khớp hash)."));
    } else {
      nodes.push(
        para(
          `${STATUS_VI[decision.status] || decision.status} · ${decision.reason_code} — ${decision.detail_vi}`,
          "mc-bar-analysis__summary"
        )
      );
      const list = document.createElement("ul");
      list.className = "sc-checklist";
      for (const item of decision.checklist || []) {
        const li = document.createElement("li");
        const state = item.passed === true ? "pass" : item.passed === false ? "fail" : "na";
        li.className = `sc-check sc-check--${state}`;
        const mark = document.createElement("span");
        mark.className = "sc-check__mark";
        mark.setAttribute("aria-hidden", "true");
        mark.textContent = state === "pass" ? "✓" : state === "fail" ? "✗" : "—";
        const label = document.createElement("span");
        label.className = "sc-check__label";
        label.textContent = `${item.label_vi} (${state === "pass" ? "đạt" : state === "fail" ? "không đạt" : "chưa xác định"})`;
        const value = document.createElement("span");
        value.className = "sc-check__value";
        value.textContent = `${item.actual} · cần ${item.operator} ${item.threshold}`;
        li.append(mark, label, value);
        list.append(li);
      }
      if (list.children.length) nodes.push(list);
      else nodes.push(para("Quyết định này được tạo trước khi có checklist (SIG-3)."));
      for (const gate of decision.gate_details || []) nodes.push(para(`Cổng kiểm soát: ${gate}`));
    }
    if (analysisLink && analysisLink.url) {
      const links = document.createElement("p");
      links.className = "mc-bar-analysis__links";
      const makeLink = (href, text, kind) => {
        const a = document.createElement("a");
        a.href = href;
        a.textContent = text;
        if (kind) a.dataset.barAnalysisLink = kind;
        return a;
      };
      links.append(makeLink(analysisLink.url, analysisLink.label || "Phân tích bot", analysisLink.kind || "analysis"));
      if (analysisLink.kind === "microscope" && analysisLink.reports_url) {
        links.append(document.createTextNode(" · "), makeLink(analysisLink.reports_url, "Phân tích & Báo cáo bot", "reports"));
      }
      if (analysisLink.bot_url) {
        links.append(document.createTextNode(" · "), makeLink(analysisLink.bot_url, "Trang bot", "bot"));
      }
      nodes.push(links);
    }
    analysisBody.replaceChildren(...nodes);
  };
  analysis?.querySelector("[data-bar-analysis-close]")?.addEventListener("click", () => {
    analysis.hidden = true;
    container.focus();
  });
  const analyzeAt = async (clientX) => {
    const rect = container.getBoundingClientRect();
    const time = timeScale.coordinateToTime(clientX - rect.left);
    if (typeof time !== "number") return;
    const params = new URLSearchParams({
      bot_id: container.dataset.botId,
      symbol: container.dataset.symbol || "",
      timeframe: container.dataset.timeframe,
      time: String(time),
    });
    renderAnalysis(time, null, "Đang tải quyết định tại nến…");
    try {
      const response = await fetch(`/admin/api/chart/decision?${params.toString()}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        renderAnalysis(time, null, "Không đọc được quyết định tại nến này (thiếu quyền hoặc lỗi máy chủ).");
        return;
      }
      const body = await response.json();
      renderAnalysis(time, body.decision, null, body.analysis_link || null);
    } catch {
      renderAnalysis(time, null, "Lỗi mạng khi tải quyết định tại nến.");
    }
  };
  container.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    analyzeAt(event.clientX);
  });
  let pressTimer = null;
  container.addEventListener(
    "touchstart",
    (event) => {
      if (event.touches.length !== 1) return;
      const x = event.touches[0].clientX;
      pressTimer = window.setTimeout(() => analyzeAt(x), 600);
    },
    { passive: true }
  );
  for (const name of ["touchmove", "touchend", "touchcancel"]) {
    container.addEventListener(name, () => window.clearTimeout(pressTimer), { passive: true });
  }

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

  // CK-8: API nội bộ cho chart-drawings.js và lưới nhiều khung (cùng trang, không phải API công khai).
  const timeForLogical = (logical) => {
    const n = barTimes.length;
    if (n === 0) return null;
    if (logical <= 0) return Math.round(barTimes[0] + logical * intervalSeconds);
    if (logical >= n - 1) return Math.round(barTimes[n - 1] + (logical - (n - 1)) * intervalSeconds);
    const lo = Math.floor(logical);
    return Math.round(barTimes[lo] + (logical - lo) * (barTimes[lo + 1] - barTimes[lo]));
  };
  window.CrypidianChart = {
    LWC,
    chart,
    candles,
    container,
    palette,
    CanvasPrimitive,
    fmtNumber,
    fmtTime,
    symbol: container.dataset.symbol,
    timeframe: container.dataset.timeframe,
    botId: container.dataset.botId,
    xForTime: (time) => {
      const logical = logicalForTime(time);
      return logical === null ? null : chart.timeScale().logicalToCoordinate(logical);
    },
    timeForX: (x) => {
      const logical = chart.timeScale().coordinateToLogical(x);
      return logical === null ? null : timeForLogical(logical);
    },
    // Nến chứa thời điểm ``time`` (để nam châm bắt OHLC).
    barAt: (time) => {
      let found = null;
      for (const bar of payload.bars) {
        if (bar.time <= time) found = bar;
        else break;
      }
      return found;
    },
    bars: () => payload.bars,
    layerOn,
    visibleLogicalRange: () => timeScale.getVisibleLogicalRange(),
    positionLines: () => positionLineMeta.map((item) => ({ ...item })),
  };
  document.dispatchEvent(new CustomEvent("mc:chart-ready"));

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
