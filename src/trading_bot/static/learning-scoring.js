(() => {
  const form = document.getElementById("scorer-disable-form");
  const status = document.getElementById("learning-scoring-status");
  if (!form || !status) return;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const reason = form.elements.reason.value.trim();
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    status.hidden = false;
    status.textContent = "Đang ghi nhận yêu cầu tắt scorer.";
    try {
      const response = await fetch("/api/v1/learning-scorer/deployment", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": form.dataset.csrfToken,
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          mode: "DISABLED",
          expected_pointer_version: Number(form.dataset.expectedPointerVersion),
          expected_deployment_revision: Number(form.dataset.expectedDeploymentRevision),
          reason,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "Chưa thể tắt scorer.");
      status.textContent = "Đã ghi nhận sự kiện tắt scorer.";
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
})();
