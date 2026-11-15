// Hành vi dùng chung cho nhóm trang Học máy và Nguồn dữ liệu (UI kit v1).
// - Nút sao chép `.copy-btn[data-copy]`: chép mã đầy đủ, báo bằng toast dùng chung.
// - Thanh tỷ lệ `[data-fill-percent]`: đặt độ rộng qua CSSOM vì CSP bỏ qua style nội tuyến.
// Ủy quyền sự kiện ở `document` để cả phần tử do JS của trang dựng lại (ví dụ thẻ huấn luyện
// làm mới mỗi 5 giây) vẫn sao chép được.
(() => {
  "use strict";

  const notify = (tone, title, message) => {
    if (window.crypidianToast) {
      window.crypidianToast.show({ tone, title, message, timeout: 2500 });
    }
  };

  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".copy-btn[data-copy]");
    if (!button) return;
    event.preventDefault();
    const value = button.dataset.copy || "";
    try {
      await navigator.clipboard.writeText(value);
      notify("success", "Đã sao chép", value);
    } catch {
      notify("warning", "Chưa sao chép được", "Trình duyệt chặn bộ nhớ tạm; hãy chọn và sao chép thủ công.");
    }
  });

  const fill = (root) => {
    for (const bar of root.querySelectorAll("[data-fill-percent]")) {
      const percent = Number.parseFloat(bar.dataset.fillPercent);
      bar.style.setProperty("width", `${Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : 0}%`);
    }
  };
  fill(document);
  window.crypidianLearningUi = { fill };
})();
