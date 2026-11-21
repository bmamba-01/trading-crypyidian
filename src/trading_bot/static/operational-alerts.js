/* CRY-274: bảng cảnh báo vận hành trên dashboard (tab Điều khiển).
 * Đọc /api/v1/incidents/operational-alerts — kể cả PENDING/DEAD khi Telegram gián đoạn.
 * Chỉ đọc, không mutation; tự làm mới 15 giây khi tab hiển thị. */
(function () {
  "use strict";

  var panel = document.getElementById("operational-alerts-panel");
  if (!panel) return;
  var refreshBtn = document.getElementById("operational-alerts-refresh");
  var includeDelivered = document.getElementById("operational-alerts-include-delivered");
  var TIMER_MS = 15000;
  var timer = null;

  var STATUS_LABELS = {
    PENDING: "Chờ gửi",
    PROCESSING: "Đang gửi",
    DEAD: "Dead-letter · Telegram lỗi",
    DELIVERED: "Đã gửi",
  };

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString("vi-VN", { hour12: false });
  }

  function render(alerts) {
    if (!alerts.length) {
      panel.innerHTML =
        '<p class="muted">Chưa có cảnh báo vận hành nào trong không gian làm việc này.</p>';
      return;
    }
    var rows = alerts
      .map(function (a) {
        var status = STATUS_LABELS[a.status] || a.status;
        var statusClass = "ops-alert-status ops-alert-status--" + String(a.status).toLowerCase();
        var err = a.last_error_code
          ? '<span class="ops-alert-error" title="Lỗi gửi gần nhất">' +
            escapeHtml(a.last_error_code) +
            "</span>"
          : "";
        var link = a.admin_link
          ? '<a href="' + escapeHtml(a.admin_link) + '">Mở Admin</a>'
          : "—";
        return (
          "<tr>" +
          '<td data-label="Thời gian"><time datetime="' + escapeHtml(a.created_at) + '">' +
          escapeHtml(formatTime(a.created_at)) + "</time></td>" +
          '<td data-label="Sự kiện"><strong>' + escapeHtml(a.title_vi) + "</strong>" +
          '<span class="ops-alert-mode">' + escapeHtml(a.mode) + "</span></td>" +
          '<td data-label="Lý do">' + escapeHtml(a.reason) + "</td>" +
          '<td data-label="Trạng thái"><span class="' + statusClass + '">' +
          escapeHtml(status) + "</span>" + err + "</td>" +
          '<td data-label="Lần thử">' + escapeHtml(a.attempts) + "</td>" +
          '<td data-label="Liên kết">' + link + "</td>" +
          "</tr>"
        );
      })
      .join("");
    panel.innerHTML =
      '<div class="table-responsive ops-alert-table-wrap">' +
      '<table class="table ops-alert-table">' +
      "<thead><tr>" +
      "<th scope=\"col\">Thời gian</th>" +
      "<th scope=\"col\">Sự kiện</th>" +
      "<th scope=\"col\">Lý do</th>" +
      "<th scope=\"col\">Trạng thái</th>" +
      "<th scope=\"col\">Lần thử</th>" +
      "<th scope=\"col\">Liên kết</th>" +
      "</tr></thead><tbody>" + rows + "</tbody></table></div>";
  }

  function load() {
    var include = includeDelivered && includeDelivered.checked ? "true" : "false";
    panel.setAttribute("aria-busy", "true");
    fetch("/api/v1/incidents/operational-alerts?include_delivered=" + include + "&limit=50", {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        render(Array.isArray(data.alerts) ? data.alerts : []);
      })
      .catch(function () {
        panel.innerHTML =
          '<p class="muted" role="alert">Không tải được cảnh báo vận hành; thử lại sau ít phút.</p>';
      })
      .finally(function () {
        panel.setAttribute("aria-busy", "false");
      });
  }

  function startTimer() {
    if (timer) return;
    timer = setInterval(function () {
      if (document.visibilityState === "visible") load();
    }, TIMER_MS);
  }

  function stopTimer() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  if (refreshBtn) refreshBtn.addEventListener("click", load);
  if (includeDelivered) includeDelivered.addEventListener("change", load);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      load();
      startTimer();
    } else {
      stopTimer();
    }
  });

  load();
  startTimer();
})();
