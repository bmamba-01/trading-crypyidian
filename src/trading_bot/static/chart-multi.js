"use strict";

// CK-8 — Lưới nhiều khung cùng mã trên /admin/chart. Mỗi khung phụ đọc /admin/api/chart (nến đã
// lưu, chỉ đọc), đồng bộ con trỏ và vùng thời gian theo chart chính. Chỉ nến + EMA để nhẹ.
(() => {
  const api = window.CrypidianChart;
  const select = document.querySelector("[data-multi-count]");
  const grid = document.querySelector("[data-multi-grid]");
  if (!api || !select || !grid) return;

  const STORAGE_KEY = "crypidian.marketChart.multi";
  const INTERVALS = { "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400 };
  const REFRESH_MS = 30000;
  const available = (grid.dataset.timeframes || "").split(",").filter((tf) => tf && tf !== api.timeframe);
  let minis = [];
  let refreshTimer = null;

  // Ưu tiên khung lớn hơn khung đang xem (bối cảnh), sau đó khung nhỏ hơn.
  const pickFrames = (count) => {
    const current = INTERVALS[api.timeframe] || 0;
    const higher = available.filter((tf) => INTERVALS[tf] > current).sort((a, b) => INTERVALS[a] - INTERVALS[b]);
    const lower = available.filter((tf) => INTERVALS[tf] < current).sort((a, b) => INTERVALS[b] - INTERVALS[a]);
    return [...higher, ...lower].slice(0, count - 1);
  };

  const destroy = () => {
    for (const mini of minis) mini.chart.remove();
    minis = [];
    grid.replaceChildren();
    window.clearInterval(refreshTimer);
  };

  const load = async (mini) => {
    const params = new URLSearchParams({
      bot_id: api.botId,
      symbol: api.symbol,
      timeframe: mini.timeframe,
      limit: "300",
    });
    try {
      const response = await fetch(`/admin/api/chart?${params.toString()}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        mini.status.textContent = "không đọc được dữ liệu";
        return;
      }
      const body = await response.json();
      if (body.status !== "ok") return;
      mini.bars = body.bars;
      mini.candles.setData(body.bars);
      mini.ema20.setData(body.lines.ema_20 || []);
      mini.ema50.setData(body.lines.ema_50 || []);
      mini.status.textContent = body.bars.length ? `${body.bars.length} nến` : "chưa có nến";
      mini.cell.dataset.bars = String(body.bars.length);
      syncRange();
    } catch {
      mini.status.textContent = "lỗi mạng";
    }
  };

  const build = (count) => {
    destroy();
    const frames = pickFrames(count);
    grid.hidden = frames.length === 0;
    const colors = api.palette();
    for (const timeframe of frames) {
      const cell = document.createElement("section");
      cell.className = "mc-multi-cell";
      cell.dataset.timeframe = timeframe;
      const title = document.createElement("h3");
      title.className = "mc-multi-cell__title";
      const status = document.createElement("span");
      status.className = "sc-meta";
      status.textContent = "đang tải…";
      title.append(`${api.symbol} · ${timeframe.toUpperCase()} `, status);
      const holder = document.createElement("div");
      holder.className = "mc-multi-cell__chart";
      holder.setAttribute("role", "img");
      holder.setAttribute("aria-label", `Biểu đồ nến ${api.symbol} khung ${timeframe}`);
      cell.append(title, holder);
      grid.append(cell);
      const chart = api.LWC.createChart(holder, {
        autoSize: true,
        layout: {
          attributionLogo: false,
          background: { color: colors.background },
          textColor: colors.text,
          fontFamily: '"Be Vietnam Pro", system-ui, sans-serif',
        },
        grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
        localization: { locale: "vi-VN", priceFormatter: (v) => api.fmtNumber(v), timeFormatter: api.fmtTime },
        timeScale: { timeVisible: true, secondsVisible: false, borderColor: colors.border },
        rightPriceScale: { borderColor: colors.border },
      });
      const candles = chart.addSeries(api.LWC.CandlestickSeries, {
        upColor: colors.up,
        downColor: colors.down,
        borderUpColor: colors.up,
        borderDownColor: colors.down,
        wickUpColor: colors.up,
        wickDownColor: colors.down,
      });
      const line = (color) =>
        chart.addSeries(api.LWC.LineSeries, {
          color,
          lineWidth: 1,
          lastValueVisible: false,
          priceLineVisible: false,
          crosshairMarkerVisible: false,
        });
      const mini = { timeframe, cell, status, chart, candles, ema20: line(colors.ema20), ema50: line(colors.ema50), bars: [] };
      minis.push(mini);
      load(mini);
    }
    refreshTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") for (const mini of minis) load(mini);
    }, REFRESH_MS);
  };

  // ---- Đồng bộ con trỏ và vùng thời gian từ chart chính ----
  const syncRange = () => {
    const range = api.chart.timeScale().getVisibleRange();
    if (!range) return;
    for (const mini of minis) {
      if (!mini.bars.length) continue;
      try {
        mini.chart.timeScale().setVisibleRange({ from: range.from, to: range.to });
      } catch {
        /* vùng ngoài dữ liệu khung phụ: giữ vùng hiện tại */
      }
    }
  };
  api.chart.timeScale().subscribeVisibleTimeRangeChange(syncRange);
  api.chart.subscribeCrosshairMove((param) => {
    for (const mini of minis) {
      if (typeof param.time !== "number" || !mini.bars.length) {
        mini.chart.clearCrosshairPosition();
        continue;
      }
      const interval = INTERVALS[mini.timeframe];
      const barTime = Math.floor(param.time / interval) * interval;
      const bar = mini.bars.find((item) => item.time === barTime);
      if (bar) mini.chart.setCrosshairPosition(bar.close, bar.time, mini.candles);
      else mini.chart.clearCrosshairPosition();
    }
  });

  select.addEventListener("change", () => {
    build(Number(select.value));
    try {
      window.localStorage.setItem(STORAGE_KEY, select.value);
    } catch {
      /* bỏ qua khi không ghi được localStorage */
    }
  });
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved && ["1", "2", "4"].includes(saved)) select.value = saved;
  } catch {
    /* localStorage có thể bị chặn */
  }
  if (select.value !== "1") build(Number(select.value));
})();
