// Nâng cấp ô chọn bằng Tom Select (tự host, Apache-2.0; xem ASSET-SOURCES.md).
//
// - `select[data-multiselect]`: chọn nhiều có tìm kiếm, mỗi mục có nút bỏ chọn.
//   `data-multiselect="coins"` hiện biểu tượng và tên coin từ `data-icon`/`data-name` của option.
// - `select[data-searchable]`: chọn một có tìm kiếm cho danh sách dài.
//
// Thẻ `<select>` gốc vẫn nằm trong form và được gửi như bình thường, nên trang vẫn dùng được khi
// script không chạy. Nội dung option dựng bằng DOM (textContent), không chèn chuỗi HTML.
(() => {
  "use strict";

  if (typeof window.TomSelect !== "function") return;

  const ICON_NAME = /^[a-z0-9-]{1,32}$/;

  const textBlock = (className, text) => {
    const block = document.createElement("div");
    block.className = className;
    block.textContent = text;
    return block;
  };

  // Một hàng coin: biểu tượng tự host (nếu có) hoặc chữ cái đầu, mã và tên đầy đủ.
  const coinRow = (data) => {
    const row = document.createElement("div");
    row.className = "ts-coin";
    const icon = typeof data.icon === "string" && ICON_NAME.test(data.icon) ? data.icon : "";
    const fallback = document.createElement("span");
    fallback.className = "ts-coin__letter";
    fallback.setAttribute("aria-hidden", "true");
    fallback.textContent = String(data.value || "?").charAt(0);
    if (icon) {
      const image = document.createElement("img");
      image.className = "ts-coin__icon";
      image.src = `/static/coins/${icon}.svg`;
      image.alt = "";
      image.width = 18;
      image.height = 18;
      image.addEventListener("error", () => image.replaceWith(fallback), { once: true });
      row.append(image);
    } else {
      row.append(fallback);
    }
    const code = document.createElement("span");
    code.className = "ts-coin__code";
    code.textContent = String(data.value || "");
    row.append(code);
    if (typeof data.name === "string" && data.name) {
      const name = document.createElement("span");
      name.className = "ts-coin__name";
      name.textContent = data.name;
      row.append(name);
    }
    return row;
  };

  const enhance = (select) => {
    if (select.tomselect) return;
    const multiple = select.multiple;
    const coins = select.dataset.multiselect === "coins";
    const render = {
      no_results: () => textBlock("no-results", "Không có lựa chọn phù hợp"),
    };
    if (coins) {
      render.option = coinRow;
      render.item = coinRow;
    }
    new window.TomSelect(select, {
      create: false,
      maxOptions: null,
      allowEmptyOption: !multiple,
      hidePlaceholder: false,
      closeAfterSelect: !multiple,
      placeholder: select.dataset.placeholder || (multiple ? "Gõ để tìm và chọn…" : ""),
      plugins: multiple
        ? {
            remove_button: { title: "Bỏ chọn mục này" },
            clear_button: { title: "Bỏ chọn tất cả" },
          }
        : {},
      render,
    });
  };

  const run = () => {
    for (const select of document.querySelectorAll(
      "select[data-multiselect], select[data-searchable]",
    )) {
      enhance(select);
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run, { once: true });
  } else {
    run();
  }
})();
