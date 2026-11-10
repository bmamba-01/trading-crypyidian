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
        priceFormatter: (value) =>
          Number(value).toLocaleString("vi-VN", { maximumFractionDigits: 8 }),
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6,
        borderColor: theme.borderColor,
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
                : "ĐÓNG";

      // Vẽ đường Stop Loss (SL) và Take Profit (TP) nếu có dữ liệu định lượng
      const pm = trade.post_mortem;
      if (pm && pm.entry_analysis) {
        const slPrice = Number(pm.entry_analysis.stop_loss_price);
        const tpPrice = Number(pm.entry_analysis.take_profit_price);
        if (slPrice > 0) {
          candleSeries.createPriceLine({
            price: slPrice,
            color: colors.down,
            lineWidth: 1,
            lineStyle: chartLibrary.LineStyle?.Dashed ?? 2,
            axisLabelVisible: true,
            title: "🛑 Dừng lỗ (SL)",
          });
        }
        if (tpPrice > 0) {
          candleSeries.createPriceLine({
            price: tpPrice,
            color: colors.up,
            lineWidth: 1,
            lineStyle: chartLibrary.LineStyle?.Dashed ?? 2,
            axisLabelVisible: true,
            title: "🎯 Chốt lời (TP)",
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
          ? `Vào lệnh ${trade.symbol} lúc ${fill.occurred_at}; Giá: ${fill.price}; Chiến thuật: ${entryReason.strategy_id || "TREND_V1"}; Tín hiệu: ${entryReason.detail_vi || "Mô hình nến xác nhận"}.`
          : `Khớp đóng lệnh ${trade.symbol} lúc ${fill.occurred_at}; Giá: ${fill.price}; Lý do đóng: ${trade.exit_reason || "Chưa rõ"}; PnL ròng: ${trade.net_pnl} ${trade.quote_asset}.`;
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
      if (eventDetail) {
        chart.subscribeCrosshairMove((parameter) => {
          if (parameter.time === undefined || parameter.time === null) return;
          const details = markerDetails.get(Number(parameter.time));
          if (details?.length) eventDetail.textContent = details.join(" ");
        });
      }
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
      payload.series.forEach((item, index) => {
        const color = colors[index % colors.length];
        const line = chart.addSeries(chartLibrary.LineSeries, {
          color,
          lineWidth: 2,
          title: item.name,
          priceLineVisible: false,
        });
        line.setData(item.data);
        chartLibrary.createSeriesMarkers(line, item.markers || []);
        for (const marker of item.markers || []) {
          if (!marker.title) continue;
          const details = markerDetails.get(Number(marker.time)) || [];
          details.push(marker.title);
          markerDetails.set(Number(marker.time), details);
        }
      });
      const eventDetail = document.getElementById("dashboard-chart-event-detail");
      if (eventDetail) {
        chart.subscribeCrosshairMove((parameter) => {
          if (parameter.time === undefined || parameter.time === null) return;
          const details = markerDetails.get(Number(parameter.time));
          if (details?.length) eventDetail.textContent = details.join(" ");
        });
      }
      chart.timeScale().fitContent();
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
