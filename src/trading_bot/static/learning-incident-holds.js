(() => {
  const panel = document.querySelector("[data-learning-incident-panel]");
  if (!panel) return;

  const csrfToken = panel.dataset.csrfToken;
  const status = document.getElementById("learning-incident-status");
  const error = document.getElementById("learning-incident-error");
  const reauthStatus = document.getElementById("learning-incident-reauth-status");
  const reauthForm = document.querySelector("[data-reauth-form]");
  const setFreshMfa = () => {
    panel.dataset.recentTotp = "true";
    panel.querySelectorAll("[data-incident-submit]").forEach((button) => {
      button.disabled = false;
    });
  };

  if (panel.dataset.recentTotp === "true") setFreshMfa();

  const responseMessage = (payload, fallback) => {
    const detail = payload && payload.detail;
    return typeof detail === "string" ? detail : fallback;
  };

  reauthForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!reauthForm.reportValidity()) return;
    const data = new FormData(reauthForm);
    const submit = reauthForm.querySelector("button[type='submit']");
    submit.disabled = true;
    reauthStatus.textContent = "Đang xác thực mật khẩu và mã bảo mật.";
    error.hidden = true;
    try {
      const response = await fetch("/api/v1/auth/reauthenticate", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ password: data.get("password"), code: data.get("code") }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(responseMessage(payload, "Chưa xác thực được mã bảo mật. Hãy kiểm tra lại."));
      }
      setFreshMfa();
      reauthStatus.textContent = "Đã xác thực. Có thể xác nhận thao tác chặn hoặc tháo chặn trong 15 phút.";
      reauthForm.reset();
    } catch (requestError) {
      error.textContent = requestError.message;
      error.hidden = false;
      error.focus();
      reauthStatus.textContent = "Yêu cầu xác thực chưa thành công.";
    } finally {
      submit.disabled = false;
    }
  });

  panel.querySelectorAll("[data-incident-submit]").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = button.closest("[data-incident-action-form]");
      const reasonField = form.querySelector("textarea[name='reason']");
      const fieldError = form.querySelector("[data-reason-error]");
      const reason = reasonField.value.trim();
      if (!form.reportValidity() || !reason) {
        fieldError.hidden = false;
        reasonField.setAttribute("aria-invalid", "true");
        reasonField.focus();
        return;
      }
      fieldError.hidden = true;
      reasonField.removeAttribute("aria-invalid");
      const proposalId = form.dataset.proposalId;
      const action = form.dataset.action;
      const path = `/api/v1/learning/incident-entry-holds/${proposalId}/${action}`;
      const body = action === "hold"
        ? {
          assessment_id: form.dataset.assessmentId,
          assessment_hash: form.dataset.assessmentHash,
          expected_revision: Number(form.dataset.expectedRevision),
          expected_deployment_revision: Number(form.dataset.deploymentRevision),
          expected_active_pointer_version: Number(form.dataset.pointerVersion),
          reason,
        }
        : { expected_revision: Number(form.dataset.expectedRevision), reason };
      const fingerprint = JSON.stringify(body);
      if (form.dataset.idempotencyFingerprint !== fingerprint) {
        form.dataset.idempotencyFingerprint = fingerprint;
        form.dataset.idempotencyKey = crypto.randomUUID();
      }
      button.disabled = true;
      error.hidden = true;
      status.hidden = false;
      status.textContent = action === "hold" ? "Đang xác nhận chặn lệnh vào." : "Đang tháo chặn lệnh vào.";
      try {
        const response = await fetch(path, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
            "Idempotency-Key": form.dataset.idempotencyKey,
          },
          body: JSON.stringify(body),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(responseMessage(payload, "Máy chủ chưa thể xác nhận thao tác sự cố."));
        }
        status.textContent = action === "hold"
          ? "Đã ghi nhận thao tác chặn. Lệnh vào mới sẽ bị ngăn sau khi giao dịch hiện tại hoàn tất."
          : "Đã ghi nhận thao tác tháo chặn. Trạng thái lệnh vào đã được cập nhật.";
        window.setTimeout(() => window.location.reload(), 900);
      } catch (requestError) {
        error.textContent = requestError.message;
        error.hidden = false;
        error.focus();
        status.textContent = "Thao tác chưa được ghi nhận.";
        button.disabled = panel.dataset.recentTotp !== "true";
      }
    });
  });

  panel.querySelectorAll("textarea[name='reason']").forEach((field) => {
    field.addEventListener("input", () => {
      if (!field.value.trim()) return;
      field.closest("[data-incident-action-form]")
        .querySelector("[data-reason-error]").hidden = true;
      field.removeAttribute("aria-invalid");
    });
  });
})();
