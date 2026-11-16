"use strict";

// CK-11 — Watchlist cá nhân trên trang bộ quét: lưu trong localStorage (giai đoạn 1), hiện số liệu
// quét của từng mã đã chọn và link mở chart. Chỉ textContent; bọc try/catch localStorage.
(() => {
  const select = document.querySelector("[data-watchlist]");
  const list = document.querySelector("[data-watchlist-rows]");
  const dataNode = document.getElementById("scanner-data");
  if (!select || !list || !dataNode) return;
  const KEY = "crypidian.watchlist.v1";
  let data = { rows: [], excluded: [] };
  try {
    data = JSON.parse(dataNode.textContent || "{}");
  } catch {
    /* giữ rỗng */
  }
  const rows = new Map(data.rows.map((row) => [row.symbol, row]));
  const excluded = new Map(data.excluded.map((row) => [row.symbol, row.reason]));
  const fmt = (value, digits = 2) =>
    value === null || value === undefined ? "—" : Number(value).toLocaleString("vi-VN", { maximumFractionDigits: digits });

  const saved = (() => {
    try {
      const parsed = JSON.parse(window.localStorage.getItem(KEY) || "[]");
      return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string").slice(0, 50) : [];
    } catch {
      return [];
    }
  })();
  for (const option of select.options) option.selected = saved.includes(option.value);
  // Tom Select (form-controls.js) đã khởi tạo trước: cập nhật cả giao diện chip, không phát change.
  if (select.tomselect) select.tomselect.setValue(saved.filter((symbol) => rows.has(symbol) || excluded.has(symbol)), true);

  const render = () => {
    const chosen = Array.from(select.selectedOptions).map((option) => option.value);
    try {
      window.localStorage.setItem(KEY, JSON.stringify(chosen));
    } catch {
      /* bỏ qua khi không ghi được */
    }
    if (chosen.length === 0) {
      const li = document.createElement("li");
      li.textContent = "Chưa chọn mã nào.";
      list.replaceChildren(li);
      return;
    }
    list.replaceChildren(
      ...chosen.map((symbol) => {
        const li = document.createElement("li");
        li.dataset.watchSymbol = symbol;
        const link = document.createElement("a");
        link.href = `/admin/chart?symbol=${encodeURIComponent(symbol)}&timeframe=1h`;
        link.textContent = symbol;
        const row = rows.get(symbol);
        const detail = row
          ? ` — giá ${fmt(row.close)}, 7 ngày ${row.change_7d_pct >= 0 ? "+" : ""}${fmt(row.change_7d_pct)}%, biên 21 ngày ${fmt(row.range_21d_pct)}%, quote 24h ${fmt(row.quote_volume_24h, 0)}`
          : ` — ${excluded.get(symbol) || "không có trong kết quả quét"}`;
        li.append(link, detail);
        return li;
      })
    );
  };
  select.addEventListener("change", render);
  render();
})();
