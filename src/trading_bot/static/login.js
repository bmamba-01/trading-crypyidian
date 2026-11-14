// Trang đăng nhập: ghi nhớ trường không bí mật, hiện/ẩn mật khẩu, phản hồi khi gửi form.
//
// Chỉ mã không gian làm việc và email được lưu vào localStorage của trình duyệt, và chỉ khi
// người dùng tick "Ghi nhớ". Mật khẩu và mã xác thực không bao giờ được script lưu; việc lưu
// mật khẩu do trình quản lý mật khẩu của trình duyệt đảm nhiệm nhờ autocomplete chuẩn.
(() => {
  "use strict";

  const STORAGE_KEY = "crypidian.login.remembered";
  const REMEMBERED_FIELDS = ["workspace-slug", "email"];

  const form = document.getElementById("login-form");
  if (!form) return;
  const remember = document.getElementById("remember-login");
  const submit = document.getElementById("login-submit");

  const readStore = () => {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === "object" ? parsed : null;
    } catch {
      return null;
    }
  };

  const writeStore = (value) => {
    try {
      if (value) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
      else window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Trình duyệt chặn bộ nhớ (chế độ riêng tư): bỏ qua, form vẫn hoạt động bình thường.
    }
  };

  // Điền lại giá trị đã nhớ, không ghi đè thứ trình duyệt hoặc người dùng đã điền.
  const stored = readStore();
  if (stored) {
    for (const id of REMEMBERED_FIELDS) {
      const input = document.getElementById(id);
      const value = stored[id];
      if (input && !input.value && typeof value === "string" && value.length <= 320) {
        input.value = value;
      }
    }
    if (remember) remember.checked = true;
  }

  // Đưa con trỏ tới ô đầu tiên còn trống để nhập nhanh.
  const firstEmpty = ["workspace-slug", "email", "password", "totp-code"]
    .map((id) => document.getElementById(id))
    .find((input) => input && !input.value);
  // Chỉ tự focus trên màn hình rộng: trên di động, focus làm trang cuộn qua phần giới thiệu
  // và bật bàn phím ảo ngay khi mở trang.
  const wide = window.matchMedia && window.matchMedia("(min-width: 900px)").matches;
  if (wide && firstEmpty && !document.querySelector(".error-summary")) {
    firstEmpty.focus({ preventScroll: true });
  }

  const toggle = document.getElementById("password-toggle");
  const password = document.getElementById("password");
  if (toggle && password) {
    toggle.addEventListener("click", () => {
      const showing = password.type === "text";
      password.type = showing ? "password" : "text";
      toggle.setAttribute("aria-pressed", showing ? "false" : "true");
      toggle.textContent = showing ? "Hiện" : "Ẩn";
      password.focus();
    });
  }

  // Mã xác thực: chỉ giữ chữ số, kể cả khi dán "123 456".
  const code = document.getElementById("totp-code");
  if (code) {
    code.addEventListener("input", () => {
      const digits = code.value.replace(/\D/g, "").slice(0, 6);
      if (digits !== code.value) code.value = digits;
    });
  }

  const toast = (options) => window.crypidianToast && window.crypidianToast.show(options);
  const SUBMIT_LABEL = submit ? submit.textContent : "Đăng nhập";
  const RESPONSE_TIMEOUT_MS = 15000;
  let responseTimer = 0;

  const resetSubmit = () => {
    window.clearTimeout(responseTimer);
    if (submit) {
      submit.disabled = false;
      submit.textContent = SUBMIT_LABEL;
    }
  };

  // Trang khôi phục từ bộ nhớ đệm khi bấm Back: nút có thể còn ở trạng thái "Đang đăng nhập…"
  // và không bấm được nữa. Luôn mở lại nút khi trang hiện ra.
  window.addEventListener("pageshow", resetSubmit);

  const labelOf = (input) => {
    const label = input.id ? document.querySelector(`label[for="${input.id}"]`) : null;
    return label ? label.textContent.trim() : input.name;
  };

  // Trường không hợp lệ: ngoài bong bóng của trình duyệt, hiện toast nói rõ trường nào và vì sao.
  let invalidReported = false;
  form.addEventListener(
    "invalid",
    (event) => {
      if (invalidReported) return;
      invalidReported = true;
      window.setTimeout(() => {
        invalidReported = false;
      }, 0);
      const input = event.target;
      let message = input.validationMessage;
      if (input.id === "totp-code") message = "Nhập đủ 6 chữ số từ ứng dụng xác thực.";
      else if (input.validity.valueMissing) message = "Trường này không được để trống.";
      else if (input.validity.typeMismatch && input.type === "email")
        message = "Email chưa đúng định dạng, ví dụ ten@congty.vn.";
      toast({ tone: "error", title: `Chưa thể đăng nhập: ${labelOf(input)}`, message });
    },
    true,
  );

  form.addEventListener("submit", (event) => {
    if (!form.checkValidity()) return;
    if (remember && remember.checked) {
      const snapshot = {};
      for (const id of REMEMBERED_FIELDS) {
        const input = document.getElementById(id);
        if (input) snapshot[id] = input.value.trim();
      }
      writeStore(snapshot);
    } else {
      writeStore(null);
    }
    if (password && password.type === "text") password.type = "password";
    if (submit) {
      // Không disable ngay trong cùng tick để trình duyệt vẫn gửi form bình thường.
      window.setTimeout(() => {
        submit.disabled = true;
        submit.textContent = "Đang đăng nhập…";
      }, 0);
    }
    // Không có phản hồi (mất kết nối, máy chủ dừng): báo lỗi và cho gửi lại thay vì im lặng.
    responseTimer = window.setTimeout(() => {
      resetSubmit();
      toast({
        tone: "warning",
        title: "Máy chủ chưa phản hồi",
        message:
          "Kiểm tra admin-web còn chạy và kết nối mạng, rồi bấm Đăng nhập lại. " +
          "Nếu đã quá vài phút, tải lại trang trước khi gửi.",
      });
    }, RESPONSE_TIMEOUT_MS);
    return event;
  });
})();
