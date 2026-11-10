"use strict";

(() => {
  const chartLibrary = window.LightweightCharts;

  const readData = (id) => {
    const element = document.getElementById(id);
    return element ? JSON.parse(element.textContent || "{}") : null;
  };

  const showFallback = (container, message) => {
    if (!container) return;
    container.hidden = true;
    const notice = document.createElement("p");
    notice.setAttribute("role", "status");
    notice.textContent = message;
    container.insertAdjacentElement("afterend", notice);
  };

  const marketColors = () => {
    const style = getComputedStyle(document.documentElement);
    return {
      up: style.getPropertyValue("--market-up").trim() || "#0ECB81",
      down: style.getPropertyValue("--market-down").trim() || "#F6465D",
      flat: style.getPropertyValue("--market-flat").trim() || "#8B98A5",
      upOutline: style.getPropertyValue("--pnl-positive-text").trim() || "#087A55",
      downOutline: style.getPropertyValue("--pnl-negative-text").trim() || "#B4233A",
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

  const createChart = (container, height) =>
    chartLibrary.createChart(container, {
      autoSize: true,
      height,
      layout: {
        background: { color: "#ffffff" },
        textColor: "#172b3a",
        fontFamily: '"Be Vietnam Pro", system-ui, sans-serif',
      },
      grid: {
        vertLines: { color: "#e1e8ee" },
        horzLines: { color: "#e1e8ee" },
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
        borderColor: "#aebdca",
      },
      rightPriceScale: { borderColor: "#aebdca" },
      crosshair: { mode: chartLibrary.CrosshairMode?.Normal ?? 0 },
    });

  const chartForTrade = () => {
    const container = document.getElementById("trade-chart");
    if (!container) return;
    const payload = readData("trade-chart-data");
    if (!chartLibrary || !payload || !Array.isArray(payload.bars) || payload.bars.length === 0) {
      showFallback(container, "Biểu đồ chưa tải được; chi tiết giao dịch ở bảng bên dưới vẫn dùng được.");
      return;
    }

    try {
      const bars = payload.bars;
      const colors = marketColors();
      const chart = createChart(container, 360);
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
      const exitCode = trade.exit_reason === "STOP_LOSS" ? "SL" :
        trade.exit_reason === "TAKE_PROFIT" ? "TP" :
          trade.exit_reason === "TIME_STOP" ? "TIME" :
            trade.exit_reason === "OPPOSING_SIGNAL" ? "ĐẢO" : "ĐÓNG";
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
          ? `Vào ${isLong ? "LONG" : "SHORT"}`
          : partialExit
            ? "Giảm vị thế"
            : finalExit
              ? `${exitCode} ${trade.net_pnl} ${trade.quote_asset}`
              : "Đóng (chưa rõ)";
        const entryReason = trade.entry_reason || {};
        const explanation = entry
          ? `Vào lệnh ${trade.symbol} lúc ${fill.occurred_at}; chiến thuật ${entryReason.strategy_id || "Chưa có dữ liệu"}; vì sao bắt tín hiệu: ${entryReason.detail_vi || entryReason.reason_code || "Chưa có dữ liệu"}.`
          : `Khớp đóng lệnh ${trade.symbol} lúc ${fill.occurred_at}; trạng thái ${fill.position_event_type || "chưa rõ"}; lý do đóng ${trade.exit_reason || "Chưa có dữ liệu"}; PnL ròng ${trade.net_pnl} ${trade.quote_asset}.`;
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
      const eventDetail = document.getElementById("trade-chart-event-detail");
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
      const colors = ["#07549a", "#126b45", "#84315b", "#9a4e00", "#5546a5"];
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

  document.addEventListener("DOMContentLoaded", () => {
    chartForTrade();
    dashboardChart();
  });
})();
