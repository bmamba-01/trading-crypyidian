(() => {
  const form = document.getElementById("paper-performance-assess-form");
  if (!form) return;
  const button = document.getElementById("paper-performance-assess");
  const status = document.getElementById("paper-performance-form-status");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
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
      window.location.reload();
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "Yêu cầu chưa hoàn tất.";
      button.disabled = false;
    }
  });
})();
// Thanh độ phủ (`data-fill-percent`) do /static/pages/learning-ui.js đặt độ rộng qua CSSOM.
