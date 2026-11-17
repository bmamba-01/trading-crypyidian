// Bảo vệ phiên phía trình duyệt: khi một fetch cùng origin nhận 401 (phiên hết hạn hoặc không
// còn hợp lệ), chuyển trang về đăng nhập Admin kèm `next` là trang hiện tại để quay lại sau.
//
// Nạp đồng bộ trong <head> (trước mọi script defer) để bọc `window.fetch` trước khi các script
// trang bắt đầu gọi API (ví dụ snapshot realtime mỗi 5 giây). Server vẫn trả 401 JSON cho API;
// script này chỉ quyết định điều hướng. Không đụng tới 401 của chính luồng xác thực
// (`/api/v1/auth/...`, ví dụ nhập sai mật khẩu khi xác thực lại) vì đó không phải hết phiên.
(() => {
  "use strict";

  const LOGIN_PATH = "/admin/login";
  const AUTH_API_PREFIX = "/api/v1/auth/";
  let redirecting = false;
  // Trang đang rời đi (người dùng/trình duyệt đã bắt đầu điều hướng khác): không chen ngang,
  // nếu không lệnh chuyển về đăng nhập sẽ hủy điều hướng người dùng vừa chọn.
  let leaving = false;
  window.addEventListener("beforeunload", () => { leaving = true; });
  window.addEventListener("pagehide", () => { leaving = true; });
  window.addEventListener("pageshow", () => { leaving = false; redirecting = false; });

  const isLoginPage = () => window.location.pathname === LOGIN_PATH;

  // Chỉ trang trong cây /admin (không phải trang đăng nhập) mới được dùng làm `next`.
  const currentNext = () => {
    const { pathname, search } = window.location;
    if (pathname !== "/admin" && !pathname.startsWith("/admin/")) return null;
    if (pathname === LOGIN_PATH) return null;
    return pathname + search;
  };

  const loginUrl = () => {
    const next = currentNext();
    return next ? `${LOGIN_PATH}?next=${encodeURIComponent(next)}` : LOGIN_PATH;
  };

  const redirectToLogin = () => {
    if (redirecting || leaving || isLoginPage()) return;
    redirecting = true;
    window.location.assign(loginUrl());
  };

  // 401 đáng chuyển trang: request cùng origin, ngoài API xác thực.
  const isSessionExpiry = (input, response) => {
    if (!response || response.status !== 401) return false;
    let url;
    try {
      const raw = typeof input === "string" || input instanceof URL ? input : input && input.url;
      url = new URL(String(raw || response.url || ""), window.location.href);
    } catch {
      return false;
    }
    if (url.origin !== window.location.origin) return false;
    return !url.pathname.startsWith(AUTH_API_PREFIX);
  };

  window.CrypidianSession = Object.freeze({ loginUrl, redirectToLogin });

  if (typeof window.fetch !== "function") return;
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const response = await originalFetch(input, init);
    if (isSessionExpiry(input, response)) redirectToLogin();
    return response;
  };
})();
