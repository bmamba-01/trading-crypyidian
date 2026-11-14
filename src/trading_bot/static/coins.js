// Chip mã coin cho hợp đồng Binance USDⓈ-M: biểu tượng chữ cái + mã gốc + tên đầy đủ.
//
// Danh mục `/static/data/binance-usdm-perpetual-usdt.json` lấy từ exchangeInfo công khai
// (perpetual, đang giao dịch, quote USDT; xem trường `source`, `fetched_at`). Biểu tượng là
// chữ cái đầu trên nền màu tính từ mã (không tải ảnh ngoài vì CSP và quyền sử dụng).
// Mã không có trong danh mục vẫn hiển thị, kèm ghi chú "ngoài danh mục perpetual USDT".
(() => {
  "use strict";

  const CATALOG_URL = "/static/data/binance-usdm-perpetual-usdt.json";
  let catalog = null;
  const pending = new Set();

  const baseOf = (symbol) => String(symbol || "").replace(/USDT$/, "") || String(symbol || "?");
  const hue = (text) => {
    let hash = 0;
    for (const char of text) hash = (hash * 31 + char.charCodeAt(0)) % 360;
    return hash;
  };

  const describe = (chip) => {
    const symbol = chip.dataset.symbol;
    const entry = catalog && catalog.get(symbol);
    const name = entry && entry.name ? entry.name : null;
    const label = chip.querySelector(".coin-chip__name");
    if (label) label.textContent = name || "";
    chip.title = entry
      ? `${symbol} · ${name || entry.base} · Binance USDⓈ-M perpetual`
      : catalog
        ? `${symbol} · ngoài danh mục perpetual USDT đang giao dịch`
        : symbol;
  };

  const load = () => {
    if (catalog || load.started) return;
    load.started = true;
    fetch(CATALOG_URL, { credentials: "same-origin" })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        catalog = new Map((data && Array.isArray(data.symbols) ? data.symbols : []).map((item) => [item.symbol, item]));
        for (const chip of pending) if (chip.isConnected) describe(chip);
        pending.clear();
      })
      .catch(() => {
        catalog = new Map();
      });
  };

  const chip = (symbol) => {
    const base = baseOf(symbol);
    const element = document.createElement("span");
    element.className = "coin-chip";
    element.dataset.symbol = String(symbol || "");
    const avatar = document.createElement("span");
    avatar.className = "coin-chip__avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = base.replace(/^1000+/, "").slice(0, 1) || "?";
    avatar.style.setProperty("--coin-hue", String(hue(base)));
    const code = document.createElement("strong");
    code.className = "coin-chip__code";
    code.textContent = String(symbol || "");
    const name = document.createElement("span");
    name.className = "coin-chip__name";
    element.append(avatar, code, name);
    if (catalog) describe(element);
    else {
      pending.add(element);
      load();
    }
    return element;
  };

  window.crypidianCoins = { chip, baseOf };

  // Nâng các thẻ mã do máy chủ dựng (`[data-coin]`) thành chip có biểu tượng và tên.
  const upgrade = () => {
    for (const tag of document.querySelectorAll("[data-coin]")) {
      tag.replaceWith(chip(tag.dataset.coin));
    }
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", upgrade);
  else upgrade();
})();
