(() => {
  const form = document.getElementById("paper-performance-assess-form");
  if (!form) return;
  const button = document.getElementById("paper-performance-assess");
  const status = document.getElementById("paper-performance-form-status");
  const errorBox = document.getElementById("paper-performance-form-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    if (errorBox) {
      errorBox.hidden = true;
      errorBox.textContent = "";
    }
    status.textContent = "Đang kiểm tra quyền hiện hành và tính các chỉ số…";
    try {
      const response = await fetch(form.dataset.endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": form.dataset.csrfToken,
          "Idempotency-Key": crypto.randomUUID()
        },
        body: JSON.stringify({
          bot_id: document.getElementById("paper-performance-bot").value
        })
      });
      if (!response.ok) {
        throw new Error(response.status === 409
          ? "Target hoặc quyền dữ liệu đã đổi, hoặc chưa đủ điều kiện. Hãy tải lại và thử lại."
          : "Chưa thể tạo snapshot. Hãy thử lại sau.");
      }
      status.textContent = "Đã lưu snapshot chẩn đoán.";
      window.crypidianToast?.flash({
        tone: "success",
        title: "Snapshot chẩn đoán đã sẵn sàng",
        message: "Các chỉ số hiệu năng PAPER vừa được cập nhật.",
      });
      window.location.reload();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Yêu cầu chưa hoàn tất.";
      status.textContent = "Yêu cầu chưa hoàn tất.";
      if (errorBox) {
        errorBox.textContent = message;
        errorBox.hidden = false;
        errorBox.focus();
      }
      window.crypidianToast?.show({
        tone: "error",
        title: "Chưa tạo được snapshot",
        message,
        mirrored: Boolean(errorBox),
      });
      button.disabled = false;
    } finally {
      button.removeAttribute("aria-busy");
    }
  });
})();
// Thanh độ phủ (`data-fill-percent`) do /static/pages/learning-ui.js đặt độ rộng qua CSSOM.
