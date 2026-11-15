(() => {
  const root = document.getElementById("learning-training");
  const form = document.getElementById("learning-training-form");
  const jobsRoot = document.getElementById("learning-training-jobs");
  const status = document.getElementById("learning-training-status");
  if (!root || !jobsRoot || !status) return;

  const csrfToken = root.dataset.csrfToken;
  const apiRoot = "/api/v1/learning-training/jobs";
  const statusLabels = {
    queued: "Đang chờ",
    running: "Đang huấn luyện",
    succeeded: "Hoàn tất",
    failed: "Thất bại",
    cancelled: "Đã dừng",
    rejected: "Bị từ chối theo hạn mức",
  };
  const stageLabels = {
    queued: "Trong hàng đợi",
    training: "Tiến trình huấn luyện đang xử lý",
    complete: "Đã lưu mô hình",
    failed: "Đã dừng do lỗi",
    cancelled: "Đã xác nhận dừng",
  };

  const send = async (url, body) => {
    const response = await fetch(url, {
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

  // Dựng thẻ tác vụ giống hệt template learning_training.html (UI kit v1) để lần làm mới 5 giây
  // không đổi giao diện. Bảng tông màu/biểu tượng phải khớp với template.
  const statusTones = {
    queued: "neutral", running: "info", succeeded: "success",
    failed: "danger", cancelled: "neutral", rejected: "warning",
  };
  const statusIcons = {
    queued: "clock", running: "activity", succeeded: "check-circle",
    failed: "x-circle", cancelled: "pause", rejected: "ban",
  };
  const iconPaths = {
    hash: "M5 9h14M5 15h14M10 3 8 21M16 3l-2 18",
    refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
    branch: "M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM18 9a9 9 0 0 1-9 9",
    database: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3ZM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
    key: "M15 7a4 4 0 1 1-3.9 5H3v3h3v3h3v-3h2.1",
    alert: "M12 3 2 20h20L12 3ZM12 10v4M12 17h.01",
    layers: "M12 3 2 8l10 5 10-5-10-5ZM2 16l10 5 10-5M2 12l10 5 10-5",
    file: "M14 3H6v18h12V7l-4-4ZM14 3v4h4",
    cpu: "M7 7h10v10H7zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3",
    pause: "M8 6v12M16 6v12",
    link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
    clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM12 7v5l3 2",
    activity: "M3 12h4l3-8 4 16 3-8h4",
    "check-circle": "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM8 12l3 3 5-6",
    "x-circle": "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM9 9l6 6M15 9l-6 6",
    ban: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM5.6 5.6l12.8 12.8",
    info: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM12 11v5M12 8h.01",
    copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  };
  const svgNs = "http://www.w3.org/2000/svg";
  const icon = (name) => {
    const svg = document.createElementNS(svgNs, "svg");
    svg.setAttribute("class", "ico");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const path = document.createElementNS(svgNs, "path");
    path.setAttribute("d", iconPaths[name] || iconPaths.info);
    svg.append(path);
    return svg;
  };
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const chip = (label, tone, iconName, tag = "span") => {
    const node = el(tag, `lx-chip lx-chip--${tone}`);
    node.append(icon(iconName), el("span", "", label));
    return node;
  };
  const hashValue = (value, label) => {
    const text = String(value ?? "");
    const wrapper = el("span", "lx-hash");
    const code = el("code", "lx-hash__text", text);
    code.title = text;
    wrapper.append(code);
    if (text) {
      const copy = el("button", "copy-btn");
      copy.type = "button";
      copy.dataset.copy = text;
      copy.setAttribute("aria-label", `Sao chép ${label}`);
      copy.title = `Sao chép ${label} đầy đủ`;
      copy.append(icon("copy"));
      wrapper.append(copy);
    }
    return wrapper;
  };
  // kind: "text" (mặc định), "code" (mã ngắn), "hash" (mã đầy đủ + nút sao chép).
  const addField = (list, iconName, label, value, kind = "text", copyLabel = label.toLowerCase()) => {
    const row = el("div", "kv");
    const term = el("dt");
    if (iconName) term.append(icon(iconName));
    term.append(label);
    const description = el("dd");
    if (kind === "hash") {
      description.className = "kv-hash";
      description.append(hashValue(value, copyLabel));
    } else if (kind === "code") {
      description.append(el("code", "lx-mono", value));
    } else if (kind === "muted") {
      description.append(el("span", "value-muted", value));
    } else {
      description.textContent = value;
    }
    row.append(term, description);
    list.append(row);
  };
  const formatCount = (value) => {
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString("vi-VN") : String(value);
  };

  const updateKpis = (jobs) => {
    const set = (key, value) => {
      const node = document.querySelector(`[data-training-kpi="${key}"]`);
      if (node) node.textContent = String(value);
    };
    set("total", jobs.length);
    set("active", jobs.filter((job) => ["queued", "running"].includes(job.status)).length);
    set("succeeded", jobs.filter((job) => job.status === "succeeded").length);
  };

  const renderJobs = (jobs) => {
    const expandedLineages = new Set(
      Array.from(jobsRoot.querySelectorAll(".learning-training-artifact details[open]"),
        (details) => details.closest("[data-job-id]")?.dataset.jobId),
    );
    jobsRoot.replaceChildren();
    updateKpis(jobs);
    if (!jobs.length) {
      const empty = el("p", "lx-empty learning-training-empty");
      empty.append(icon("info"), "Chưa có tác vụ huấn luyện nào trong không gian làm việc này.");
      jobsRoot.append(empty);
      return;
    }
    jobs.forEach((job) => {
      const article = el("article", "glass-card lx-card learning-training-card");
      article.dataset.jobId = job.job_id;
      const statusLabel = statusLabels[job.status] || "Trạng thái chưa nhận diện";
      const header = el("header", "lx-card__head");
      const titleGroup = el("div", "lx-card__title");
      const meta = el("p", "lx-meta");
      meta.append(el("span", "", `${stageLabels[job.stage] || "Giai đoạn chưa nhận diện"} · ${job.progress}%`));
      titleGroup.append(el("h3", "", statusLabel), meta);
      const badge = el("span", `learning-training-state learning-training-state--${job.status}`);
      badge.append(chip(statusLabel, statusTones[job.status] || "neutral", statusIcons[job.status] || "info"));
      header.append(titleGroup, badge);
      const progress = el("progress", "lx-progress");
      progress.max = 100;
      progress.value = job.progress;
      progress.textContent = `${job.progress}%`;
      progress.setAttribute("aria-label", `Tiến độ ${statusLabel}`);
      const details = el("dl", "kv-grid kv-grid--1");
      addField(details, "hash", "Mã tác vụ", job.job_id, "hash", "mã tác vụ");
      addField(details, "refresh", "Mã lượt chạy", job.attempt_id, "hash", "mã lượt chạy");
      addField(details, "branch", "Bản kê / lát dữ liệu", `${job.manifest_hash.slice(0, 12)} / ${job.fold_id.slice(0, 12)}`, "code");
      addField(details, "database", "Hạn mức dữ liệu", `huấn luyện ${formatCount(job.training_samples)} · kiểm định ${formatCount(job.validation_samples)} · ${job.feature_count} đặc trưng`);
      addField(details, "key", "Lượt nhận / thế hệ khóa", `${job.attempt_count} / ${job.lease_generation}`);
      addField(details, "alert", "Mã lỗi", job.error_code || "—", job.error_code ? "code" : "muted");
      article.append(header, progress, details);
      if (job.status === "succeeded" && job.artifact_result) {
        const artifact = job.artifact_result;
        const result = el("section", "lx-artifact learning-training-artifact");
        result.setAttribute("aria-label", "Kết quả huấn luyện");
        const resultHead = el("div", "lx-card__head");
        const resultHeading = el("h4");
        resultHeading.append(icon("layers"), "Kết quả huấn luyện");
        const state = el("p", "learning-training-artifact-state");
        state.append(chip("ABSTAIN · Chưa hiệu chuẩn", "warning", "pause", "strong"));
        resultHead.append(resultHeading, state);
        const metadata = el("dl", "kv-grid kv-grid--1");
        addField(metadata, "file", "Mã artifact", artifact.artifact_id, "hash", "mã artifact");
        addField(metadata, "key", "Hash artifact", artifact.artifact_hash, "hash", "hash artifact");
        addField(metadata, "cpu", "Phiên bản huấn luyện", artifact.trainer_version);
        addField(metadata, "layers", "Phiên bản cấu trúc artifact", artifact.artifact_schema_version);
        const lineage = el("details", "glass-card lx-details");
        lineage.open = expandedLineages.has(job.job_id);
        const summary = el("summary", "", "Nguồn gốc dữ liệu và lượt chạy");
        const lineageMetadata = el("dl", "kv-grid kv-grid--1");
        addField(lineageMetadata, null, "Mã bộ dữ liệu chuẩn hóa", job.normalized_dataset_id, "hash", "mã bộ dữ liệu");
        addField(lineageMetadata, null, "Hash bản kê", job.manifest_hash, "hash");
        addField(lineageMetadata, null, "Hash kế hoạch phân chia", job.fold_plan_hash, "hash");
        addField(lineageMetadata, null, "Hash lát dữ liệu", job.fold_id, "hash");
        addField(lineageMetadata, null, "Hash phê duyệt", artifact.approval_hash, "hash");
        addField(lineageMetadata, null, "Hash lô huấn luyện", artifact.batch_hash, "hash");
        addField(lineageMetadata, null, "Hash cấu trúc đặc trưng", artifact.feature_schema_hash, "hash");
        addField(lineageMetadata, null, "Hash nội dung lượt chạy", artifact.attempt_content_hash, "hash");
        lineage.append(summary, lineageMetadata);
        const notice = el("p", "lx-help", "Biểu mẫu chỉ điền sẵn mã artifact; đăng ký, phê duyệt và kích hoạt cần thao tác riêng của người có quyền.");
        const handoff = el("a", "lx-link");
        handoff.href = `/admin/learning/models?artifact_id=${encodeURIComponent(artifact.artifact_id)}`;
        handoff.append(icon("link"), "Mở biểu mẫu đăng ký artifact");
        result.append(resultHead, metadata, lineage, notice, handoff);
        article.append(result);
      }
      const active = ["queued", "running"].includes(job.status);
      if (active && !job.cancel_requested) {
        const actions = el("div", "lx-form__actions");
        const cancel = el("button", "btn btn-secondary");
        cancel.type = "button";
        cancel.dataset.cancelJob = job.job_id;
        cancel.append(icon("pause"), "Yêu cầu dừng");
        actions.append(cancel);
        article.append(actions);
      } else if (active && job.cancel_requested) {
        const notice = el("p", "lx-status learning-training-cancel-requested");
        notice.append(icon("clock"), "Đã gửi yêu cầu dừng; chờ tiến trình huấn luyện xác nhận.");
        article.append(notice);
      }
      jobsRoot.append(article);
    });
  };

  const refresh = async () => {
    const response = await fetch(apiRoot, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error("Không cập nhật được trạng thái tác vụ huấn luyện.");
    const snapshot = await response.json();
    renderJobs(snapshot.jobs);
    status.textContent = `${snapshot.jobs.length} tác vụ trong không gian làm việc · vừa cập nhật.`;
  };

  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const select = form.querySelector("select");
      const option = select?.selectedOptions[0];
      if (!option?.dataset.datasetId || !option.dataset.foldId) return;
      const submit = form.querySelector("button[type=submit]");
      submit.disabled = true;
      status.textContent = "Đang đưa tác vụ vào hàng đợi.";
      try {
        const result = await send(apiRoot, {
          normalized_dataset_id: option.dataset.datasetId,
          fold_id: option.dataset.foldId,
        });
        status.textContent = result.status === "rejected"
          ? "Yêu cầu đã được ghi nhận nhưng vượt hạn mức máy chủ."
          : "Đã đưa tác vụ huấn luyện vào hàng đợi.";
        await refresh();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        submit.disabled = false;
      }
    });
  }

  jobsRoot.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cancel-job]");
    if (!button) return;
    button.disabled = true;
    status.textContent = "Đang gửi yêu cầu dừng.";
    try {
      await send(`${apiRoot}/${button.dataset.cancelJob}/cancel`, {});
      status.textContent = "Đã gửi yêu cầu dừng cho tiến trình huấn luyện.";
      await refresh();
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });

  window.setInterval(() => {
    if (!document.hidden) refresh().catch((error) => { status.textContent = error.message; });
  }, 5000);
})();
