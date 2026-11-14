"use strict";

// Tương tác Microscope Replay: nạp mẫu nến, gọi API soi vết và dựng kết quả 7 tầng L0–L6.
//
// Một hàm `render(trace)` duy nhất dựng mọi trạng thái từ dữ liệu `MicroscopeTrace` (cả vết ban đầu
// do máy chủ tính sẵn trong #initial-trace-data lẫn kết quả API), nên giao diện không thể lệch
// giữa lần tải đầu và lần soi sau. Chỉ dựng DOM bằng createElement/textContent (không innerHTML).
// Số hiển thị làm tròn 2 chữ số theo định dạng Việt Nam; giá trị gốc nằm ở `title`/`data-value`.
(() => {
  const readJson = (id, fallback) => {
    const node = document.getElementById(id);
    if (!node) return fallback;
    try {
      return JSON.parse(node.textContent || "null") ?? fallback;
    } catch {
      return fallback;
    }
  };

  const cardId = readJson("microscope-card-id", null);
  if (typeof cardId !== "string" || !cardId) return;
  const presets = readJson("presets-data", []);
  const initialTrace = readJson("initial-trace-data", null);

  const form = document.getElementById("microscope-inspect-form");
  const inputs = {
    open: document.getElementById("input-open"),
    high: document.getElementById("input-high"),
    low: document.getElementById("input-low"),
    close: document.getElementById("input-close"),
    volume: document.getElementById("input-volume"),
    minAtr: document.getElementById("input-min-atr"),
  };
  const directionSelect = document.getElementById("input-direction");
  const submitButton = document.getElementById("btn-inspect-submit");
  const submitLabel = submitButton ? submitButton.querySelector("[data-inspect-label]") : null;
  const presetButtons = [...document.querySelectorAll("button[data-preset-id]")];
  const statusSection = document.getElementById("pipeline-status-section");

  const LAYERS = [
    { slug: "l0", key: "L0_DATA_HYGIENE" },
    { slug: "l1", key: "L1_CANDLE_ANATOMY" },
    { slug: "l2", key: "L2_INDICATORS" },
    { slug: "l3", key: "L3_ALGORITHMS" },
    { slug: "l4", key: "L4_CONTEXT_GATE" },
    { slug: "l5", key: "L5_STRATEGY_TRIGGER" },
    { slug: "l6", key: "L6_ZONES" },
  ];
  // Màu chấm và nhãn rút gọn trên đường ống theo trạng thái tầng.
  const DOT = { pass: "dot-green", trigger: "dot-green", fail: "dot-red", block: "dot-red", idle: "dot-yellow", skip: "dot-gray", none: "dot-gray" };
  const RAIL = { pass: "Đạt", trigger: "Kích hoạt", fail: "Vi phạm", block: "Chặn", idle: "Không tín hiệu", skip: "Bỏ qua", none: "Không dùng" };
  const NODE_STATUS = {
    SUCCESS: ["pass", "Đạt"],
    PASS: ["pass", "Cho qua"],
    TRIGGERED: ["trigger", "Kích hoạt"],
    FAILED: ["fail", "Vi phạm"],
    BLOCK: ["block", "Chặn"],
    NO_SIGNAL: ["idle", "Không tín hiệu"],
    SKIPPED: ["skip", "Bỏ qua"],
  };

  const number2 = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 });
  const fmt2 = (value) => (typeof value === "number" && Number.isFinite(value) ? number2.format(value) : "Chưa có dữ liệu");

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const svgIcon = (paths) => {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "ico");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    for (const d of paths) {
      const path = document.createElementNS(ns, "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    return svg;
  };
  const copyButton = (value, label) => {
    const button = el("button", "copy-btn");
    button.type = "button";
    button.dataset.copy = value;
    button.setAttribute("aria-label", label);
    button.title = "Sao chép mã đầy đủ";
    button.append(svgIcon(["M9 9h11v11H9z", "M5 15V5a2 2 0 0 1 2-2h10"]));
    return button;
  };

  const tracesOf = (trace, key) => (Array.isArray(trace.layer_traces) ? trace.layer_traces.filter((item) => item.layer === key) : []);
  const failedAtL0 = (trace) => Boolean(trace.is_short_circuited && String(trace.short_circuit_reason || "").includes("L0"));

  // Trạng thái tổng của từng tầng: [mã trạng thái, chữ trên huy hiệu]. Chữ giữ đúng nhãn đã có.
  const layerState = (trace, layer) => {
    const nodes = tracesOf(trace, layer.key);
    const allSkipped = nodes.length > 0 && nodes.every((item) => item.status === "SKIPPED");
    const signal = String(trace.trigger_signal || "");
    switch (layer.slug) {
      case "l0":
        return nodes.some((item) => item.status === "FAILED") ? ["fail", "VI PHẠM (FAILED)"] : ["pass", "ĐẠT CHUẨN (PASSED)"];
      case "l4":
        if (trace.context_gate_passed) return ["pass", "CHO PHÉP (PASS)"];
        if (failedAtL0(trace) || allSkipped) return ["skip", "BỎ QUA (SKIPPED)"];
        return ["block", "CHẶN ĐỨNG (BLOCK)"];
      case "l5":
        if (signal === "LONG_ENTRY" || signal === "SHORT_ENTRY") return ["trigger", `KÍCH HOẠT (${signal})`];
        if (trace.is_short_circuited) return ["skip", "BỎ QUA (SKIPPED)"];
        return ["idle", "KHÔNG TÍN HIỆU (NO_SIGNAL)"];
      case "l6":
        if (trace.zones) return ["pass", "ĐÃ THIẾT LẬP (ZONES READY)"];
        if (trace.is_short_circuited) return ["skip", "BỎ QUA (SKIPPED)"];
        return ["idle", "CHƯA KÍCH HOẠT"];
      default:
        if (failedAtL0(trace) || allSkipped) return ["skip", "BỎ QUA (SKIPPED)"];
        if (nodes.length === 0) return ["none", "KHÔNG DÙNG TẦNG NÀY"];
        if (nodes.some((item) => item.status === "FAILED")) return ["fail", "VI PHẠM (FAILED)"];
        return ["pass", "HOÀN THÀNH (SUCCESS)"];
    }
  };

  const chip = (status) => {
    const [state, label] = NODE_STATUS[status] || ["none", status || "Chưa rõ"];
    const node = el("span", "mx-chip", label);
    node.dataset.state = state;
    return node;
  };

  // Một nút có đầu ra số → hàng nhãn–giá trị; đầu ra chữ → khối có tiêu đề và đoạn mô tả.
  const nodeRow = (item) => {
    const row = el("div", "kv");
    const term = el("dt");
    term.append(el("span", null, item.node_id), el("code", null, item.operation));
    const value = el("dd");
    if (item.status === "SKIPPED") {
      value.textContent = "Bỏ qua";
      value.className = "value-muted";
    } else {
      value.textContent = fmt2(item.output);
      value.title = String(item.output);
      value.dataset.value = String(item.output);
    }
    row.append(term, value);
    return row;
  };
  const nodeBlock = (item) => {
    const block = el("div", "mx-node");
    const head = el("div", "mx-node__head");
    const name = el("span", "mx-node__name");
    name.append(el("span", null, item.node_id), el("code", null, item.operation));
    head.append(name, chip(item.status));
    block.append(head);
    if (item.output !== null && item.output !== undefined && typeof item.output !== "object") {
      block.append(el("p", "mx-node__out", item.output));
    }
    return block;
  };
  const renderNodes = (container, nodes, emptyText) => {
    container.replaceChildren();
    if (nodes.length === 0) {
      container.append(el("p", "mx-empty", emptyText));
      return;
    }
    const numeric = nodes.filter((item) => typeof item.output === "number" || item.status === "SKIPPED");
    const textual = nodes.filter((item) => !numeric.includes(item));
    if (numeric.length) {
      const list = el("dl", "kv-grid");
      for (const item of numeric) list.append(nodeRow(item));
      container.append(list);
    }
    for (const item of textual) container.append(nodeBlock(item));
  };

  const zoneRow = (label, id, value, tone) => {
    const row = el("div", "kv");
    row.append(el("dt", null, label));
    const dd = el("dd", tone || null);
    const span = el("span", null, fmt2(value));
    span.id = id;
    span.dataset.value = String(value);
    span.title = String(value);
    dd.append(span);
    row.append(dd);
    return row;
  };
  const renderZones = (container, trace) => {
    container.replaceChildren();
    const zones = trace.zones;
    if (!zones) {
      const empty = el("div", "empty-zones-placeholder");
      empty.id = "empty-zones-msg";
      empty.append(el("p", "mx-empty", "Chưa có vùng thực thi do quy trình bị ngắt mạch hoặc bộ kích hoạt không phát tín hiệu."));
      container.append(empty);
      return;
    }
    const list = el("dl", "kv-grid");
    list.append(
      zoneRow("Điểm vào lệnh (Entry)", "zone-entry-val", zones.entry_price),
      zoneRow("Dừng lỗ ban đầu (Stop Loss)", "zone-sl-val", zones.stop_loss, "mx-zone-stop"),
      zoneRow("Chốt lời mục tiêu (Take Profit)", "zone-tp-val", zones.take_profit, "mx-zone-target"),
    );
    const rr = el("div", "kv");
    rr.append(el("dt", null, "Tỷ lệ Risk : Reward"));
    const rrValue = el("dd");
    const rrSpan = el("span", null, `1 : ${fmt2(zones.risk_reward_ratio)}`);
    rrSpan.id = "zone-rr-val";
    rrSpan.dataset.value = String(zones.risk_reward_ratio);
    rrValue.append(rrSpan);
    rr.append(rrValue);
    list.append(rr);
    container.append(list);

    const candidate = String(trace.candidate_id || "");
    if (candidate) {
      const box = el("div", "mx-candidate candidate-id-box");
      box.append(el("span", "candidate-label", "Mã ứng viên (SHA-256 tất định)"));
      const idChip = el("span", "id-chip");
      const code = el("code", "candidate-code", candidate.length > 12 ? `${candidate.slice(0, 8)}…${candidate.slice(-4)}` : candidate);
      code.id = "candidate-id-val";
      code.title = candidate;
      code.dataset.value = candidate;
      idChip.append(code, el("span", "visually-hidden", candidate), copyButton(candidate, "Sao chép mã ứng viên đầy đủ"));
      box.append(idChip);
      container.append(box);
    }
  };

  const renderBanner = (trace) => {
    if (!statusSection) return;
    const signal = String(trace.trigger_signal || "");
    const triggered = signal === "LONG_ENTRY" || signal === "SHORT_ENTRY";
    let variant;
    let heading;
    let message;
    let hint;
    let icon;
    if (trace.is_short_circuited) {
      variant = "banner-short-circuit";
      heading = "NGẮT MẠCH TỨC THÌ (SHORT-CIRCUITED)";
      message = String(trace.short_circuit_reason || "Quy trình dừng sớm.");
      hint = "Các tầng sau bị cô lập: bộ kích hoạt L5 không chạy và không tạo vùng giá L6.";
      icon = ["M12 3 2 20h20L12 3Z", "M12 10v4M12 17h.01"];
    } else if (triggered && trace.zones) {
      variant = "banner-success";
      heading = "QUY TRÌNH THÔNG SUỐT (PIPELINE PASSED)";
      message = `Nến qua cổng bối cảnh L4 và kích hoạt tín hiệu ${signal}.`;
      hint = "Đã đóng gói ứng viên (StrategyCandidate) kèm vùng giá L6. Đây là mô phỏng, không có lệnh nào được gửi.";
      icon = ["M20 6 9 17l-5-5"];
    } else {
      variant = "banner-neutral";
      heading = "QUY TRÌNH THÔNG SUỐT (PIPELINE PASSED)";
      message = "Nến qua cổng bối cảnh L4 nhưng bộ kích hoạt L5 không phát tín hiệu.";
      hint = "Không tạo ứng viên và vùng giá L6 cho nến này.";
      icon = ["M12 8v4M12 16h.01", "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z"];
    }
    const banner = el("div", `alert-banner ${variant}`);
    banner.id = "status-banner";
    banner.setAttribute("role", trace.is_short_circuited ? "alert" : "status");
    const iconBox = el("div", "banner-icon");
    iconBox.setAttribute("aria-hidden", "true");
    iconBox.append(svgIcon(icon));
    const body = el("div", "banner-body");
    const reason = el("p", "banner-message", message);
    reason.id = "banner-reason";
    body.append(el("h3", "banner-heading", heading), reason, el("p", "banner-hint", hint));
    banner.append(iconBox, body);
    statusSection.replaceChildren(banner);
  };

  const CONTAINERS = {
    l0: "l0-trace-content",
    l1: "l1-metrics-grid",
    l2: "l2-metrics-grid",
    l3: "l3-trace-content",
    l4: "l4-trace-content",
    l5: "l5-trace-content",
  };

  const render = (trace) => {
    if (!trace || !Array.isArray(trace.layer_traces)) return;
    renderBanner(trace);
    for (const layer of LAYERS) {
      const [state, label] = layerState(trace, layer);
      const card = document.getElementById(`card-layer-${layer.slug}`);
      if (card) {
        card.dataset.state = state;
        card.classList.toggle("layer-blocked", state === "block" || state === "fail");
      }
      const badge = document.getElementById(`${layer.slug}-status-badge`);
      if (badge) {
        badge.dataset.state = state;
        const dot = el("span", `status-dot ${DOT[state]}`);
        dot.setAttribute("aria-hidden", "true");
        badge.replaceChildren(dot, document.createTextNode(label));
      }
      const step = document.querySelector(`[data-rail-layer="${layer.slug}"]`);
      if (step) {
        step.dataset.state = state;
        const text = step.querySelector("[data-rail-state]");
        if (text) text.textContent = RAIL[state];
      }
      if (layer.slug === "l6") {
        const wrapper = document.getElementById("l6-zones-wrapper");
        if (wrapper) renderZones(wrapper, trace);
        continue;
      }
      const container = document.getElementById(CONTAINERS[layer.slug]);
      if (container) {
        renderNodes(container, tracesOf(trace, layer.key), "Thẻ này không khai báo nút ở tầng này.");
      }
    }
  };

  // Gửi một lượt soi; bỏ kết quả của lượt cũ nếu người dùng đã bấm lượt mới (tránh kết quả lệch).
  let controller = null;
  let generation = 0;
  const setBusy = (busy) => {
    if (!submitButton) return;
    submitButton.disabled = busy;
    submitButton.setAttribute("aria-busy", busy ? "true" : "false");
    if (submitLabel) submitLabel.textContent = busy ? "Đang phân tích…" : "Soi vết nến (Inspect Bar)";
  };
  const readPayload = () => {
    const values = {};
    for (const [name, input] of Object.entries(inputs)) {
      if (!(input instanceof HTMLInputElement)) return null;
      const raw = String(input.value || "").trim();
      if (name === "minAtr" && raw === "") {
        values[name] = null;
        continue;
      }
      const value = Number(raw);
      if (raw === "" || !Number.isFinite(value) || value < 0) {
        input.focus();
        return null;
      }
      values[name] = value;
    }
    const context = { direction: directionSelect ? directionSelect.value : "LONG" };
    if (values.minAtr !== null) context.min_atr = values.minAtr;
    return {
      ohlcv: { open: values.open, high: values.high, low: values.low, close: values.close, volume: values.volume },
      context,
    };
  };
  const toast = (options) => {
    if (window.crypidianToast && typeof window.crypidianToast.show === "function") window.crypidianToast.show(options);
  };

  const inspect = async () => {
    const payload = readPayload();
    if (!payload) {
      toast({ tone: "warning", title: "Giá trị nến chưa hợp lệ", message: "Nhập số không âm cho Open, High, Low, Close và Volume." });
      return;
    }
    controller?.abort();
    const request = new AbortController();
    controller = request;
    const token = ++generation;
    setBusy(true);
    try {
      const response = await fetch(`/admin/api/strategy-cards/${encodeURIComponent(cardId)}/inspect-bar`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(payload),
        signal: request.signal,
      });
      if (!response.ok) throw new Error(`Máy chủ trả mã ${response.status}`);
      const data = await response.json();
      if (token !== generation) return;
      if (data && data.status === "success" && data.trace) render(data.trace);
      else throw new Error("Phản hồi không có vết soi");
    } catch (error) {
      if (error && error.name === "AbortError") return;
      toast({ tone: "danger", title: "Không soi được nến", message: `${error && error.message ? error.message : "Lỗi không xác định"}. Kết quả trước vẫn được giữ.` });
    } finally {
      if (token === generation) setBusy(false);
    }
  };

  for (const button of presetButtons) {
    button.addEventListener("click", () => {
      const target = presets.find((item) => item && item.id === button.dataset.presetId);
      if (!target) return;
      for (const other of presetButtons) {
        other.classList.toggle("preset-active", other === button);
        other.setAttribute("aria-pressed", other === button ? "true" : "false");
      }
      const ohlcv = target.ohlcv || {};
      for (const name of ["open", "high", "low", "close", "volume"]) {
        if (ohlcv[name] !== undefined && inputs[name]) inputs[name].value = ohlcv[name];
      }
      const context = target.context || {};
      if (inputs.minAtr) inputs.minAtr.value = context.min_atr !== undefined && context.min_atr !== null ? context.min_atr : "";
      if (context.direction && directionSelect) directionSelect.value = context.direction;
      inspect();
    });
  }

  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      for (const other of presetButtons) {
        other.classList.remove("preset-active");
        other.setAttribute("aria-pressed", "false");
      }
      inspect();
    });
  }

  render(initialTrace);
})();
