// Tải bằng chứng license và thao tác nguồn nghiên cứu trên trang quản trị.
(() => {
  const form = document.getElementById("license-evidence-upload");
  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const fileInput = form.querySelector('input[type="file"]');
      const status = form.querySelector("[role='status']");
      const errorBox = document.getElementById("license-evidence-error");
      const button = form.querySelector("button[type='submit']");
      if (errorBox) {
        errorBox.hidden = true;
        errorBox.textContent = "";
      }
      fileInput.removeAttribute("aria-invalid");
      const file = fileInput.files[0];
      if (!file || file.size === 0 || file.size > 10 * 1024 * 1024) {
        const message = "Chọn tài liệu không rỗng, tối đa 10 MiB.";
        fileInput.setAttribute("aria-invalid", "true");
        status.textContent = message;
        if (errorBox) {
          errorBox.textContent = message;
          errorBox.hidden = false;
        }
        window.crypidianToast?.show({
          tone: "warning",
          title: "Tệp không hợp lệ",
          message,
          mirrored: Boolean(errorBox),
        });
        return;
      }
      if (!["application/pdf", "text/plain", "text/html", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"].includes(file.type)) {
        const message = "Định dạng không được hỗ trợ. Hãy chọn PDF, TXT, HTML hoặc DOCX.";
        fileInput.setAttribute("aria-invalid", "true");
        status.textContent = message;
        if (errorBox) {
          errorBox.textContent = message;
          errorBox.hidden = false;
        }
        window.crypidianToast?.show({
          tone: "warning",
          title: "Định dạng không hỗ trợ",
          message,
          mirrored: Boolean(errorBox),
        });
        return;
      }
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
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
        window.crypidianToast?.flash({
          tone: "success",
          title: "Đã tính SHA-256 tài liệu",
          message: "Chỉ lưu mã băm xác nhận, không lưu nội dung tệp.",
        });
        window.location.reload();
      } catch (error) {
        const message = error instanceof Error ? error.message : "Upload thất bại.";
        status.textContent = "Upload chưa hoàn tất.";
        if (errorBox) {
          errorBox.textContent = message;
          errorBox.hidden = false;
          errorBox.focus();
        }
        window.crypidianToast?.show({
          tone: "error",
          title: "Tải bằng chứng thất bại",
          message,
          mirrored: Boolean(errorBox),
        });
        button.disabled = false;
      } finally {
        button.removeAttribute("aria-busy");
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
        window.crypidianToast?.show({
          tone: "success",
          title: "Đã sao chép SHA-256",
          message: "Mã băm đầy đủ đã được lưu vào bộ nhớ tạm.",
          timeout: 2500,
        });
      } catch {
        if (status) status.textContent = "Trình duyệt không cho phép sao chép. Hãy chọn và sao chép mã.";
        window.crypidianToast?.show({
          tone: "warning",
          title: "Không sao chép được",
          message: "Trình duyệt chặn bộ nhớ tạm; hãy chọn và sao chép mã.",
        });
      }
    });
  });
})();
