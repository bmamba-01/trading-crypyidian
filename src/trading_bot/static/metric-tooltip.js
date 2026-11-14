"use strict";

// Chú giải chỉ số (UI-1): mở bằng rê chuột, focus bàn phím và chạm; đóng bằng Escape, bấm ra
// ngoài hoặc rời focus. Nội dung do máy chủ dựng sẵn trong phần tử role="tooltip"; tệp này chỉ
// điều khiển hiển thị và vị trí. Vị trí gán qua CSSOM nên không vi phạm CSP `default-src 'self'`.
(() => {
  const root = document.documentElement;
  const VIEWPORT_MARGIN = 8;
  const TRIGGER_GAP = 6;
  const MAX_WIDTH = 352;
  const HOVER_CLOSE_DELAY_MS = 140;

  // Khi script chạy, CSS dự phòng (:hover/:focus-within cho trình duyệt tắt JavaScript) bị tắt
  // để Escape đóng được tooltip dù con trỏ vẫn nằm trên nhãn.
  root.classList.add("metric-help-js");

  let active = null;
  let closeTimer = null;

  const triggerOf = (target) =>
    target instanceof Element ? target.closest("button.metric-help") : null;

  const bubbleOf = (trigger) => {
    const id = trigger.getAttribute("aria-describedby");
    const bubble = id ? document.getElementById(id) : null;
    return bubble && bubble.getAttribute("role") === "tooltip" ? bubble : null;
  };

  const cancelScheduledClose = () => {
    if (closeTimer !== null) {
      window.clearTimeout(closeTimer);
      closeTimer = null;
    }
  };

  const place = () => {
    if (!active) return;
    const { trigger, bubble } = active;
    const rect = trigger.getBoundingClientRect();
    const viewportWidth = root.clientWidth;
    const viewportHeight = window.innerHeight;
    bubble.style.maxWidth = `${Math.max(120, Math.min(MAX_WIDTH, viewportWidth - 2 * VIEWPORT_MARGIN))}px`;
    bubble.style.maxHeight = `${Math.max(96, viewportHeight - 2 * VIEWPORT_MARGIN)}px`;
    bubble.style.left = "0px";
    bubble.style.top = "0px";
    const width = bubble.offsetWidth;
    const height = bubble.offsetHeight;
    const left = Math.max(
      VIEWPORT_MARGIN,
      Math.min(rect.left, viewportWidth - width - VIEWPORT_MARGIN)
    );
    const below = rect.bottom + TRIGGER_GAP;
    const above = rect.top - TRIGGER_GAP - height;
    let top = below;
    if (below + height > viewportHeight - VIEWPORT_MARGIN) {
      top = above >= VIEWPORT_MARGIN ? above : Math.max(VIEWPORT_MARGIN, viewportHeight - height - VIEWPORT_MARGIN);
    }
    bubble.style.left = `${Math.round(left)}px`;
    bubble.style.top = `${Math.round(top)}px`;
  };

  const close = () => {
    cancelScheduledClose();
    if (!active) return;
    active.bubble.classList.remove("is-open");
    active.trigger.setAttribute("aria-expanded", "false");
    active = null;
  };

  const open = (trigger, pinned) => {
    const bubble = bubbleOf(trigger);
    if (!bubble) return;
    cancelScheduledClose();
    if (active && active.trigger === trigger) {
      active.pinned = active.pinned || pinned;
      return;
    }
    close();
    // Đưa tooltip ra <body> để không bị cắt bởi vùng cuộn của bảng hay transform của thẻ.
    if (bubble.parentNode !== document.body) document.body.appendChild(bubble);
    bubble.classList.add("is-open");
    trigger.setAttribute("aria-expanded", "true");
    active = { trigger, bubble, pinned };
    place();
  };

  const scheduleClose = () => {
    cancelScheduledClose();
    closeTimer = window.setTimeout(() => {
      closeTimer = null;
      if (active && !active.pinned && document.activeElement !== active.trigger) close();
    }, HOVER_CLOSE_DELAY_MS);
  };

  document.addEventListener("pointerover", (event) => {
    if (event.pointerType !== "mouse") return;
    const trigger = triggerOf(event.target);
    if (trigger) {
      open(trigger, false);
    } else if (active && active.bubble.contains(event.target)) {
      cancelScheduledClose();
    }
  });

  document.addEventListener("pointerout", (event) => {
    if (event.pointerType !== "mouse" || !active) return;
    const next = event.relatedTarget;
    if (next instanceof Node && (active.trigger.contains(next) || active.bubble.contains(next))) {
      return;
    }
    if (active.trigger.contains(event.target) || active.bubble.contains(event.target)) {
      scheduleClose();
    }
  });

  document.addEventListener("focusin", (event) => {
    const trigger = triggerOf(event.target);
    if (trigger) open(trigger, false);
  });

  document.addEventListener("focusout", (event) => {
    if (!active || !active.trigger.contains(event.target)) return;
    const next = event.relatedTarget;
    if (next instanceof Node && active.bubble.contains(next)) return;
    close();
  });

  document.addEventListener("click", (event) => {
    const trigger = triggerOf(event.target);
    if (!trigger) return;
    if (active && active.trigger === trigger) {
      // Lần bấm/chạm đầu ghim tooltip vừa mở do focus hoặc rê chuột; lần kế tiếp đóng lại.
      if (active.pinned) close();
      else active.pinned = true;
    } else {
      open(trigger, true);
    }
  });

  document.addEventListener("pointerdown", (event) => {
    if (!active || !(event.target instanceof Node)) return;
    if (active.trigger.contains(event.target) || active.bubble.contains(event.target)) return;
    close();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && active) close();
  });

  window.addEventListener("scroll", place, { capture: true, passive: true });
  window.addEventListener("resize", place);
})();
