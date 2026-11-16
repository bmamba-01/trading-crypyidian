// Điều khiển hộp thoại (<dialog class="glass-dialog">) dùng chung toàn Admin Web, tuân thủ CSP `default-src 'self'`.
// - `[data-dialog-open="<id>"]`: mở hộp thoại bằng showModal() và khôi phục focus khi đóng.
// - `[data-dialog-close]`: đóng hộp thoại gần nhất.
// - Nhấn ra ngoài khung (backdrop) hoặc Escape đều đóng và trả focus về nút mở.
// - `window.crypidianDialog.openDetail({ title, lead, rows, rawText, trigger })`: mở modal chi tiết dùng chung.
(() => {
  "use strict";

  const openers = new WeakMap();
  const SVG_NS = "http://www.w3.org/2000/svg";

  const makeCloseIcon = () => {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "ico");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", "M6 6 18 18M18 6 6 18");
    svg.appendChild(path);
    return svg;
  };

  const resolveDialog = (target) => {
    if (target instanceof HTMLDialogElement) return target;
    if (typeof target === "string" && target) {
      const node = document.getElementById(target);
      return node instanceof HTMLDialogElement ? node : null;
    }
    return null;
  };

  const open = (target, trigger = null) => {
    const dialog = resolveDialog(target);
    if (!dialog) return null;
    const opener = trigger instanceof HTMLElement ? trigger : document.activeElement;
    if (opener instanceof HTMLElement) openers.set(dialog, opener);
    if (!dialog.open && typeof dialog.showModal === "function") {
      dialog.showModal();
    }
    return dialog;
  };

  const close = (target) => {
    const dialog = resolveDialog(target);
    if (dialog && dialog.open) dialog.close();
  };

  const ensureDetailDialog = () => {
    const existing = document.getElementById("crypidian-detail-dialog");
    if (existing instanceof HTMLDialogElement) return existing;

    const dialog = document.createElement("dialog");
    dialog.id = "crypidian-detail-dialog";
    dialog.className = "glass-dialog";
    dialog.setAttribute("aria-labelledby", "crypidian-detail-dialog-title");

    const head = document.createElement("div");
    head.className = "glass-dialog__head";
    const titleWrap = document.createElement("div");
    titleWrap.className = "glass-dialog__titles";
    const title = document.createElement("h2");
    title.id = "crypidian-detail-dialog-title";
    title.className = "glass-dialog__title";
    const lead = document.createElement("p");
    lead.id = "crypidian-detail-dialog-lead";
    lead.className = "glass-dialog__lead";
    lead.hidden = true;
    titleWrap.append(title, lead);

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "btn btn-secondary icon-btn glass-dialog__close";
    closeBtn.setAttribute("aria-label", "Đóng hộp thoại");
    closeBtn.setAttribute("data-dialog-close", "");
    closeBtn.appendChild(makeCloseIcon());
    head.append(titleWrap, closeBtn);

    const body = document.createElement("div");
    body.id = "crypidian-detail-dialog-body";
    body.className = "glass-dialog__body";

    const actions = document.createElement("div");
    actions.className = "glass-dialog__actions";
    const doneBtn = document.createElement("button");
    doneBtn.type = "button";
    doneBtn.className = "btn btn-secondary";
    doneBtn.setAttribute("data-dialog-close", "");
    doneBtn.textContent = "Đóng";
    actions.appendChild(doneBtn);

    dialog.append(head, body, actions);
    document.body.appendChild(dialog);
    return dialog;
  };

  const openDetail = ({ title = "Chi tiết", lead = "", rows = [], rawText = "", trigger = null } = {}) => {
    const dialog = ensureDetailDialog();
    const titleEl = dialog.querySelector("#crypidian-detail-dialog-title");
    const leadEl = dialog.querySelector("#crypidian-detail-dialog-lead");
    const bodyEl = dialog.querySelector("#crypidian-detail-dialog-body");
    if (titleEl) titleEl.textContent = title;
    if (leadEl) {
      leadEl.textContent = lead;
      leadEl.hidden = !lead;
    }
    if (bodyEl) {
      const nodes = [];
      if (Array.isArray(rows) && rows.length > 0) {
        const dl = document.createElement("dl");
        dl.className = "kv";
        for (const row of rows) {
          if (!row || !row.label) continue;
          const wrap = document.createElement("div");
          const dt = document.createElement("dt");
          dt.textContent = String(row.label);
          const dd = document.createElement("dd");
          dd.textContent = row.value == null || row.value === "" ? "—" : String(row.value);
          if (row.mono) dd.className = "lx-mono";
          wrap.append(dt, dd);
          dl.appendChild(wrap);
        }
        nodes.push(dl);
      }
      if (rawText) {
        const section = document.createElement("section");
        section.className = "glass-dialog__section";
        const pre = document.createElement("pre");
        pre.className = "glass-dialog__pre";
        pre.textContent = String(rawText);
        section.appendChild(pre);
        nodes.push(section);
      }
      bodyEl.replaceChildren(...nodes);
    }
    return open(dialog, trigger);
  };

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) {
      if (target instanceof HTMLDialogElement && target.classList.contains("glass-dialog") && target.open) {
        target.close();
      }
      return;
    }

    const openTrigger = target.closest("[data-dialog-open]");
    if (openTrigger instanceof HTMLElement) {
      const id = openTrigger.getAttribute("data-dialog-open");
      if (id) open(id, openTrigger);
      return;
    }

    const closeTrigger = target.closest("[data-dialog-close]");
    if (closeTrigger) {
      const host = closeTrigger.closest("dialog");
      if (host instanceof HTMLDialogElement && host.open) host.close();
      return;
    }

    if (target instanceof HTMLDialogElement && target.classList.contains("glass-dialog") && target.open) {
      if (event.clientX === 0 && event.clientY === 0) return;
      const rect = target.getBoundingClientRect();
      const inside =
        event.clientX >= rect.left &&
        event.clientX <= rect.right &&
        event.clientY >= rect.top &&
        event.clientY <= rect.bottom;
      if (!inside) target.close();
    }
  });

  document.addEventListener(
    "close",
    (event) => {
      const dialog = event.target;
      if (!(dialog instanceof HTMLDialogElement) || !dialog.classList.contains("glass-dialog")) return;
      const opener = openers.get(dialog);
      openers.delete(dialog);
      if (opener && opener.isConnected && typeof opener.focus === "function") {
        opener.focus();
      }
    },
    true,
  );

  window.crypidianDialog = { open, close, openDetail };
})();
