const byId = (id) => document.getElementById(id);

const authPill = byId("authPill");
const sendOtp = byId("sendOtp");
const confirmOtp = byId("confirmOtp");
const otpArea = byId("otpArea");
const authMessage = byId("authMessage");
const authDialog = byId("authDialog");

authPill.addEventListener("click", () => authDialog.showModal());
byId("closeAuth").addEventListener("click", () => authDialog.close());
authDialog.addEventListener("click", (event) => {
  if (event.target === authDialog) authDialog.close();
});
const promptEl = byId("prompt");
const imagesEl = byId("images");
const dropzone = byId("dropzone");
const imageList = byId("imageList");
const generateBtn = byId("generate");
const generateMessage = byId("generateMessage");
const jobsEl = byId("jobs");
const activeJob = byId("activeJob");
const sendChatBtn = byId("sendChat");

let selectedFiles = [];
let pollTimer = null;

const templates = {
  product:
    "Create a realistic 10-second vertical 9:16 product advertisement video. " +
    "Keep the main product clearly visible, use natural camera movement, realistic hand motion, " +
    "clean commercial lighting, smooth transitions, sharp details, no subtitles, no text overlay, no watermark.",
  cinematic:
    "Create a cinematic 10-second vertical 9:16 video with realistic motion, atmospheric lighting, " +
    "shallow depth of field, smooth camera movement, natural physics, high detail, no subtitles, no text overlay, no watermark.",
  social:
    "Create a realistic 10-second vertical 9:16 social media video. Strong visual hook in the first second, " +
    "dynamic but natural camera movement, smooth transitions, clear subject focus, polished commercial quality, " +
    "no subtitles, no watermark."
};

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let data;
  try {
    data = await response.json();
  } catch {
    data = { detail: await response.text() };
  }
  if (!response.ok) {
    throw new Error(data.detail || data.message || "HTTP " + response.status);
  }
  return data;
}

function setMessage(el, text, type = "") {
  el.textContent = text || "";
  el.className = "message " + type;
}

async function refreshAuth() {
  try {
    const state = await api("/api/auth/status");
    authPill.className = "pill auth-trigger " + (state.authenticated ? "ok" : "bad");
    authPill.querySelector("span:last-child").textContent =
      state.authenticated
        ? (state.ready_accounts || 0) + " TK sẵn sàng"
        : "Quản lý tài khoản";
    byId("authDialogStatus").textContent =
      (state.ready_accounts || 0) + " tài khoản sẵn sàng";
    generateBtn.disabled = !state.authenticated;
    sendChatBtn.disabled = !state.authenticated;
  } catch {
    authPill.className = "pill auth-trigger bad";
    authPill.querySelector("span:last-child").textContent = "Mất kết nối";
    byId("authDialogStatus").textContent = "Không thể kiểm tra trạng thái";
    generateBtn.disabled = true;
    sendChatBtn.disabled = true;
  }
}

sendOtp.addEventListener("click", async () => {
  const emails = [...new Set(byId("email").value
    .split(/[\s,;]+/)
    .map(value => value.trim())
    .filter(Boolean))];
  if (!emails.length || emails.length > 10 ||
      emails.some(value => !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value))) {
    setMessage(authMessage, "Nhập 1–10 email hợp lệ, cách nhau bằng dấu phẩy hoặc xuống dòng.", "error");
    return;
  }
  sendOtp.disabled = true;
  setMessage(authMessage, "Đang gửi OTP cho " + emails.length + " tài khoản...");
  try {
    const responses = await Promise.allSettled(emails.map(email =>
      api("/api/accounts", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({email, region: "VN"})
      })
    ));
    const successes = responses.filter(result => result.status === "fulfilled");
    const failures = responses.filter(result => result.status === "rejected");
    await window.refreshMuseAccounts?.();
    if (successes.length) {
      byId("otpAccountSelect").value = successes[0].value.id;
      otpArea.classList.remove("hidden");
      byId("otp").focus();
    }
    setMessage(
      authMessage,
      "Đã gửi OTP: " + successes.length + "/" + emails.length + " tài khoản." +
      (failures.length ? " Lỗi: " + failures.map(result => result.reason.message).join("; ") : ""),
      failures.length ? "error" : "success"
    );
  } catch (err) {
    setMessage(authMessage, err.message, "error");
  } finally {
    sendOtp.disabled = false;
  }
});

confirmOtp.addEventListener("click", async () => {
  const otp = byId("otp").value.trim();
  if (!otp) {
    setMessage(authMessage, "Nhập OTP.", "error");
    return;
  }
  confirmOtp.disabled = true;
  try {
    const id = byId("otpAccountSelect").value;
    if (!id) throw new Error("Chọn tài khoản đang chờ OTP.");
    await api("/api/accounts/" + encodeURIComponent(id) + "/otp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ otp: otp })
    });
    byId("otp").value = "";
    setMessage(authMessage, "Đã thêm và xác minh tài khoản Muse.", "success");
    await window.refreshMuseAccounts?.();
    await refreshAuth();
  } catch (err) {
    setMessage(authMessage, err.message, "error");
  } finally {
    confirmOtp.disabled = false;
  }
});

promptEl.addEventListener("input", () => {
  byId("charCount").textContent = String(promptEl.value.length) + " ký tự";
});

document.querySelectorAll("[data-template]").forEach((button) => {
  button.addEventListener("click", () => {
    promptEl.value = templates[button.dataset.template];
    promptEl.dispatchEvent(new Event("input"));
    promptEl.focus();
  });
});

function renderFiles() {
  imageList.innerHTML = "";
  selectedFiles.forEach((file) => {
    const tag = document.createElement("span");
    tag.className = "file-tag";
    tag.title = file.name;
    tag.textContent = file.name;
    imageList.appendChild(tag);
  });
}

imagesEl.addEventListener("change", () => {
  selectedFiles = Array.from(imagesEl.files);
  renderFiles();
});

["dragenter", "dragover"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("drag");
  });
});

["dragleave", "drop"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("drag");
  });
});

dropzone.addEventListener("drop", (event) => {
  selectedFiles = Array.from(event.dataTransfer.files).filter((file) =>
    file.type.startsWith("image/")
  );
  renderFiles();
});

byId("refreshModel").addEventListener("click", async () => {
  const button = byId("refreshModel");
  button.disabled = true;
  button.textContent = "Đang kiểm tra...";
  try {
    const data = await api("/api/model");
    button.textContent =
      typeof data.model === "string" ? data.model : "Model OK";
  } catch (err) {
    button.textContent = "Model lỗi";
    setMessage(generateMessage, err.message, "error");
  }
  setTimeout(() => {
    button.disabled = false;
    button.textContent = "Kiểm tra model";
  }, 2500);
});

generateBtn.addEventListener("click", async () => {
  const prompt = promptEl.value.trim();
  if (!prompt) {
    setMessage(generateMessage, "Nhập prompt trước.", "error");
    return;
  }

  const form = new FormData();
  form.append("prompt", prompt);
  form.append("timeout", byId("timeout").value);
  form.append("min_videos", byId("minVideos").value);
  form.append("task_count", byId("taskCount").value);
  selectedFiles.forEach((file) => form.append("images", file, file.name));

  generateBtn.disabled = true;
  setMessage(generateMessage, "Đang gửi generation job...");

  try {
    const batch = await api("/api/generations", {
      method: "POST",
      body: form
    });
    setMessage(
      generateMessage,
      "Đã tạo batch " + batch.batch_id + " với " + batch.count +
      " task trên " + batch.count + " tài khoản Muse khác nhau.",
      "success"
    );
    await window.refreshMuseAccounts?.();
    promptEl.value = "";
    promptEl.dispatchEvent(new Event("input"));
    selectedFiles = [];
    imagesEl.value = "";
    renderFiles();
    await refreshJobs();
    startPolling();
  } catch (err) {
    setMessage(generateMessage, err.message, "error");
  } finally {
    await refreshAuth();
  }
});

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function statusLabel(status) {
  const labels = {
    queued: "Đang chờ",
    connecting: "Kết nối",
    generating: "Đang tạo",
    downloading: "Đang tải",
    completed: "Hoàn tất",
    completed_no_media: "Hoàn tất",
    failed: "Lỗi",
    cancelled: "Đã hủy",
    interrupted: "Gián đoạn"
  };
  return labels[status] || status;
}

function renderJobs(jobs) {
  jobsEl.innerHTML = "";
  const running = jobs.find((job) =>
    ["queued", "connecting", "generating", "downloading"].includes(job.status)
  );

  if (running) {
    activeJob.classList.remove("hidden");
    activeJob.innerHTML =
      '<span class="spinner"></span><strong>' +
      escapeHtml(statusLabel(running.status)) +
      "</strong> · " +
      escapeHtml(running.message) +
      " · <code>" +
      escapeHtml(running.id) +
      "</code>";
  } else {
    activeJob.classList.add("hidden");
  }

  if (!jobs.length) {
    jobsEl.innerHTML = '<div class="muted">Chưa có generation job nào.</div>';
    return;
  }

  const template = byId("jobTemplate");

  jobs.forEach((job) => {
    const node = template.content.cloneNode(true);
    node.querySelector(".job-id").textContent = "#" + job.id;
    node.querySelector(".job-time").textContent =
      new Date(job.created_at).toLocaleString();

    const status = node.querySelector(".status");
    status.textContent = statusLabel(job.status);
    status.classList.add(job.status);

    node.querySelector(".job-prompt").textContent = job.prompt;
    node.querySelector(".job-message").textContent = job.message || "";

    const meta = node.querySelector(".job-meta");
    const entries = [
      String((job.images || []).length) + " ảnh",
      "min " + job.min_videos + " video",
      "TK: " + (job.account_label || "chưa gán"),
      ...(job.batch_id ? ["batch " + job.batch_id] : [])
    ];

    if (job.session_id) {
      entries.push("session " + job.session_id.slice(0, 8) + "…");
    }

    entries.forEach((value) => {
      const el = document.createElement("span");
      el.className = "meta";
      el.textContent = value;
      meta.appendChild(el);
    });

    const videos = node.querySelector(".videos");
    (job.download_urls || []).forEach((url, index) => {
      const wrap = document.createElement("div");
      wrap.className = "video-wrap";

      const video = document.createElement("video");
      video.controls = true;
      video.preload = "metadata";
      video.src = url;

      const actions = document.createElement("div");
      actions.className = "video-actions";

      const label = document.createElement("span");
      label.textContent = "Video " + (index + 1);

      const download = document.createElement("a");
      download.href = url;
      download.download = "";
      download.textContent = "Tải MP4 ↓";

      actions.appendChild(label);
      actions.appendChild(download);
      wrap.appendChild(video);
      wrap.appendChild(actions);
      videos.appendChild(wrap);
    });

    const errors = Array.from(job.download_errors || []);
    if (job.error) {
      errors.push(job.error);
    }
    if (errors.length) {
      node.querySelector(".job-error").textContent = errors.join("\n");
    }

    jobsEl.appendChild(node);
  });
}

async function refreshJobs() {
  try {
    const data = await api("/api/generations");
    renderJobs(data.jobs);
    return data.jobs;
  } catch (err) {
    jobsEl.innerHTML =
      '<div class="job-error">' + escapeHtml(err.message) + "</div>";
    return [];
  }
}

byId("refreshJobs").addEventListener("click", refreshJobs);

function startPolling() {
  if (pollTimer) {
    return;
  }

  pollTimer = setInterval(async () => {
    const jobs = await refreshJobs();
    const running = jobs.some((job) =>
      ["queued", "connecting", "generating", "downloading"].includes(job.status)
    );
    if (!running) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 3000);
}

(async function init() {
  await refreshAuth();
  const jobs = await refreshJobs();
  if (
    jobs.some((job) =>
      ["queued", "connecting", "generating", "downloading"].includes(job.status)
    )
  ) {
    startPolling();
  }
})();
