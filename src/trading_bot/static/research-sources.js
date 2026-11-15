// Tải bằng chứng license và thao tác nguồn nghiên cứu trên trang quản trị.
(() => {
  const form = document.getElementById("license-evidence-upload");
  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const fileInput = form.querySelector('input[type="file"]');
      const status = form.querySelector("[role='status']");
      const button = form.querySelector("button[type='submit']");
      const file = fileInput.files[0];
      if (!file || file.size === 0 || file.size > 10 * 1024 * 1024) {
        status.textContent = "Chọn tài liệu không rỗng, tối đa 10 MiB.";
        return;
      }
      if (!["application/pdf", "text/plain", "text/html", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"].includes(file.type)) {
        status.textContent = "Định dạng không được hỗ trợ. Hãy chọn PDF, TXT, HTML hoặc DOCX.";
        return;
      }
      button.disabled = true;
      status.textContent = "Đang gửi tài liệu để tính digest…";
      try {
        const response = await fetch(form.dataset.endpoint, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": file.type,
            "X-CSRF-Token": form.dataset.csrf,
            "Idempotency-Key": crypto.randomUUID()
          },
          body: await file.arrayBuffer()
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(payload.detail || "Không thể ghi digest.");
        }
        const result = await response.json();
        status.textContent = `Đã ghi SHA-256 ${result.digest}. Nội dung tài liệu không được lưu.`;
        fileInput.value = "";
        window.location.reload();
      } catch (error) {
        status.textContent = error instanceof Error ? error.message : "Upload thất bại.";
        button.disabled = false;
      }
    });
  }
  document.querySelectorAll("[data-copy-digest]").forEach((button) => {
    button.addEventListener("click", async () => {
      const source = document.getElementById(button.dataset.copyDigest);
      const status = document.getElementById("license-evidence-status");
      try {
        await navigator.clipboard.writeText(source.textContent.trim());
        if (status) status.textContent = "Đã sao chép SHA-256 đầy đủ.";
      } catch {
        if (status) status.textContent = "Trình duyệt không cho phép sao chép. Hãy chọn và sao chép mã.";
      }
    });
  });
})();
