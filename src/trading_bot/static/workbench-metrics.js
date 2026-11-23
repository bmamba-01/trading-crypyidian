/**
 * Đọc /admin/api/workbench/metrics-alerts để hiển thị metric freshness/coverage/lag/latency và cảnh báo WP-07E.
 */
(function () {
  var panel = document.getElementById("workbench-metrics-panel");
  if (!panel) return;
  var refreshBtn = document.getElementById("workbench-metrics-refresh");

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function makeEl(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  function renderReport(data) {
    clearNode(panel);
    var summary = (data && data.summary) || {};
    var scanner = (data && data.scanner_lag) || {};
    var delivery = (data && data.delivery_latency) || {};
    var alerts = (data && data.alerts) || [];
    var bars = (data && data.bar_freshness) || [];

    var kpis = makeEl("div", "workbench-kpi-grid");
    var cardSpecs = [
      ["Cảnh báo đang mở", String(summary.alert_count || 0) + " (" + String(summary.critical_count || 0) + " nghiêm trọng)"],
      ["Chuỗi nến cũ / thiếu", String(summary.stale_bar_streams || 0) + " stale · " + String(summary.total_bar_gaps || 0) + " gap"],
      ["Độ trễ scanner", scanner.lag_seconds != null ? String(scanner.lag_seconds) + "s" : "Chưa có tick"],
      ["Latency gửi Telegram (p95)", delivery.p95_seconds != null ? String(delivery.p95_seconds) + "s" : "Chưa có mẫu"],
      ["Retry / Dead-letter", String(delivery.retry_count || 0) + " retry · " + String(delivery.dead_letter_count || 0) + " dead"],
      ["Luồng nến đang theo dõi", String(bars.length)]
    ];
    cardSpecs.forEach(function (spec) {
      var card = makeEl("div", "workbench-kpi-card");
      card.appendChild(makeEl("span", "workbench-kpi-label", spec[0]));
      card.appendChild(makeEl("span", "workbench-kpi-value", spec[1]));
      kpis.appendChild(card);
    });
    panel.appendChild(kpis);

    if (!alerts.length) {
      panel.appendChild(makeEl("p", "muted", "Chưa phát hiện cảnh báo chất lượng dữ liệu, quyền hay Telegram."));
      return;
    }

    var list = makeEl("ul", "workbench-alert-list");
    alerts.forEach(function (item) {
      var li = makeEl("li", "workbench-alert-item");
      var head = makeEl("div", "workbench-alert-head");
      var sev = item.severity === "critical" ? "critical" : (item.severity === "warning" ? "warning" : "info");
      head.appendChild(makeEl("span", "workbench-alert-badge workbench-alert-badge--" + sev, sev));
      head.appendChild(makeEl("strong", null, item.title_vi || item.code));
      li.appendChild(head);
      li.appendChild(makeEl("p", "workbench-alert-detail", item.detail_vi || ""));
      if (item.trace_ref) {
        li.appendChild(makeEl("p", "workbench-alert-trace", "Truy vết: " + item.trace_ref));
      }
      list.appendChild(li);
    });
    panel.appendChild(list);
  }

  function load() {
    panel.setAttribute("aria-busy", "true");
    fetch("/admin/api/workbench/metrics-alerts", { credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(renderReport)
      .catch(function () {
        clearNode(panel);
        panel.appendChild(makeEl("p", "muted", "Không tải được chỉ số WP-07E từ máy chủ."));
      })
      .finally(function () {
        panel.setAttribute("aria-busy", "false");
      });
  }

  if (refreshBtn) refreshBtn.addEventListener("click", load);
  load();
})();
