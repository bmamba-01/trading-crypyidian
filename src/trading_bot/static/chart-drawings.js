"use strict";

// CK-8 — Công cụ vẽ trên /admin/chart (tự viết bằng primitive của Lightweight Charts, không dùng
// gói vẽ bên thứ ba). Bản vẽ lưu theo mã giao dịch trong localStorage của trình duyệt (giai đoạn 1;
// lưu vào DB chờ owner quyết định). Toạ độ lưu bằng thời gian (epoch giây UTC) + giá nên đổi khung
// vẫn đúng chỗ. Chỉ textContent/canvas; không HTML từ dữ liệu, không style nội tuyến.
(() => {
  const api = window.CrypidianChart;
  const toolbar = document.querySelector("[data-draw-toolbar]");
  if (!api || !toolbar) return;

  const STORAGE_PREFIX = "crypidian.chartDrawings.v1.";
  const MAX_DRAWINGS = 200;
  const MAX_TEXT = 120;
  const TYPES = ["trendline", "hline", "ray", "rect", "text", "fib"];
  const TWO_POINT = new Set(["trendline", "ray", "rect", "fib"]);
  const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  const HIT_PX = 6;
  const storageKey = `${STORAGE_PREFIX}${api.symbol}`;
  // Điện thoại/màn hẹp: chỉ xem bản vẽ (acceptance CK-8).
  const viewOnly = () =>
    window.matchMedia("(pointer: coarse)").matches || window.innerWidth < 768;

  // ---- Lưu / tải có kiểm cấu trúc (dữ liệu localStorage không được tin) ----
  const validPoint = (point) =>
    point && Number.isFinite(point.time) && Number.isFinite(point.price) && point.price > 0;
  const sanitize = (raw) => {
    if (!Array.isArray(raw)) return [];
    const output = [];
    for (const item of raw.slice(0, MAX_DRAWINGS)) {
      if (!item || !TYPES.includes(item.type) || !Array.isArray(item.points)) continue;
      const needed = TWO_POINT.has(item.type) ? 2 : 1;
      const points = item.points.slice(0, needed).map((p) => ({ time: Number(p.time), price: Number(p.price) }));
      if (points.length !== needed || !points.every(validPoint)) continue;
      output.push({
        id: String(item.id || `${Date.now()}-${output.length}`).slice(0, 40),
        type: item.type,
        points,
        text: item.type === "text" ? String(item.text || "").slice(0, MAX_TEXT) : undefined,
      });
    }
    return output;
  };
  const load = () => {
    try {
      return sanitize(JSON.parse(window.localStorage.getItem(storageKey) || "[]"));
    } catch {
      return [];
    }
  };
  const save = () => {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(drawings));
    } catch {
      /* bỏ qua khi không ghi được localStorage */
    }
  };

  let drawings = load();
  let undoStack = [];
  let redoStack = [];
  let tool = "cursor";
  let pending = null; // { type, start, end }
  let selectedId = null;
  let magnet = false;
  let locked = false;
  let hidden = false;

  const commit = (next) => {
    undoStack.push(JSON.stringify(drawings));
    if (undoStack.length > 50) undoStack.shift();
    redoStack = [];
    drawings = next;
    save();
    refresh();
  };

  // ---- Vẽ ----
  const fmtPct = (level) => `${(level * 100).toLocaleString("vi-VN", { maximumFractionDigits: 1 })}%`;
  class DrawingsLayer extends api.CanvasPrimitive {
    draw(ctx, size) {
      const dark = document.documentElement.getAttribute("data-theme") === "dark";
      const base = dark ? "#60A5FA" : "#2563EB";
      const items = pending ? [...drawings, { id: "__pending", ...pendingShape() }] : drawings;
      ctx.font = '600 10px "Be Vietnam Pro", system-ui, sans-serif';
      for (const item of items) {
        if (!item || !item.points) continue;
        const xy = item.points.map((p) => [api.xForTime(p.time), this.y(p.price)]);
        if (xy.some(([x, y]) => x === null || y === null)) continue;
        const selected = item.id === selectedId;
        ctx.strokeStyle = base;
        ctx.fillStyle = base;
        ctx.lineWidth = selected ? 2.4 : 1.4;
        ctx.globalAlpha = item.id === "__pending" ? 0.6 : 1;
        const [[x1, y1], [x2, y2] = [x1, y1]] = xy;
        ctx.beginPath();
        if (item.type === "trendline") {
          ctx.moveTo(x1, y1);
          ctx.lineTo(x2, y2);
        } else if (item.type === "ray") {
          const slope = x2 === x1 ? 0 : (y2 - y1) / (x2 - x1);
          const xEnd = x2 >= x1 ? size.width : 0;
          ctx.moveTo(x1, y1);
          ctx.lineTo(xEnd, y1 + slope * (xEnd - x1));
        } else if (item.type === "hline") {
          ctx.moveTo(0, y1);
          ctx.lineTo(size.width, y1);
        } else if (item.type === "rect") {
          ctx.globalAlpha *= 0.18;
          ctx.fillRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
          ctx.globalAlpha = item.id === "__pending" ? 0.6 : 1;
          ctx.rect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
        } else if (item.type === "fib") {
          const [p1, p2] = item.points;
          for (const level of FIB_LEVELS) {
            const price = p2.price + (p1.price - p2.price) * level;
            const y = this.y(price);
            if (y === null) continue;
            ctx.moveTo(Math.min(x1, x2), y);
            ctx.lineTo(Math.max(x1, x2, x1 + 40), y);
            ctx.fillText(`${fmtPct(level)} · ${api.fmtNumber(price, price >= 1 ? 2 : 8)}`, Math.max(x1, x2, x1 + 40) + 4, y + 3);
          }
        } else if (item.type === "text") {
          ctx.fillText(item.text || "", x1 + 4, y1 - 4);
          ctx.moveTo(x1 - 3, y1);
          ctx.lineTo(x1 + 3, y1);
        }
        ctx.stroke();
        if (selected) {
          for (const [x, y] of xy) ctx.fillRect(x - 3, y - 3, 6, 6);
        }
        ctx.globalAlpha = 1;
      }
    }
  }
  const layer = new DrawingsLayer("top");
  api.candles.attachPrimitive(layer);

  const pendingShape = () => {
    if (!pending) return null;
    return { type: pending.type, points: [pending.start, pending.end || pending.start] };
  };

  const countNode = toolbar.querySelector("[data-draw-count]");
  const refresh = () => {
    layer.set(drawings, !hidden);
    if (countNode) countNode.textContent = `${drawings.length} hình vẽ`;
    api.container.dataset.drawingsCount = String(drawings.length);
    for (const button of toolbar.querySelectorAll("[data-draw-tool]")) {
      button.setAttribute("aria-pressed", String(button.dataset.drawTool === tool));
    }
    for (const button of toolbar.querySelectorAll("[data-draw-toggle]")) {
      const state = { magnet, lock: locked, hide: hidden }[button.dataset.drawToggle];
      button.setAttribute("aria-pressed", String(Boolean(state)));
    }
    const undo = toolbar.querySelector('[data-draw-action="undo"]');
    const redo = toolbar.querySelector('[data-draw-action="redo"]');
    if (undo) undo.disabled = undoStack.length === 0;
    if (redo) redo.disabled = redoStack.length === 0;
  };

  // ---- Toạ độ điểm từ con trỏ (có nam châm bắt OHLC) ----
  const pointAt = (x, y) => {
    const time = api.timeForX(x);
    const price = api.candles.coordinateToPrice(y);
    if (time === null || price === null || !(price > 0)) return null;
    if (!magnet) return { time, price };
    const bar = api.barAt(time);
    if (!bar) return { time, price };
    const nearest = [bar.open, bar.high, bar.low, bar.close].reduce((best, value) =>
      Math.abs(value - price) < Math.abs(best - price) ? value : best
    );
    return { time: bar.time, price: nearest };
  };

  // ---- Chọn hình bằng khoảng cách tới nét vẽ ----
  const distanceToSegment = (px, py, x1, y1, x2, y2) => {
    const dx = x2 - x1;
    const dy = y2 - y1;
    const length = dx * dx + dy * dy;
    const t = length === 0 ? 0 : Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / length));
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  };
  const hitTest = (px, py) => {
    for (const item of [...drawings].reverse()) {
      const xy = item.points.map((p) => [api.xForTime(p.time), api.candles.priceToCoordinate(p.price)]);
      if (xy.some(([x, y]) => x === null || y === null)) continue;
      const [[x1, y1], [x2, y2] = [x1, y1]] = xy;
      let distance = Infinity;
      if (item.type === "trendline") distance = distanceToSegment(px, py, x1, y1, x2, y2);
      else if (item.type === "ray") distance = distanceToSegment(px, py, x1, y1, x1 + (x2 - x1) * 100, y1 + (y2 - y1) * 100);
      else if (item.type === "hline") distance = Math.abs(py - y1);
      else if (item.type === "text") distance = Math.hypot(px - x1, py - y1) - 10;
      else if (item.type === "rect") {
        const inside = px >= Math.min(x1, x2) && px <= Math.max(x1, x2) && py >= Math.min(y1, y2) && py <= Math.max(y1, y2);
        distance = inside ? 0 : Infinity;
      } else if (item.type === "fib") {
        const [p1, p2] = item.points;
        for (const level of FIB_LEVELS) {
          const y = api.candles.priceToCoordinate(p2.price + (p1.price - p2.price) * level);
          if (y !== null) distance = Math.min(distance, distanceToSegment(px, py, Math.min(x1, x2), y, Math.max(x1, x2, x1 + 40), y));
        }
      }
      if (distance <= HIT_PX) return item.id;
    }
    return null;
  };

  // ---- Sự kiện chart ----
  const textInput = toolbar.querySelector("[data-draw-text]");
  const handleClick = (param) => {
    if (!param.point || locked || hidden || viewOnly()) return;
    const { x, y } = param.point;
    if (tool === "cursor") {
      selectedId = hitTest(x, y);
      refresh();
      return;
    }
    const point = pointAt(x, y);
    if (!point) return;
    if (!TWO_POINT.has(tool)) {
      const text = tool === "text" ? (textInput?.value || "Ghi chú").slice(0, MAX_TEXT) : undefined;
      commit([...drawings, { id: `${Date.now()}`, type: tool, points: [point], text }]);
      return;
    }
    if (!pending) {
      pending = { type: tool, start: point, end: null };
      refresh();
      return;
    }
    const finished = { id: `${Date.now()}`, type: tool, points: [pending.start, point] };
    pending = null;
    if (drawings.length >= MAX_DRAWINGS) return;
    commit([...drawings, finished]);
  };
  // Dùng sự kiện click DOM của khung chart: khi nhấp nhanh lần hai, Lightweight Charts không phát
  // click/dblClick nên sẽ mất điểm. Bỏ qua thao tác kéo (> 4px) và vùng trục giá bên phải.
  let downAt = null;
  api.container.addEventListener("pointerdown", (event) => {
    downAt = [event.clientX, event.clientY];
  });
  api.container.addEventListener("click", (event) => {
    const rect = api.container.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    if (downAt && Math.hypot(event.clientX - downAt[0], event.clientY - downAt[1]) > 4) return;
    if (x < 0 || x > api.chart.timeScale().width() || y < 0) return;
    handleClick({ point: { x, y } });
  });
  api.isDrawing = () => tool !== "cursor";
  api.chart.subscribeCrosshairMove((param) => {
    if (!pending || !param.point) return;
    pending.end = pointAt(param.point.x, param.point.y) || pending.end;
    layer.set(drawings, !hidden);
  });

  const actions = {
    undo: () => {
      if (!undoStack.length) return;
      redoStack.push(JSON.stringify(drawings));
      drawings = sanitize(JSON.parse(undoStack.pop()));
      save();
      refresh();
    },
    redo: () => {
      if (!redoStack.length) return;
      undoStack.push(JSON.stringify(drawings));
      drawings = sanitize(JSON.parse(redoStack.pop()));
      save();
      refresh();
    },
    delete: () => {
      if (locked) return;
      if (selectedId) {
        commit(drawings.filter((item) => item.id !== selectedId));
        selectedId = null;
      } else if (drawings.length && window.confirm(`Xóa toàn bộ ${drawings.length} hình vẽ của ${api.symbol}?`)) {
        commit([]);
      }
    },
  };
  for (const button of toolbar.querySelectorAll("[data-draw-tool]")) {
    button.addEventListener("click", () => {
      tool = button.dataset.drawTool;
      pending = null;
      refresh();
    });
  }
  for (const button of toolbar.querySelectorAll("[data-draw-toggle]")) {
    button.addEventListener("click", () => {
      const key = button.dataset.drawToggle;
      if (key === "magnet") magnet = !magnet;
      if (key === "lock") locked = !locked;
      if (key === "hide") hidden = !hidden;
      refresh();
    });
  }
  for (const button of toolbar.querySelectorAll("[data-draw-action]")) {
    button.addEventListener("click", () => actions[button.dataset.drawAction]?.());
  }
  api.container.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      pending = null;
      selectedId = null;
      refresh();
    } else if ((event.key === "Delete" || event.key === "Backspace") && selectedId) {
      event.preventDefault();
      actions.delete();
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
      event.preventDefault();
      if (event.shiftKey) actions.redo();
      else actions.undo();
    }
  });
  refresh();
})();
