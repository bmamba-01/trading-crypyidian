(() => {
  const root = document.querySelector(".learning-split-candidates");
  const status = document.getElementById("learning-split-status");
  if (!root || !status) return;
  const csrfToken = root.dataset.csrfToken;

  // Lỗi 422 của FastAPI trả `detail` dạng mảng; ghép thông điệp thay vì hiện "[object Object]".
  const describeError = (detail) => {
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail) && detail.length) {
      return `Dữ liệu chưa hợp lệ: ${detail.map((item) => item?.msg || String(item)).join("; ")}`;
    }
    return "Phê duyệt chưa thành công.";
  };

  root.addEventListener("input", (event) => {
    if (event.target.matches?.("textarea[name=reason]")) event.target.setCustomValidity("");
  });

  root.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-approve-form]");
    if (!form) return;
    event.preventDefault();
    const candidateId = form.dataset.candidateId;
    const reasonField = form.querySelector("textarea[name=reason]");
    const button = form.querySelector("button[type=submit]");
    const reason = reasonField?.value.trim();
    // `minlength` của trình duyệt đếm cả khoảng trắng; máy chủ yêu cầu ≥ 12 ký tự sau khi cắt.
    if (reasonField) {
      reasonField.setCustomValidity(
        (reason || "").length < 12 ? "Lý do cần ít nhất 12 ký tự có nội dung." : "",
      );
      if (!reasonField.reportValidity()) return;
    }
    if (!candidateId || !reason || !csrfToken) return;
    form.dataset.idempotencyKey ||= crypto.randomUUID();
    button.disabled = true;
    status.textContent = "Đang ghi nhận phê duyệt bất biến.";
    try {
      const response = await fetch(
        `/api/v1/learning-splits/candidates/${encodeURIComponent(candidateId)}/approve`,
        {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
            "Idempotency-Key": form.dataset.idempotencyKey,
          },
          body: JSON.stringify({ reason }),
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(describeError(payload.detail));
      const card = form.closest("[data-candidate-id]");
      const badge = card?.querySelector("[data-candidate-state]");
      if (badge) {
        badge.textContent = "Đã phê duyệt";
        const chip = badge.closest(".learning-split-state");
        chip?.classList.add("learning-split-state--approved", "lx-chip--success");
        chip?.classList.remove("lx-chip--warning");
      }
      const notice = document.createElement("p");
      notice.className = "lx-status learning-split-approved-at";
      notice.textContent = `Đã phê duyệt lúc ${new Date(payload.approved_at).toLocaleString("vi-VN")}. Builder có thể niêm phong sau khi kiểm lại quyền hiện hành.`;
      form.replaceWith(notice);
      status.textContent = "Approval đã được ghi; mọi lần dùng dữ liệu vẫn kiểm quyền hiện hành.";
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
})();
