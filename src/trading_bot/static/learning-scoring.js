(() => {
  const form = document.getElementById("scorer-deployment-form");
  const status = document.getElementById("learning-scoring-status");
  const errorSummary = document.getElementById("scorer-error");
  if (!form || !status) return;

  const mode = form.elements.mode;
  const reasonField = form.elements.reason;
  const submitButton = form.querySelector("button[type='submit']");
  const refreshSubmitState = () => {
    const selectedOption = mode.selectedOptions[0];
    submitButton.disabled = !selectedOption || selectedOption.disabled ||
      reasonField.disabled || reasonField.value.trim().length < 12;
    submitButton.textContent = mode.value === "DISABLED"
      ? "Tắt chấm điểm"
      : "Gửi yêu cầu bật chế độ";
  };

  mode.addEventListener("change", refreshSubmitState);
  reasonField.addEventListener("input", refreshSubmitState);
  refreshSubmitState();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const selectedOption = mode.selectedOptions[0];
    if (!selectedOption || selectedOption.disabled) return;
    const reason = reasonField.value.trim();
    const body = {
      mode: mode.value,
      expected_pointer_version: Number(form.dataset.expectedPointerVersion),
      expected_deployment_revision: Number(form.dataset.expectedDeploymentRevision),
      reason,
    };
    if (mode.value !== "DISABLED") {
      if (!form.dataset.evaluationReportId) {
        errorSummary.hidden = false;
        errorSummary.textContent = "Báo cáo hiện hành thiếu mã định danh. Hãy tải lại trang sau khi báo cáo được lưu.";
        errorSummary.focus();
        return;
      }
      body.evaluation_report_id = form.dataset.evaluationReportId;
    }
    const fingerprint = JSON.stringify(body);
    if (form.dataset.idempotencyFingerprint !== fingerprint) {
      form.dataset.idempotencyFingerprint = fingerprint;
      form.dataset.idempotencyKey = crypto.randomUUID();
    }
    submitButton.disabled = true;
    errorSummary.hidden = true;
    errorSummary.textContent = "";
    status.hidden = false;
    status.textContent = "Đang gửi yêu cầu đổi chế độ scorer.";
    try {
      const response = await fetch("/api/v1/learning-scorer/deployment", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": form.dataset.csrfToken,
          "Idempotency-Key": form.dataset.idempotencyKey,
        },
        body: JSON.stringify(body),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = payload.detail;
        const message = typeof detail === "string" ? detail : JSON.stringify(detail);
        throw new Error(message || "Máy chủ chưa thể xử lý yêu cầu scorer.");
      }
      status.textContent = "Máy chủ đã ghi nhận sự kiện đổi chế độ scorer.";
      window.location.reload();
    } catch (error) {
      errorSummary.hidden = false;
      errorSummary.textContent = error.message;
      errorSummary.focus();
      status.textContent = "Yêu cầu chưa được ghi nhận.";
      refreshSubmitState();
    }
  });
})();
