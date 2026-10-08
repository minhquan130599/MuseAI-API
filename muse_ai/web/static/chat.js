// Independent Chat mode: keeps conversations on this browser and resumes Muse sessions.
(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const KEY = "muse-ai-web-chat-v1";
  const MAX_THREADS = 30;
  const MAX_MESSAGES = 120;
  const buttons = [...document.querySelectorAll(".mode-tab")];
  const chatMode = el("chatMode");
  const videoMode = el("videoMode");
  const jobsPanel = el("jobsPanel");
  const chatInput = el("chatInput");
  const sendButton = el("sendChat");
  const messageBox = el("chatMessages");
  const chatNotice = el("chatMessage");
  const threadSelect = el("chatThreads");
  const accountSelect = el("chatAccount");
  const sessionInfo = el("chatSession");
  let knownAccounts = [];
  const title = el("workspaceTitle");
  let threads = [];
  let activeId = null;
  let busy = false;

  function makeId() {
    return typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : String(Date.now()) + "-" + Math.random().toString(36).slice(2);
  }

  function load() {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || "{}");
      if (Array.isArray(saved.threads)) {
        threads = saved.threads.slice(0, MAX_THREADS).filter((thread) =>
          thread && typeof thread.id === "string" &&
          Array.isArray(thread.messages)
        );
      }
      if (typeof saved.activeId === "string") activeId = saved.activeId;
    } catch (_) {
      threads = [];
    }
    if (!threads.some((thread) => thread.id === activeId)) {
      activeId = threads[0]?.id || null;
    }
  }

  function persist() {
    try {
      localStorage.setItem(KEY, JSON.stringify({
        activeId,
        threads: threads.slice(0, MAX_THREADS).map((thread) => ({
          ...thread,
          messages: thread.messages.slice(-MAX_MESSAGES)
        }))
      }));
    } catch (_) {
      chatNotice.textContent = "Trình duyệt không thể lưu lịch sử cục bộ.";
      chatNotice.className = "message error";
    }
  }

  function activeThread() {
    return threads.find((thread) => thread.id === activeId) || null;
  }

  function createThread() {
    const thread = {
      id: makeId(),
      name: "Cuộc trò chuyện mới",
      sessionId: null,
      accountId: accountSelect.value || null,
      messages: [],
      updatedAt: Date.now()
    };
    threads.unshift(thread);
    threads = threads.slice(0, MAX_THREADS);
    activeId = thread.id;
    persist();
    render();
    chatInput.focus();
  }

  function render() {
    threadSelect.replaceChildren();
    for (const thread of threads) {
      const option = document.createElement("option");
      option.value = thread.id;
      option.textContent = thread.name || "Cuộc trò chuyện";
      threadSelect.appendChild(option);
    }
    if (activeId) threadSelect.value = activeId;
    const thread = activeThread();
    if (thread && thread.accountId) accountSelect.value = thread.accountId;
    sessionInfo.textContent = thread?.sessionId
      ? "Muse session: " + thread.sessionId
      : "Chưa có session · Lượt đầu sẽ tạo session mới";
    messageBox.replaceChildren();
    if (!thread || thread.messages.length === 0) {
      const empty = document.createElement("div");
      empty.className = "chat-empty";
      const heading = document.createElement("strong");
      heading.textContent = "Gửi tin nhắn bình thường tới Muse";
      const hint = document.createElement("span");
      hint.textContent = "Bạn có thể hỏi tiếp nhiều lượt trong cùng một session.";
      empty.append(heading, hint);
      messageBox.appendChild(empty);
    } else {
      for (const message of thread.messages) {
        const wrapper = document.createElement("div");
        wrapper.className = "chat-bubble " +
          (message.role === "assistant" ? "assistant" : "user");
        const label = document.createElement("div");
        label.className = "chat-bubble-label";
        label.textContent = message.role === "assistant" ? "Muse" : "Bạn";
        const body = document.createElement("div");
        body.className = "chat-bubble-text";
        body.textContent = message.text || "";
        wrapper.append(label, body);
        if (Array.isArray(message.files) && message.files.length) {
          const gallery = document.createElement("div");
          gallery.className = "chat-attachments";
          for (const file of message.files) {
            if (!file || typeof file.url !== "string" ||
                !/^\/api\/chat\/media\/[a-f0-9]+\/files\/[\w.\-]+$/.test(file.url)) {
              continue;
            }
            const item = document.createElement("div");
            item.className = "chat-attachment";
            if (file.kind === "image") {
              const img = document.createElement("img");
              img.src = file.url;
              img.alt = file.filename || "Ảnh từ Muse";
              img.loading = "lazy";
              item.appendChild(img);
            } else if (file.kind === "video") {
              const video = document.createElement("video");
              video.src = file.url;
              video.preload = "metadata";
              video.controls = true;
              item.appendChild(video);
            }
            const link = document.createElement("a");
            link.href = file.url + "?download=1";
            link.textContent = "↓ Tải " + (file.filename || "tệp");
            link.download = file.filename || "";
            item.appendChild(link);
            gallery.appendChild(item);
          }
          if (gallery.childNodes.length) wrapper.appendChild(gallery);
        }
        if (message.mediaStatus === "watching" && message.mediaExpected) {
          const status = document.createElement("p");
          status.className = "chat-media-status";
          status.textContent = "Đang kiểm tra tệp đính kèm từ Muse…";
          wrapper.appendChild(status);
        } else if (Array.isArray(message.mediaErrors) && message.mediaErrors.length) {
          const warning = document.createElement("p");
          warning.className = "chat-media-warning";
          warning.textContent = message.mediaErrors.join(" ");
          wrapper.appendChild(warning);
        }
        messageBox.appendChild(wrapper);
      }
    }
    if (busy && activeId === pendingThreadId) {
      const typing = document.createElement("div");
      typing.className = "chat-bubble assistant chat-typing";
      typing.textContent = "Muse đang trả lời…";
      messageBox.appendChild(typing);
    }
    messageBox.scrollTop = messageBox.scrollHeight;
  }

  let pendingThreadId = null;
  const activeMediaPolls = new Set();

  async function followMedia(thread, reply) {
    const jobId = reply.mediaJobId;
    if (!jobId || activeMediaPolls.has(jobId)) return;
    activeMediaPolls.add(jobId);
    let failures = 0;
    try {
      while (reply.mediaStatus === "watching") {
        const response = await fetch("/api/chat/media/" + encodeURIComponent(jobId), {
          cache: "no-store"
        });
        if (!response.ok) {
          failures++;
          if (failures >= 3) throw new Error("Không kiểm tra được tệp từ Muse.");
        } else {
          failures = 0;
          const data = await response.json();
          reply.files = Array.isArray(data.files) ? data.files : [];
          reply.mediaErrors = Array.isArray(data.errors) ? data.errors : [];
          reply.mediaStatus = data.status || "watching";
          persist();
          if (activeId === thread.id) render();
        }
        if (reply.mediaStatus !== "watching") break;
        await new Promise((resolve) => setTimeout(resolve, 3000));
      }
    } catch (err) {
      reply.mediaStatus = "failed";
      reply.mediaErrors = [String(err.message || err)];
      persist();
      if (activeId === thread.id) render();
    } finally {
      activeMediaPolls.delete(jobId);
    }
  }

  function setMode(mode) {
    const chat = mode === "chat";
    chatMode.classList.toggle("hidden", !chat);
    videoMode.classList.toggle("hidden", chat);
    jobsPanel.classList.toggle("hidden", chat);
    title.textContent = chat ? "Chat với Muse" : "Tạo video";
    for (const button of buttons) {
      const selected = button.dataset.mode === mode;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", selected ? "true" : "false");
    }
    if (chat) {
      render();
      chatInput.focus();
    }
  }

  async function sendMessage() {
    const message = chatInput.value.trim();
    if (!message || busy) return;
    if (!activeThread()) createThread();
    const thread = activeThread();
    if (!thread.accountId && thread.sessionId) {
      chatNotice.textContent = "Hội thoại cũ không gắn tài khoản. Hãy tạo Chat mới và chọn tài khoản Muse.";
      chatNotice.className = "message error";
      return;
    }
    if (!thread.accountId) thread.accountId = accountSelect.value || null;
    if (!thread.accountId ||
        !knownAccounts.some(a => a.id === thread.accountId && a.enabled && a.status === "ready")) {
      chatNotice.textContent = "Vui lòng chọn tài khoản Muse đang sẵn sàng để chat.";
      chatNotice.className = "message error";
      return;
    }
    const requestedId = thread.id;
    busy = true;
    pendingThreadId = requestedId;
    sendButton.disabled = true;
    chatNotice.textContent = "";
    chatNotice.className = "message";
    thread.messages.push({role: "user", text: message});
    thread.messages = thread.messages.slice(-MAX_MESSAGES);
    if (thread.messages.length === 1) {
      thread.name = message.length > 54
        ? message.slice(0, 54) + "…"
        : message;
    }
    thread.updatedAt = Date.now();
    chatInput.value = "";
    persist();
    render();

    try {
      const response = await fetch("/api/chat/send", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          message,
          account_id: thread.accountId,
          session_id: thread.sessionId || null,
          timeout: 120
        })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(typeof data.detail === "string"
          ? data.detail
          : "Không gửi được tin nhắn (HTTP " + response.status + ")");
      }
      if (typeof data.text !== "string" || !data.text.trim()) {
        throw new Error("Muse chưa trả về nội dung text.");
      }
      thread.sessionId = data.session_id || thread.sessionId;
      const reply = {
        role: "assistant",
        text: typeof data.text === "string" ? data.text : "",
        files: Array.isArray(data.attachments) ? data.attachments : [],
        mediaJobId: data.media_job_id || null,
        mediaStatus: data.media_job_id ? "watching" : "completed",
        mediaExpected: Boolean(data.media_expected),
        mediaErrors: []
      };
      thread.messages.push(reply);
      thread.messages = thread.messages.slice(-MAX_MESSAGES);
      thread.updatedAt = Date.now();
      persist();
      if (reply.mediaJobId) void followMedia(thread, reply);
    } catch (err) {
      chatNotice.textContent = err.message +
        " Có thể Muse đã nhận yêu cầu; hãy kiểm tra trước khi gửi lại để tránh lặp.";
      chatNotice.className = "message error";
    } finally {
      busy = false;
      pendingThreadId = null;
      sendButton.disabled = false;
      if (activeId === requestedId) render();
      chatInput.focus();
    }
  }

  buttons.forEach((button) => {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  });
  el("newChat").addEventListener("click", createThread);
  threadSelect.addEventListener("change", () => {
    activeId = threadSelect.value;
    persist();
    render();
  });
  accountSelect.addEventListener("change", () => {
    const thread = activeThread();
    if (!thread || thread.messages.length || thread.sessionId) {
      createThread();
    } else {
      thread.accountId = accountSelect.value || null;
      persist();
      render();
    }
  });
  window.addEventListener("muse-accounts-updated", (event) => {
    knownAccounts = event.detail || [];
    const previous = accountSelect.value;
    accountSelect.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Chọn tài khoản Muse";
    accountSelect.appendChild(placeholder);
    for (const account of knownAccounts) {
      if (account.enabled && account.status === "ready") {
        const option = document.createElement("option");
        option.value = account.id;
        option.textContent = account.label || account.email || account.id;
        accountSelect.appendChild(option);
      }
    }
    const thread = activeThread();
    if (thread?.accountId) accountSelect.value = thread.accountId;
    else if (previous) accountSelect.value = previous;
    else if (accountSelect.options.length === 2) accountSelect.value = accountSelect.options[1].value;
    if (thread && !thread.accountId && !thread.messages.length &&
        !thread.sessionId && accountSelect.value) {
      thread.accountId = accountSelect.value;
      persist();
    }
  });
  sendButton.addEventListener("click", sendMessage);
  chatInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage();
    }
  });
  load();
  if (!activeThread()) createThread();
  setMode("video");
  for (const thread of threads) {
    for (const message of thread.messages) {
      if (message.mediaJobId && message.mediaStatus === "watching") {
        void followMedia(thread, message);
      }
    }
  }
})();
