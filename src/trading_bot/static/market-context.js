/* Bối cảnh thị trường có provenance trên trang báo cáo (CRY-374).
 * Mọi dữ liệu từ API được ghi bằng textContent: không nội suy HTML, không dùng innerHTML. */
(function () {
  "use strict";

  var KIND_VI = {
    funding: "Funding",
    open_interest: "Open interest",
    liquidation: "Thanh lý",
    mark_price: "Mark price",
    trade_flow: "Trade flow",
  };

  function text(tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value;
    return node;
  }

  function render(result, context) {
    result.replaceChildren();
    result.dataset.marketContextLoaded = "1";
    result.dataset.displayAllowed = context.display_allowed ? "1" : "0";

    result.appendChild(
      text(
        "p",
        "market-context-summary",
        "Symbol " + context.symbol + " · quyền hiển thị: " + (context.display_allowed ? "có" : "không") +
          " · cập nhật " + context.as_of
      )
    );

    var alerts = Array.isArray(context.alerts) ? context.alerts : [];
    if (alerts.length) {
      var alertBox = text("div", "market-context-alerts", "");
      alerts.forEach(function (alert) {
        alertBox.appendChild(text("span", "market-context-alert", alert.message_vi || alert.code));
      });
      result.appendChild(alertBox);
    }
    if (!context.display_allowed) return;

    var coverage = Array.isArray(context.coverage) ? context.coverage : [];
    if (coverage.length) {
      var grid = text("div", "report-breakdown-grid market-context-grid", "");
      coverage.forEach(function (item) {
        var card = text("div", "report-breakdown-card market-context-item", "");
        card.appendChild(
          text(
            "strong",
            "market-context-item__label",
            KIND_VI[item.kind] || item.kind
          )
        );
        card.appendChild(
          text(
            "span",
            "market-context-item__count",
            item.count + " / " + item.expected_count + " mẫu" + (item.stale ? " · cũ" : "")
          )
        );
        if (item.latest_event_at) {
          card.appendChild(text("span", "market-context-item__time", "Gần nhất: " + item.latest_event_at));
        }
        grid.appendChild(card);
      });
      result.appendChild(grid);
    }

    var observations = Array.isArray(context.observations) ? context.observations.slice(0, 8) : [];
    if (observations.length) {
      var list = text("ul", "market-context-observations", "");
      observations.forEach(function (item) {
        var row = text("li", "market-context-observation", "");
        row.appendChild(
          text(
            "span",
            "market-context-observation__value",
            (KIND_VI[item.kind] || item.kind) + ": " + (item.value === null ? "—" : item.value) +
              (item.unit ? " " + item.unit : "")
          )
        );
        row.appendChild(
          text(
            "small",
            "market-context-observation__source",
            item.source + " · " + item.checksum.slice(0, 12) + "… · " + item.event_at
          )
        );
        list.appendChild(row);
      });
      result.appendChild(list);
    }
  }

  function boot() {
    var form = document.getElementById("market-context-form");
    var input = document.getElementById("market-context-symbol");
    var result = document.getElementById("market-context-result");
    if (!form || !input || !result) return;

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var symbol = (input.value || "").trim();
      if (!symbol) return;
      result.dataset.marketContextLoaded = "0";
      result.textContent = "Đang kiểm tra bối cảnh thị trường…";

      fetch("/api/v1/reports/market-context?symbol=" + encodeURIComponent(symbol), {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      })
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.json();
        })
        .then(function (payload) {
          render(result, payload.market_context || {});
        })
        .catch(function (error) {
          result.dataset.marketContextLoaded = "1";
          result.dataset.displayAllowed = "0";
          result.textContent = "Không tải được bối cảnh thị trường: " + error.message;
        });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
