"use strict";

(() => {
  const chartLibrary = window.LightweightCharts;
  let activeTradeChart = null;
  let activeDashboardChart = null;

  const readData = (id) => {
    const element = document.getElementById(id);
    return element ? JSON.parse(element.textContent || "{}") : null;
  };

  const showFallback = (container, message) => {
    if (!container) return;
    container.hidden = true;
    const notice = document.createElement("p");
    notice.setAttribute("role", "status");
    notice.className = "chart-fallback-notice";
    notice.textContent = message;
    container.insertAdjacentElement("afterend", notice);
  };

  const getThemeColors = () => {
    const isDark =
      document.documentElement.getAttribute("data-theme") === "dark" ||
      (!document.documentElement.getAttribute("data-theme") &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);
    return {
      isDark,
      background: isDark ? "#151b26" : "#ffffff",
      textColor: isDark ? "#94a3b8" : "#475569",
      gridColor: isDark ? "#1e2638" : "#e2e8f0",
      borderColor: isDark ? "#242f42" : "#cbd5e1",
    };
  };

  const marketColors = () => {
    const style = getComputedStyle(document.documentElement);
    return {
      up: style.getPropertyValue("--market-up").trim() || "#0ECB81",
      down: style.getPropertyValue("--market-down").trim() || "#F6465D",
      flat: style.getPropertyValue("--market-flat").trim() || "#8B98A5",
      upOutline: style.getPropertyValue("--pnl-positive-text").trim() || "#0ECB81",
      downOutline: style.getPropertyValue("--pnl-negative-text").trim() || "#F6465D",
    };
  };

  // Mọi thời điểm trên trục, nhãn con trỏ và chú giải đều theo múi giờ vận hành của dự án.
  const DISPLAY_TIME_ZONE = "Asia/Ho_Chi_Minh";
  const timeParts = (options) =>
    new Intl.DateTimeFormat("vi-VN", { timeZone: DISPLAY_TIME_ZONE, hourCycle: "h23", ...options });
  const dateTimeFormat = timeParts({
    hour: "2-digit",
    minute: "2-digit",
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
  const tickFormats = [
    timeParts({ year: "numeric" }),
    timeParts({ month: "2-digit", year: "numeric" }),
    timeParts({ day: "2-digit", month: "2-digit" }),
    timeParts({ hour: "2-digit", minute: "2-digit" }),
    timeParts({ hour: "2-digit", minute: "2-digit", second: "2-digit" }),
  ];
  const isTimestamp = (time) => typeof time === "number" && Number.isFinite(time);
  const formatDateTime = (time) =>
    isTimestamp(time) ? dateTimeFormat.format(new Date(time * 1000)) : String(time);
  const formatIsoDateTime = (text) => {
    const milliseconds = Date.parse(text);
    return Number.isFinite(milliseconds) ? formatDateTime(milliseconds / 1000) : String(text);
  };
  const formatNumber = (value) =>
    Number(value).toLocaleString("vi-VN", { maximumFractionDigits: 8 });
  const formatSigned = (value, minimumFractionDigits, maximumFractionDigits) => {
    const text = Number(value).toLocaleString("vi-VN", {
      minimumFractionDigits,
      maximumFractionDigits,
    });
    return value > 0 ? `+${text}` : text;
  };
  const toneOf = (value) => (value > 0 ? "up" : value < 0 ? "down" : "");

  // Chú giải theo con trỏ: dựng bằng DOM (textContent), không chèn HTML từ dữ liệu.
  const legendItem = (label, value, tone) => {
    const item = document.createElement("span");
    item.className = "chart-ohlc-legend__item";
    if (label) item.append(`${label} `);
    const strong = document.createElement("span");
    strong.className = tone
      ? `chart-ohlc-legend__value chart-ohlc-legend__value--${tone}`
      : "chart-ohlc-legend__value";
    strong.textContent = value;
    item.append(strong);
    return item;
  };
  const renderLegend = (legend, items) => {
    if (!legend || items.length === 0) return;
    legend.replaceChildren(...items);
    legend.dataset.legendSource = "crosshair";
  };

  const nearestBarTime = (bars, timestamp) => {
    let selected = null;
    let distance = Number.POSITIVE_INFINITY;
    for (const bar of bars) {
      const nextDistance = Math.abs(bar.time - timestamp);
      if (nextDistance < distance) {
        selected = bar.time;
        distance = nextDistance;
      }
    }
    return selected;
  };

  const createChart = (container, height) => {
    const theme = getThemeColors();
    return chartLibrary.createChart(container, {
      autoSize: true,
      height,
      layout: {
        // Logo ghi công của thư viện tự chèn thẻ <style>, bị CSP chặn. Trang đã có dòng ghi
        // công kèm liên kết tradingview.com nên được phép tắt logo (tài liệu LayoutOptions).
        attributionLogo: false,
        background: { color: theme.background },
        textColor: theme.textColor,
        fontFamily: '"Be Vietnam Pro", system-ui, sans-serif',
      },
      grid: {
        vertLines: { color: theme.gridColor },
        horzLines: { color: theme.gridColor },
      },
      localization: {
        locale: "vi-VN",
        dateFormat: "dd/MM/yyyy",
        priceFormatter: formatNumber,
        timeFormatter: formatDateTime,
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6,
        borderColor: theme.borderColor,
        // Trả null với kiểu thời gian không phải timestamp để thư viện dùng định dạng mặc định.
        tickMarkFormatter: (time, tickMarkType) =>
          isTimestamp(time) && tickFormats[tickMarkType]
            ? tickFormats[tickMarkType].format(new Date(time * 1000))
            : null,
      },
      rightPriceScale: { borderColor: theme.borderColor },
      crosshair: { mode: chartLibrary.CrosshairMode?.Normal ?? 0 },
    });
  };

  const applyThemeUpdate = (chart) => {
    if (!chart) return;
    const theme = getThemeColors();
    chart.applyOptions({
      layout: {
        background: { color: theme.background },
        textColor: theme.textColor,
      },
      grid: {
        vertLines: { color: theme.gridColor },
        horzLines: { color: theme.gridColor },
      },
      timeScale: { borderColor: theme.borderColor },
      rightPriceScale: { borderColor: theme.borderColor },
    });
  };

  const chartForTrade = () => {
    const container =
      document.getElementById("trade-chart") || document.getElementById("bot-trade-chart");
    if (!container) return;
    const payload = readData("trade-chart-data") || readData("bot-chart-data");
    if (!chartLibrary || !payload || !Array.isArray(payload.bars) || payload.bars.length === 0) {
      showFallback(
        container,
        "Biểu đồ chưa tải được; bảng chi tiết giao dịch bên dưới vẫn đầy đủ dữ liệu."
      );
      return;
    }

    try {
      const bars = payload.bars;
      const colors = marketColors();
      const chart = createChart(container, 380);
      activeTradeChart = chart;

      const candleSeries = chart.addSeries(chartLibrary.CandlestickSeries, {
        upColor: colors.up,
        downColor: colors.down,
        borderUpColor: colors.upOutline,
        borderDownColor: colors.downOutline,
        wickUpColor: colors.upOutline,
        wickDownColor: colors.downOutline,
      });
      candleSeries.setData(bars);

      const trade = payload.trade;
      const isLong = trade.side === "LONG";
      const net = Number(trade.net_pnl);
      const exitCode =
        trade.exit_reason === "STOP_LOSS"
          ? "SL"
          : trade.exit_reason === "TAKE_PROFIT"
            ? "TP"
            : trade.exit_reason === "TIME_STOP"
              ? "TIME"
              : trade.exit_reason === "OPPOSING_SIGNAL"
                ? "ĐẢO"
                : trade.exit_reason === "CHANNEL_EXIT"
                  ? "KÊNH"
                  : trade.exit_reason === "FEATURE_EXIT"
                    ? "CHỈ BÁO"
                    : "ĐÓNG";

      // Vẽ đường giá vào, dừng lỗ (SL) và chốt lời (TP) khi hậu kiểm có số liệu; tên đường
      // lấy từ từ điển chú giải phía máy chủ (thuộc tính data-* của vùng biểu đồ).
      const pm = trade.post_mortem;
      if (pm && pm.entry_analysis) {
        const entryPrice = Number(pm.entry_analysis.entry_price);
        const slPrice = Number(pm.entry_analysis.stop_loss_price);
        const tpPrice = Number(pm.entry_analysis.take_profit_price);
        if (entryPrice > 0) {
          candleSeries.createPriceLine({
            price: entryPrice,
            color: "#3861fb",
            lineWidth: 1,
            lineStyle: chartLibrary.LineStyle?.Dotted ?? 1,
            axisLabelVisible: true,
            title: container.dataset.entryTitle || "Đường giá vào bình quân",
          });
        }
        if (slPrice > 0) {
          candleSeries.createPriceLine({
            price: slPrice,
            color: colors.down,
            lineWidth: 1,
            lineStyle: chartLibrary.LineStyle?.Dashed ?? 2,
            axisLabelVisible: true,
            title: container.dataset.stopTitle || "Đường dừng lỗ (SL)",
          });
        }
        if (tpPrice > 0) {
          candleSeries.createPriceLine({
            price: tpPrice,
            color: colors.up,
            lineWidth: 1,
            lineStyle: chartLibrary.LineStyle?.Dashed ?? 2,
            axisLabelVisible: true,
            title: container.dataset.targetTitle || "Đường chốt lời (TP)",
          });
        }
      }

      const markers = [];
      const markerDetails = new Map();
      for (const fill of trade.fills || []) {
        const occurred = Date.parse(fill.occurred_at) / 1000;
        const time = nearestBarTime(bars, occurred);
        if (time === null) continue;
        const entry = fill.role === "ENTRY";
        const partialExit = !entry && fill.position_event_type === "REDUCED";
        const finalExit = !entry && fill.position_event_type === "CLOSED";
        const upward = entry ? isLong : !isLong;
        const pnlColor = net > 0 ? colors.up : net < 0 ? colors.down : colors.flat;
        const text = entry
          ? `🟢 Vào ${isLong ? "LONG" : "SHORT"} @ ${fill.price}`
          : partialExit
            ? "Giảm vị thế"
            : finalExit
              ? `${exitCode} @ ${fill.price} (${trade.net_pnl} ${trade.quote_asset})`
              : "Đóng (chưa rõ)";
        const entryReason = trade.entry_reason || {};
        const explanation = entry
          ? `Vào lệnh ${trade.symbol} lúc ${formatIsoDateTime(fill.occurred_at)}; Giá: ${fill.price}; Chiến thuật: ${entryReason.strategy_id || "chưa có dữ liệu"}; Tín hiệu: ${entryReason.detail_vi || "ledger chưa lưu lý do vào lệnh"}.`
          : `Khớp đóng lệnh ${trade.symbol} lúc ${formatIsoDateTime(fill.occurred_at)}; Giá: ${fill.price}; Lý do đóng: ${trade.exit_reason || "Chưa rõ"}; PnL ròng: ${trade.net_pnl} ${trade.quote_asset}.`;
        markers.push({
          time,
          position: upward ? "belowBar" : "aboveBar",
          shape: upward ? "arrowUp" : "arrowDown",
          color: entry ? (isLong ? colors.up : colors.down) : partialExit ? "#07549a" : pnlColor,
          text,
          title: explanation,
          size: 2,
        });
        const details = markerDetails.get(time) || [];
        details.push(explanation);
        markerDetails.set(time, details);
      }
      markers.sort((left, right) => left.time - right.time);
      chartLibrary.createSeriesMarkers(candleSeries, markers);

      const eventDetail =
        document.getElementById("trade-chart-event-detail") ||
        document.getElementById("bot-chart-event-detail");
      const legend =
        document.getElementById("trade-chart-legend") ||
        document.getElementById("bot-chart-legend");
      const barsByTime = new Map(bars.map((bar) => [Number(bar.time), bar]));
      const showBarLegend = (time) => {
        const bar = barsByTime.get(Number(time));
        if (!bar) return;
        const open = Number(bar.open);
        const close = Number(bar.close);
        const items = [
          legendItem("", `${formatDateTime(Number(bar.time))} (${DISPLAY_TIME_ZONE})`),
          legendItem("Mở", formatNumber(open)),
          legendItem("Cao", formatNumber(bar.high)),
          legendItem("Thấp", formatNumber(bar.low)),
          legendItem("Đóng", formatNumber(close), toneOf(close - open)),
        ];
        if (open > 0) {
          const change = ((close - open) / open) * 100;
          items.push(
            legendItem("Thay đổi so với giá mở", `${formatSigned(change, 2, 2)}%`, toneOf(change))
          );
        }
        if (bar.volume !== undefined && bar.volume !== null && Number.isFinite(Number(bar.volume))) {
          items.push(legendItem("Khối lượng", formatNumber(bar.volume)));
        }
        renderLegend(legend, items);
      };
      chart.subscribeCrosshairMove((parameter) => {
        if (parameter.time === undefined || parameter.time === null) return;
        showBarLegend(parameter.time);
        if (!eventDetail) return;
        const details = markerDetails.get(Number(parameter.time));
        if (details?.length) eventDetail.textContent = details.join(" ");
      });
      chart.timeScale().fitContent();
      container.dataset.chartReady = "true";
    } catch (_error) {
      showFallback(container, "Không thể hiển thị biểu đồ lúc này; bảng khớp lệnh vẫn có đầy đủ dữ liệu.");
    }
  };

  const dashboardChart = () => {
    const container = document.getElementById("dashboard-performance-chart");
    if (!container) return;
    const payload = readData("dashboard-chart-data");
    if (!chartLibrary || !payload || !Array.isArray(payload.series) || payload.series.length === 0) {
      showFallback(container, "Biểu đồ hiệu suất chưa tải được.");
      return;
    }

    try {
      const chart = createChart(container, 300);
      activeDashboardChart = chart;
      const colors = ["#3861fb", "#0ecb81", "#f59e0b", "#a855f7", "#ec4899"];
      const markerDetails = new Map();
      const lines = [];
      payload.series.forEach((item, index) => {
        const color = colors[index % colors.length];
        const line = chart.addSeries(chartLibrary.LineSeries, {
          color,
          lineWidth: 2,
          title: item.name,
          priceLineVisible: false,
        });
        line.setData(item.data);
        lines.push({ name: item.name, series: line });
        chartLibrary.createSeriesMarkers(line, item.markers || []);
        for (const marker of item.markers || []) {
          if (!marker.title) continue;
          const details = markerDetails.get(Number(marker.time)) || [];
          details.push(marker.title);
          markerDetails.set(Number(marker.time), details);
        }
      });
      const eventDetail = document.getElementById("dashboard-chart-event-detail");
      const legend = document.getElementById("dashboard-chart-legend");
      const quoteAsset = payload.quote_asset || "";
      chart.subscribeCrosshairMove((parameter) => {
        if (parameter.time === undefined || parameter.time === null) return;
        const items = [];
        for (const { name, series } of lines) {
          const point = parameter.seriesData?.get(series);
          if (!point || !Number.isFinite(Number(point.value))) continue;
          const value = Number(point.value);
          items.push(
            legendItem(`${name}: PnL ròng tích lũy`, `${formatSigned(value, 0, 8)} ${quoteAsset}`, toneOf(value))
          );
        }
        if (items.length) {
          items.unshift(legendItem("", `${formatDateTime(Number(parameter.time))} (${DISPLAY_TIME_ZONE})`));
          renderLegend(legend, items);
        }
        if (!eventDetail) return;
        const details = markerDetails.get(Number(parameter.time));
        if (details?.length) eventDetail.textContent = details.join(" ");
      });
      chart.timeScale().fitContent();
      // Biểu đồ nằm trong tab có thể được dựng khi panel đang ẩn (rộng 0): khi tab hiện ra,
      // autoSize đổi kích thước nhưng thang thời gian vẫn co về mép phải, nên căn lại.
      const panel = container.closest('[role="tabpanel"]');
      if (panel) {
        panel.addEventListener("tabs:shown", () => {
          window.requestAnimationFrame(() => chart.timeScale().fitContent());
        });
      }
      container.dataset.chartReady = "true";
    } catch (_error) {
      showFallback(container, "Không thể hiển thị biểu đồ hiệu suất lúc này.");
    }
  };

  // Đồng bộ theme khi người dùng nhấn nút gạt Light / Dark mode
  window.addEventListener("themechange", () => {
    if (activeTradeChart) applyThemeUpdate(activeTradeChart);
    if (activeDashboardChart) applyThemeUpdate(activeDashboardChart);
  });

  document.addEventListener("DOMContentLoaded", () => {
    chartForTrade();
    dashboardChart();
  });
})();
