(() => {
  const root = document.getElementById("learning-registry");
  const registerForm = document.getElementById("learning-registry-register");
  const status = document.getElementById("learning-registry-status");
  if (!root || !registerForm || !status) return;

  const csrfToken = root.dataset.csrfToken;
  const pointer = {
    version: Number(root.dataset.pointerVersion),
    registryId: root.dataset.pointerId || null,
  };
  const apiRoot = "/api/v1/learning-models";

  const request = async (path, body) => {
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "Thao tác chưa thành công.");
    return payload;
  };

  registerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = new FormData(registerForm);
    try {
      await request(apiRoot, {
        artifact_id: values.get("artifact_id"),
        reason: values.get("reason"),
      });
      status.textContent = "Đã đăng ký artifact. Đang tải lại danh sách.";
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
    }
  });

  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const card = button.closest("[data-registry-id]");
      const reason = card?.querySelector("[data-action-reason]");
      if (!card || !reason || !reason.reportValidity()) return;
      status.textContent = "Đang gửi thao tác quản trị.";
      button.disabled = true;
      const registryId = card.dataset.registryId;
      const action = button.dataset.action;
      const body = {
        expected_revision: Number(card.dataset.revision),
        reason: reason.value.trim(),
      };
      if (action === "reject") body.reason_code = "manual_review_rejected";
      if (["activate", "rollback"].includes(action)) {
        body.expected_pointer_version = pointer.version;
        body.expected_current_registry_id = pointer.registryId;
      }
      try {
        await request(`${apiRoot}/${registryId}/${action}`, body);
        status.textContent = "Đã ghi nhận quyết định. Đang tải lại danh sách.";
        window.location.reload();
      } catch (error) {
        status.textContent = error.message;
        button.disabled = false;
      }
    });
  });
})();
