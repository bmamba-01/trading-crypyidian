/**
 * Bảng trạng thái đăng ký, lịch sử delivery và gửi test message Telegram (CRY-347).
 */
(function () {
  var section = document.querySelector(".tg-delivery-section");
  var panel = document.getElementById("tg-delivery-panel");
  if (!section || !panel) return;

  var botFilter = document.getElementById("tg-delivery-bot-filter");
  var refreshBtn = document.getElementById("tg-delivery-refresh");
  var testBtn = document.getElementById("tg-test-message-btn");
  var testStatus = document.getElementById("tg-test-message-status");
  var csrfToken = section.getAttribute("data-csrf-token") || "";

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function makeEl(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  function populateBots(bots) {
    if (!botFilter || !Array.isArray(bots)) return;
    var current = botFilter.value;
    clearNode(botFilter);
    botFilter.appendChild(makeEl("option", null, "Tất cả bot"));
    botFilter.firstChild.value = "";
    bots.forEach(function (b) {
      var opt = makeEl("option", null, b.bot_name || b.bot_id);
      opt.value = b.bot_id;
      botFilter.appendChild(opt);
    });
    botFilter.value = current;
  }

  function renderDashboard(data) {
    clearNode(panel);
    var summary = (data && data.summary) || {};
    var deliveries = (data && data.deliveries) || [];
    if (data && data.bots) populateBots(data.bots);

    var kpis = makeEl("div", "tg-kpi-grid");
    var cards = [
      ["Đã gửi (SENT)", String(summary.sent_count || 0)],
      ["Đang chờ (PENDING)", String(summary.pending_count || 0)],
      ["Lỗi hẳn (DEAD)", String(summary.dead_count || 0)],
      [
        "Lọc / Hết hạn / Dedupe",
        String(summary.filtered_count || 0) +
          " / " +
          String(summary.expired_count || 0) +
          " / " +
          String(summary.suppressed_count || 0),
      ],
      ["Lần gửi cuối", summary.last_sent_at || "Chưa có"],
      ["Lỗi gần nhất", summary.last_error_code || "Không có"],
    ];
    cards.forEach(function (item) {
      var card = makeEl("div", "tg-kpi-card");
      card.appendChild(makeEl("span", "tg-kpi-label", item[0]));
      card.appendChild(makeEl("span", "tg-kpi-value", item[1]));
      kpis.appendChild(card);
    });
    panel.appendChild(kpis);

    if (!deliveries.length) {
      panel.appendChild(makeEl("p", "ops-hint", "Chưa có lượt gửi tín hiệu nào được ghi nhận."));
      return;
    }

    var wrap = makeEl("div", "tg-delivery-table-wrap");
    var table = makeEl("table", "tg-delivery-table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    ["Bot", "Trạng thái", "Số lần thử", "Mã lỗi", "Thời điểm"].forEach(function (h) {
      headRow.appendChild(makeEl("th", null, h));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    deliveries.forEach(function (row) {
      var tr = document.createElement("tr");
      var tdBot = makeEl("td", null, row.bot_name || row.bot_id);
      tdBot.setAttribute("data-label", "Bot");
      var tdStatus = makeEl("td", null, (row.status_vi || row.status) + " (" + row.status + ")");
      tdStatus.setAttribute("data-label", "Trạng thái");
      var tdAttempts = makeEl("td", null, String(row.attempts || 0));
      tdAttempts.setAttribute("data-label", "Số lần thử");
      var tdError = makeEl("td", null, row.last_error_code || "—");
      tdError.setAttribute("data-label", "Mã lỗi");
      var tdTime = makeEl("td", null, row.sent_at || row.created_at || "—");
      tdTime.setAttribute("data-label", "Thời điểm");
      tr.appendChild(tdBot);
      tr.appendChild(tdStatus);
      tr.appendChild(tdAttempts);
      tr.appendChild(tdError);
      tr.appendChild(tdTime);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    panel.appendChild(wrap);
  }

  function loadDeliveries() {
    panel.setAttribute("aria-busy", "true");
    var url = "/admin/api/telegram/deliveries?limit=50";
    if (botFilter && botFilter.value) {
      url += "&bot_id=" + encodeURIComponent(botFilter.value);
    }
    fetch(url, { credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(renderDashboard)
      .catch(function () {
        clearNode(panel);
        panel.appendChild(makeEl("p", "ops-hint", "Không tải được lịch sử gửi Telegram."));
      })
      .finally(function () {
        panel.setAttribute("aria-busy", "false");
      });
  }

  function sendTestMessage() {
    if (!testBtn) return;
    testBtn.disabled = true;
    if (testStatus) testStatus.textContent = "Đang gửi tin nhắn thử…";
    fetch("/admin/api/telegram/test-message", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: "{}",
    })
      .then(function (resp) {
        return resp.json().then(function (body) {
          return { ok: resp.ok, body: body };
        });
      })
      .then(function (res) {
        if (!res.ok) {
          if (testStatus) testStatus.textContent = (res.body && res.body.detail) || "Gửi tin nhắn thử thất bại.";
          return;
        }
        if (testStatus) testStatus.textContent = res.body.message_vi || "Đã gửi tin nhắn thử Telegram.";
      })
      .catch(function () {
        if (testStatus) testStatus.textContent = "Lỗi kết nối khi gửi tin nhắn thử.";
      })
      .finally(function () {
        testBtn.disabled = false;
      });
  }

  if (refreshBtn) refreshBtn.addEventListener("click", loadDeliveries);
  if (botFilter) botFilter.addEventListener("change", loadDeliveries);
  if (testBtn) testBtn.addEventListener("click", sendTestMessage);
  loadDeliveries();
})();
