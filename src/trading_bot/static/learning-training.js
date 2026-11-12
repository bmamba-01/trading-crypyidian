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

  const addField = (list, label, value, code = false) => {
    const container = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    if (code) {
      const codeElement = document.createElement("code");
      codeElement.textContent = value;
      description.append(codeElement);
    } else {
      description.textContent = value;
    }
    container.append(term, description);
    list.append(container);
  };

  const renderJobs = (jobs) => {
    const expandedLineages = new Set(
      Array.from(jobsRoot.querySelectorAll(".learning-training-artifact details[open]"),
        (details) => details.closest("[data-job-id]")?.dataset.jobId),
    );
    jobsRoot.replaceChildren();
    if (!jobs.length) {
      const empty = document.createElement("p");
      empty.className = "learning-training-empty";
      empty.textContent = "Chưa có tác vụ huấn luyện nào trong không gian làm việc này.";
      jobsRoot.append(empty);
      return;
    }
    jobs.forEach((job) => {
      const article = document.createElement("article");
      article.className = "learning-training-card";
      article.dataset.jobId = job.job_id;
      const header = document.createElement("header");
      const titleGroup = document.createElement("div");
      const title = document.createElement("h3");
      const stage = document.createElement("p");
      const badge = document.createElement("span");
      title.textContent = statusLabels[job.status] || "Trạng thái chưa nhận diện";
      stage.textContent = `${stageLabels[job.stage] || "Giai đoạn chưa nhận diện"} · ${job.progress}%`;
      badge.className = `learning-training-state learning-training-state--${job.status}`;
      badge.textContent = title.textContent;
      titleGroup.append(title, stage);
      header.append(titleGroup, badge);
      const progress = document.createElement("progress");
      progress.max = 100;
      progress.value = job.progress;
      progress.textContent = `${job.progress}%`;
      progress.setAttribute("aria-label", `Tiến độ ${title.textContent}`);
      const details = document.createElement("dl");
      addField(details, "Mã tác vụ", job.job_id);
      addField(details, "Mã lượt chạy", job.attempt_id);
      addField(details, "Bản kê / lát dữ liệu", `${job.manifest_hash.slice(0, 12)} / ${job.fold_id.slice(0, 12)}`, true);
      addField(details, "Hạn mức dữ liệu", `huấn luyện ${job.training_samples} · kiểm định ${job.validation_samples} · ${job.feature_count} đặc trưng`);
      addField(details, "Lượt nhận / thế hệ khóa", `${job.attempt_count} / ${job.lease_generation}`);
      addField(details, "Mã lỗi", job.error_code || "—", Boolean(job.error_code));
      article.append(header, progress, details);
      if (job.status === "succeeded" && job.artifact_result) {
        const artifact = job.artifact_result;
        const result = document.createElement("section");
        result.className = "learning-training-artifact";
        result.setAttribute("aria-label", "Kết quả huấn luyện");
        const resultHeading = document.createElement("h4");
        resultHeading.textContent = "Kết quả huấn luyện";
        const state = document.createElement("p");
        state.className = "learning-training-artifact-state";
        const stateLabel = document.createElement("strong");
        stateLabel.textContent = "ABSTAIN · Chưa hiệu chuẩn";
        state.append(stateLabel);
        const metadata = document.createElement("dl");
        addField(metadata, "Mã artifact", artifact.artifact_id);
        addField(metadata, "Hash artifact", artifact.artifact_hash, true);
        addField(metadata, "Phiên bản huấn luyện", artifact.trainer_version);
        addField(metadata, "Phiên bản cấu trúc artifact", artifact.artifact_schema_version);
        const lineage = document.createElement("details");
        lineage.open = expandedLineages.has(job.job_id);
        const summary = document.createElement("summary");
        summary.textContent = "Nguồn gốc dữ liệu và lượt chạy";
        const lineageMetadata = document.createElement("dl");
        addField(lineageMetadata, "Mã bộ dữ liệu chuẩn hóa", job.normalized_dataset_id);
        addField(lineageMetadata, "Hash bản kê", job.manifest_hash, true);
        addField(lineageMetadata, "Hash kế hoạch phân chia", job.fold_plan_hash, true);
        addField(lineageMetadata, "Hash lát dữ liệu", job.fold_id, true);
        addField(lineageMetadata, "Hash phê duyệt", artifact.approval_hash, true);
        addField(lineageMetadata, "Hash lô huấn luyện", artifact.batch_hash, true);
        addField(lineageMetadata, "Hash cấu trúc đặc trưng", artifact.feature_schema_hash, true);
        addField(lineageMetadata, "Hash nội dung lượt chạy", artifact.attempt_content_hash, true);
        lineage.append(summary, lineageMetadata);
        const notice = document.createElement("p");
        notice.textContent = "Biểu mẫu chỉ điền sẵn mã artifact; đăng ký, phê duyệt và kích hoạt cần thao tác riêng của người có quyền.";
        const handoff = document.createElement("a");
        handoff.href = `/admin/learning/models?artifact_id=${encodeURIComponent(artifact.artifact_id)}`;
        handoff.textContent = "Mở biểu mẫu đăng ký artifact";
        result.append(resultHeading, state, metadata, lineage, notice, handoff);
        article.append(result);
      }
      if (["queued", "running"].includes(job.status) && !job.cancel_requested) {
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "button-secondary";
        cancel.dataset.cancelJob = job.job_id;
        cancel.textContent = "Yêu cầu dừng";
        article.append(cancel);
      } else if (job.cancel_requested) {
        const notice = document.createElement("p");
        notice.className = "learning-training-cancel-requested";
        notice.textContent = "Đã gửi yêu cầu dừng; chờ tiến trình huấn luyện xác nhận.";
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
