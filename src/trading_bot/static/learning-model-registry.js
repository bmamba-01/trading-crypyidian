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

  // Lỗi 422 của FastAPI trả `detail` dạng mảng; ghép thông điệp thay vì hiện "[object Object]".
  const describeError = (detail) => {
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail) && detail.length) {
      return `Dữ liệu chưa hợp lệ: ${detail.map((item) => item?.msg || String(item)).join("; ")}`;
    }
    return "Thao tác chưa thành công.";
  };

  // Giữ cùng Idempotency-Key khi gửi lại đúng nội dung (ví dụ sau lỗi mạng) để máy chủ không ghi trùng.
  const idempotencyKeys = new Map();
  const keyFor = (path, body) => {
    const fingerprint = `${path}|${JSON.stringify(body)}`;
    if (!idempotencyKeys.has(fingerprint)) idempotencyKeys.set(fingerprint, crypto.randomUUID());
    return idempotencyKeys.get(fingerprint);
  };

  const request = async (path, body) => {
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
        "Idempotency-Key": keyFor(path, body),
      },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(describeError(payload.detail));
    return payload;
  };

  // `minlength` của trình duyệt đếm cả khoảng trắng; máy chủ đếm sau khi cắt nên kiểm thêm ở đây.
  const validTrimmed = (field) => {
    const tooShort = field.value.trim().length < Number(field.getAttribute("minlength") || 0);
    field.setCustomValidity(tooShort ? "Lý do cần ít nhất 12 ký tự có nội dung." : "");
    return field.reportValidity();
  };
  document.querySelectorAll("#register-reason, [data-action-reason]").forEach((field) => {
    field.addEventListener("input", () => field.setCustomValidity(""));
  });

  registerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const reasonField = registerForm.elements.reason;
    if (reasonField && !validTrimmed(reasonField)) return;
    const values = new FormData(registerForm);
    try {
      await request(apiRoot, {
        artifact_id: values.get("artifact_id"),
        reason: String(values.get("reason") || "").trim(),
      });
      status.textContent = "Đã đăng ký artifact. Đang tải lại danh sách.";
      window.crypidianToast?.flash({
        tone: "success",
        title: "Đăng ký hoàn tất",
        message: "Đã lưu artifact vào sổ đăng ký.",
      });
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
      const errBox = document.getElementById("learning-registry-error");
      if (errBox) {
        errBox.textContent = error.message;
        errBox.hidden = false;
        errBox.focus();
      }
      window.crypidianToast?.show({
        tone: "error",
        title: "Không đăng ký được artifact",
        message: error.message,
        mirrored: Boolean(errBox),
      });
    }
  });

  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const card = button.closest("[data-registry-id]");
      const reason = card?.querySelector("[data-action-reason]");
      const errBox = document.getElementById("learning-registry-error");
      if (errBox) {
        errBox.hidden = true;
        errBox.textContent = "";
      }
      if (!card || !reason) return;
      if (!validTrimmed(reason)) {
        reason.setAttribute("aria-invalid", "true");
        reason.focus();
        window.crypidianToast?.show({
          tone: "warning",
          title: "Cần nhập lý do thao tác",
          message: "Hãy nhập lý do tối thiểu theo quy định trước khi xác nhận.",
          mirrored: Boolean(errBox),
        });
        return;
      }
      reason.removeAttribute("aria-invalid");
      status.textContent = "Đang gửi thao tác quản trị.";
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
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
        window.crypidianToast?.flash({
          tone: "success",
          title: "Đã cập nhật trạng thái mô hình",
          message: "Quyết định quản trị đã được ghi nhận.",
        });
        window.location.reload();
      } catch (error) {
        status.textContent = "Thao tác chưa hoàn tất.";
        if (errBox) {
          errBox.textContent = error.message;
          errBox.hidden = false;
          errBox.focus();
        }
        window.crypidianToast?.show({
          tone: "error",
          title: "Thao tác mô hình chưa hoàn tất",
          message: error.message,
          mirrored: Boolean(errBox),
        });
        button.disabled = false;
      } finally {
        button.removeAttribute("aria-busy");
      }
    });
  });
})();
