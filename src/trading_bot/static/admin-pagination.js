// Phân trang danh sách dài trong Admin; danh sách ngắn được giữ nguyên.
(() => {
  "use strict";

  const DEFAULT_PAGE_SIZE = 20;
  const states = new WeakMap();
  let generatedId = 0;

  const childrenFor = (container) => {
    const selector = container.getAttribute("data-admin-pagination-items");
    if (selector) {
      try {
        return Array.from(container.querySelectorAll(`:scope > ${selector}`));
      } catch {
        return [];
      }
    }
    if (container instanceof HTMLTableSectionElement && container.tagName === "TBODY") {
      return Array.from(container.children).filter((item) => item.tagName === "TR");
    }
    if (container instanceof HTMLUListElement || container instanceof HTMLOListElement) {
      return Array.from(container.children).filter((item) => item.tagName === "LI");
    }
    if (container.getAttribute("role") === "list") {
      return Array.from(container.children).filter(
        (item) => item.getAttribute("role") === "listitem",
      );
    }
    return [];
  };

  const makeIcon = (direction) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "ico");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", direction === "previous" ? "m15 18-6-6 6-6" : "m9 18 6-6-6-6");
    svg.appendChild(path);
    return svg;
  };

  const makeButton = (label, direction) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-secondary btn--icon admin-pagination__button";
    button.setAttribute("aria-label", label);
    button.title = label;
    button.appendChild(makeIcon(direction));
    return button;
  };

  const getLabel = (container) => {
    const explicit = container.getAttribute("data-pagination-label");
    if (explicit) return explicit;
    const table = container.closest("table");
    if (table?.getAttribute("aria-label")) return table.getAttribute("aria-label");
    if (container.getAttribute("aria-label")) return container.getAttribute("aria-label");
    const heading = container.closest("section, article")?.querySelector("h2, h3, h4");
    return heading?.textContent?.trim() || "danh sách";
  };

  const makeState = (container) => {
    const nav = document.createElement("nav");
    nav.className = "admin-pagination";
    nav.setAttribute("aria-label", `Phân trang ${getLabel(container)}`);
    nav.setAttribute("data-admin-pagination-nav", "");
    if (!container.id) {
      generatedId += 1;
      container.id = `admin-page-list-${generatedId}`;
    }
    nav.setAttribute("aria-controls", container.id);

    const previous = makeButton("Trang trước", "previous");
    const status = document.createElement("span");
    status.className = "admin-pagination__status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const next = makeButton("Trang sau", "next");
    nav.append(previous, status, next);

    const explicitItems = container.hasAttribute("data-admin-pagination-items");
    const anchor = container instanceof HTMLTableSectionElement
      ? container.closest("table")
      : container;
    if (explicitItems) container.appendChild(nav);
    else if (anchor) anchor.insertAdjacentElement("afterend", nav);

    const state = { container, nav, previous, status, next, pageIndex: 0, pageSize: DEFAULT_PAGE_SIZE };
    previous.addEventListener("click", () => {
      state.pageIndex = Math.max(0, state.pageIndex - 1);
      render(state);
    });
    next.addEventListener("click", () => {
      state.pageIndex += 1;
      render(state);
    });
    return state;
  };

  const render = (state) => {
    const items = childrenFor(state.container);
    const requestedSize = Number.parseInt(state.container.getAttribute("data-page-size") || "", 10);
    state.pageSize = Number.isFinite(requestedSize) && requestedSize > 0
      ? requestedSize
      : DEFAULT_PAGE_SIZE;
    if (items.length <= state.pageSize) {
      for (const item of items) item.removeAttribute("data-admin-page-hidden");
      state.nav.remove();
      states.delete(state.container);
      return;
    }

    const pageCount = Math.ceil(items.length / state.pageSize);
    state.pageIndex = Math.min(state.pageIndex, pageCount - 1);
    const start = state.pageIndex * state.pageSize;
    const end = Math.min(start + state.pageSize, items.length);
    items.forEach((item, index) => {
      if (index >= start && index < end) item.removeAttribute("data-admin-page-hidden");
      else item.setAttribute("data-admin-page-hidden", "true");
    });
    state.previous.disabled = state.pageIndex === 0;
    state.next.disabled = state.pageIndex === pageCount - 1;
    state.status.textContent = `${start + 1}–${end} / ${items.length}`;
    states.set(state.container, state);
  };

  const refresh = (root = document) => {
    const candidates = [];
    if (root instanceof Element && root.matches("tbody, ul, ol, [role='list'], [data-admin-pagination-items]")) {
      candidates.push(root);
    }
    if (root.querySelectorAll) {
      candidates.push(...root.querySelectorAll("tbody, ul, ol, [role='list'], [data-admin-pagination-items]"));
    }
    for (const container of candidates) {
      const items = childrenFor(container);
      let state = states.get(container);
      if (!state && items.length > DEFAULT_PAGE_SIZE) state = makeState(container);
      if (state) render(state);
    }
  };

  window.crypidianPagination = { refresh };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => refresh(), { once: true });
  } else {
    refresh();
  }
  const observer = new MutationObserver((records) => {
    const listChanged = records.some((record) => {
      const target = record.target instanceof Element ? record.target : record.target.parentElement;
      return !target?.closest("[data-admin-pagination-nav]");
    });
    if (listChanged) refresh();
  });
  if (document.documentElement) observer.observe(document.documentElement, { childList: true, subtree: true });
})();
