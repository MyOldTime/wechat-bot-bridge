(() => {
  "use strict";

  // 页面元素与服务端下发的运行限制。
  const elements = {
    attachmentInput: document.querySelector("#attachment-input"),
    attachmentName: document.querySelector("#attachment-name"),
    attachmentPreview: document.querySelector("#attachment-preview"),
    attachmentSize: document.querySelector("#attachment-size"),
    characterCount: document.querySelector("#character-count"),
    composer: document.querySelector("#composer"),
    connectionDot: document.querySelector("#connection-dot"),
    connectionLabel: document.querySelector("#connection-label"),
    conversationPreview: document.querySelector("#conversation-preview"),
    conversationTime: document.querySelector("#conversation-time"),
    emptyState: document.querySelector("#empty-state"),
    gatewayBadge: document.querySelector("#gateway-badge"),
    loginDescription: document.querySelector("#login-description"),
    loginPanel: document.querySelector("#login-panel"),
    loginProgress: document.querySelector("#login-progress"),
    loginProgressLabel: document.querySelector("#login-progress-label"),
    loginQr: document.querySelector("#login-qr"),
    loginResetForm: document.querySelector("#login-reset-form"),
    loadOlder: document.querySelector("#load-older"),
    logoutForm: document.querySelector("#logout-form"),
    messageInput: document.querySelector("#message-input"),
    messageList: document.querySelector("#message-list"),
    messageScroll: document.querySelector("#message-scroll"),
    refreshButton: document.querySelector("#refresh-button"),
    removeAttachment: document.querySelector("#remove-attachment"),
    sendButton: document.querySelector("#send-button"),
    startLoginButton: document.querySelector("#start-login-button"),
    toastRegion: document.querySelector("#toast-region"),
    typingIndicator: document.querySelector("#typing-indicator"),
    verificationCode: document.querySelector("#verification-code"),
    verificationForm: document.querySelector("#verification-form"),
    qrFrame: document.querySelector("#qr-frame"),
  };

  const adminToken = document.querySelector('meta[name="admin-token"]').content;
  const messageMaxChars = Number(
    document.querySelector('meta[name="message-max-chars"]').content,
  );
  const mediaMaxBytes = Number(document.querySelector('meta[name="media-max-bytes"]').content);
  const typingTimeoutMilliseconds =
    Number(document.querySelector('meta[name="typing-timeout-seconds"]').content) * 1000;
  const historyPageSize = 60;
  const messageDividerIntervalMilliseconds = 5 * 60 * 1000;
  const nearBottomThresholdPixels = 130;
  const qrRefreshMinimumIntervalMilliseconds = 4000;
  const statusPollIntervalMilliseconds = 1200;
  const toastLifetimeMilliseconds = 4200;

  // 页面内状态只作为即时视图；SQLite 历史始终是断线恢复后的事实来源。
  const messages = new Map();
  let nextCursor = null;
  let selectedFile = null;
  let websocket = null;
  let reconnectTimer = null;
  let reconnectAttempts = 0;
  let sending = false;
  let typingActive = false;
  let typingLastRenewalAt = 0;
  let typingTimer = null;
  let typingQueue = Promise.resolve();
  let gatewayReady = false;
  let historyLoaded = false;
  let loginStarting = false;
  let statusPollTimer = null;
  let qrLastRefreshAt = 0;
  let accountGeneration = 0;

  const labels = {
    external_api: "外部任务",
    web_admin: "管理页面",
    wechat: "微信",
  };

  const stateLabels = {
    failed: "发送失败",
    received: "已接收",
    sending: "发送中",
    sent: "已发送",
  };

  // 通用格式化与安全 DOM 文本工具。
  function escapeText(value) {
    return document.createTextNode(value ?? "");
  }

  function formatClock(value) {
    const date = new Date(value);
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }

  function formatDivider(value) {
    const date = new Date(value);
    const today = new Date();
    const sameDay = date.toDateString() === today.toDateString();
    return new Intl.DateTimeFormat("zh-CN", {
      ...(sameDay ? {} : { month: "short", day: "numeric" }),
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }

  function formatBytes(size) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function messageTypeLabel(type) {
    return (
      {
        file: "文件",
        image: "图片",
        text: "文字",
        video: "视频",
        voice: "语音",
      }[type] || "消息"
    );
  }

  function isMediaPlaceholder(text, type) {
    const normalized = String(text || "")
      .trim()
      .toLocaleLowerCase();
    const placeholders = {
      file: ["[file]", "[文件]"],
      image: ["[image]", "[图片]"],
      text: [],
      video: ["[video]", "[视频]"],
      voice: ["[voice]", "[语音]"],
    };
    return (placeholders[type] || []).some(
      (placeholder) => placeholder.toLocaleLowerCase() === normalized,
    );
  }

  function quoteSummary(message) {
    let text = String(message.text || message.file_name || "")
      .replace(/\s+/g, " ")
      .trim();
    if (isMediaPlaceholder(text, message.type)) text = "";
    const prefix = message.type === "text" ? "" : `[${messageTypeLabel(message.type)}] `;
    const summary = `${prefix}${text}`.trim() || `[${messageTypeLabel(message.type)}]`;
    return summary.length > 120 ? `${summary.slice(0, 119)}…` : summary;
  }

  function showToast(message, type = "normal") {
    const toast = document.createElement("div");
    toast.className = `toast ${type === "error" ? "error" : ""}`;
    toast.append(escapeText(message));
    elements.toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), toastLifetimeMilliseconds);
  }

  // 所有管理端 HTTP 请求都走同源认证，并统一提取后端错误信息。
  async function requestJson(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      let message = `请求失败（HTTP ${response.status}）`;
      try {
        const body = await response.json();
        message = body.detail?.message || body.detail || message;
      } catch {
        // 非 JSON 错误保留通用提示。
      }
      throw new Error(message);
    }
    return response.json();
  }

  function sortedMessages() {
    return [...messages.values()].sort((left, right) => {
      const timeDifference = new Date(left.occurred_at) - new Date(right.occurred_at);
      return timeDifference || left.id.localeCompare(right.id);
    });
  }

  // 消息气泡与一层引用快照渲染。
  function makeTextBubble(message) {
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const content = document.createElement("div");
    content.className = "bubble-text";
    content.append(escapeText(message.text));
    bubble.append(content);
    return bubble;
  }

  function makeImageBubble(message) {
    const bubble = document.createElement("div");
    bubble.className = "bubble media-bubble";

    if (message.media_url && message.mime_type?.startsWith("image/")) {
      const link = document.createElement("a");
      link.href = message.media_url;
      link.target = "_blank";
      link.rel = "noopener";
      const image = document.createElement("img");
      image.className = "message-image";
      image.src = message.media_url;
      image.alt =
        message.text && !isMediaPlaceholder(message.text, "image")
          ? message.text
          : "微信图片";
      image.loading = "lazy";
      const action = document.createElement("span");
      action.className = "media-action";
      action.textContent = "点击查看原图";
      link.append(image, action);
      bubble.append(link);
    } else if (message.media_url) {
      const download = document.createElement("a");
      download.className = "media-download-link";
      download.href = message.media_url;
      download.textContent = "图片文件 · 点击下载";
      bubble.append(download);
    } else {
      const fallback = document.createElement("div");
      fallback.className = "bubble-text";
      fallback.append(escapeText("[图片]"));
      bubble.append(fallback);
    }

    if (message.text && !isMediaPlaceholder(message.text, "image")) {
      const caption = document.createElement("div");
      caption.className = "bubble-text";
      caption.append(escapeText(message.text));
      bubble.append(caption);
    }
    return bubble;
  }

  function makeFileBubble(message) {
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const card = document.createElement(message.media_url ? "a" : "div");
    card.className = "file-card";
    if (message.media_url) {
      card.href = message.media_url;
    }

    const symbol = document.createElement("span");
    symbol.className = "file-symbol";
    symbol.textContent = "▤";
    const copy = document.createElement("span");
    copy.className = "file-copy";
    const name = document.createElement("strong");
    name.append(escapeText(message.file_name || message.text || "微信文件"));
    const caption = document.createElement("small");
    const captionText =
      message.text &&
      message.text !== message.file_name &&
      !isMediaPlaceholder(message.text, "file")
        ? message.text
        : "点击下载文件";
    caption.append(escapeText(captionText));
    copy.append(name, caption);
    const download = document.createElement("span");
    download.className = "download-mark";
    download.textContent = "↓";
    card.append(symbol, copy, download);
    bubble.append(card);
    return bubble;
  }

  function makeFallbackBubble(message) {
    const readableType = { video: "视频", voice: "语音" }[message.type] || "消息";
    return makeTextBubble({
      text:
        message.text && !isMediaPlaceholder(message.text, message.type)
          ? message.text
          : `[${readableType}]`,
    });
  }

  function prependQuoteBlock(bubble, message) {
    if (!message.quote) return;
    const canLocate = Boolean(message.quote.message_id);
    const quoted = document.createElement(canLocate ? "button" : "div");
    if (canLocate) {
      quoted.type = "button";
      quoted.title = "定位到被引用的原文";
    }
    quoted.className = "message-quote";
    const title = document.createElement("strong");
    title.textContent = message.quote.title
      ? `微信原生引用 · ${message.quote.title}`
      : "微信原生引用";
    const content = document.createElement("span");
    content.append(escapeText(quoteSummary(message.quote)));
    quoted.append(title, content);
    if (canLocate) {
      quoted.addEventListener("click", () => {
        focusQuotedMessage(message.quote.message_id);
      });
    }
    bubble.prepend(quoted);
  }

  function focusQuotedMessage(messageId) {
    const target = elements.messageList.querySelector(
      `[data-message-id="${CSS.escape(messageId)}"]`,
    );
    if (!target) {
      showToast("被引用的原文不在当前已加载记录中");
      return;
    }
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.remove("quote-target");
    // 强制浏览器提交一次样式，使连续点击也能重新播放两次高亮动画。
    void target.offsetWidth;
    target.classList.add("quote-target");
    window.setTimeout(() => target.classList.remove("quote-target"), 1200);
  }

  function createMessageRow(message) {
    const row = document.createElement("article");
    row.className = `message-row ${message.direction}`;
    row.dataset.messageId = message.id;

    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.textContent = message.direction === "outbound" ? "我" : "微";

    const stack = document.createElement("div");
    stack.className = "message-stack";
    const meta = document.createElement("div");
    meta.className = "message-meta";
    const origin = document.createElement("span");
    origin.className = "message-origin";
    origin.textContent = labels[message.origin] || message.origin;
    const time = document.createElement("time");
    time.dateTime = message.occurred_at;
    time.textContent = formatClock(message.occurred_at);
    const state = document.createElement("span");
    state.className = `message-state ${message.status}`;
    state.textContent = stateLabels[message.status] || message.status;
    meta.append(origin, time, state);

    const bubble =
      message.type === "image"
        ? makeImageBubble(message)
        : message.type === "file"
          ? makeFileBubble(message)
          : message.type === "text"
            ? makeTextBubble(message)
            : makeFallbackBubble(message);
    prependQuoteBlock(bubble, message);
    stack.append(meta, bubble);

    if (message.status === "failed") {
      const error = document.createElement("div");
      error.className = "message-error";
      error.append(escapeText(`发送失败：${message.error_code || "未知错误"}`));
      stack.append(error);
    }

    row.append(avatar, stack);
    return row;
  }

  function renderMessages({ preservePosition = false, forceBottom = false } = {}) {
    const previousHeight = elements.messageScroll.scrollHeight;
    const previousTop = elements.messageScroll.scrollTop;
    const ordered = sortedMessages();
    elements.messageList.replaceChildren();

    let previousTime = null;
    for (const message of ordered) {
      const timestamp = new Date(message.occurred_at).getTime();
      if (
        previousTime === null ||
        timestamp - previousTime > messageDividerIntervalMilliseconds
      ) {
        const divider = document.createElement("div");
        divider.className = "time-divider";
        divider.textContent = formatDivider(message.occurred_at);
        elements.messageList.append(divider);
      }
      elements.messageList.append(createMessageRow(message));
      previousTime = timestamp;
    }

    elements.emptyState.hidden = ordered.length !== 0;
    const latest = ordered.at(-1);
    elements.conversationPreview.textContent = latest
      ? latest.type === "text"
        ? latest.text
        : `[${{ image: "图片", file: "文件", voice: "语音", video: "视频" }[latest.type] || "消息"}]`
      : "暂无消息";
    elements.conversationTime.textContent = latest ? formatClock(latest.occurred_at) : "";

    if (preservePosition) {
      elements.messageScroll.scrollTop =
        elements.messageScroll.scrollHeight - previousHeight + previousTop;
    } else if (forceBottom) {
      elements.messageScroll.scrollTop = elements.messageScroll.scrollHeight;
    }
  }

  function upsertMessage(message, { scroll = true } = {}) {
    const nearBottom =
      elements.messageScroll.scrollHeight -
        elements.messageScroll.scrollTop -
        elements.messageScroll.clientHeight <
      nearBottomThresholdPixels;
    messages.set(message.id, message);
    renderMessages({ forceBottom: scroll && nearBottom });
  }

  async function loadMessages({ older = false } = {}) {
    const generation = accountGeneration;
    const query = new URLSearchParams({ limit: String(historyPageSize) });
    if (older && nextCursor) query.set("cursor", nextCursor);
    const page = await requestJson(`/admin/api/messages?${query}`);
    if (generation !== accountGeneration || !gatewayReady) return;
    for (const message of page.items) messages.set(message.id, message);
    nextCursor = page.next_cursor;
    elements.loadOlder.hidden = !nextCursor;
    renderMessages({
      preservePosition: older,
      forceBottom: !older,
    });
  }

  // 微信授权状态与登录面板。
  function updateConnection(connected, label) {
    elements.connectionDot.classList.toggle("online", connected);
    elements.connectionDot.classList.toggle("offline", !connected);
    elements.connectionLabel.textContent = label;
  }

  function updateGateway(status) {
    const phaseLabels = {
      error: "微信连接异常",
      login_required: "等待微信登录",
      listening: "微信已连接",
      logging_in: status.qr_ready
        ? "等待扫码登录"
        : status.verification_required
          ? "等待配对数字"
          : "正在登录微信",
      stopped: "微信未启动",
    };
    elements.gatewayBadge.textContent = phaseLabels[status.phase] || "微信状态未知";
    elements.gatewayBadge.classList.toggle("ready", status.phase === "listening");
    elements.gatewayBadge.classList.toggle(
      "warning",
      status.phase !== "listening",
    );
    updateLoginPanel(status);
  }

  function refreshQrImage() {
    const now = Date.now();
    if (now - qrLastRefreshAt < qrRefreshMinimumIntervalMilliseconds) return;
    qrLastRefreshAt = now;
    elements.loginQr.src = `/admin/api/login/qr?view=${now}`;
  }

  function resetConversationView() {
    accountGeneration += 1;
    messages.clear();
    nextCursor = null;
    historyLoaded = false;
    selectedFile = null;
    elements.attachmentInput.value = "";
    elements.attachmentPreview.hidden = true;
    elements.loadOlder.hidden = true;
    elements.typingIndicator.hidden = true;
    stopTypingStatus({ notifyServer: false });
    renderMessages();
    updateComposer();
  }

  async function ensureConversationLoaded() {
    if (historyLoaded || !gatewayReady) return;
    historyLoaded = true;
    try {
      await loadMessages();
    } catch (error) {
      historyLoaded = false;
      showToast(error.message, "error");
    }
  }

  function updateLoginPanel(status) {
    const wasReady = gatewayReady;
    gatewayReady = status.phase === "listening";
    elements.loginPanel.hidden = gatewayReady;
    elements.composer.inert = !gatewayReady;
    elements.refreshButton.disabled = !gatewayReady;

    if (gatewayReady) {
      elements.qrFrame.hidden = true;
      elements.verificationForm.hidden = true;
      elements.startLoginButton.hidden = true;
      elements.loginResetForm.hidden = true;
      if (!wasReady) void ensureConversationLoaded();
      updateComposer();
      return;
    }

    if (wasReady) resetConversationView();
    elements.conversationPreview.textContent = "等待微信登录";
    elements.conversationTime.textContent = "";
    elements.qrFrame.hidden = !status.qr_ready;
    elements.verificationForm.hidden = !status.verification_required;
    elements.startLoginButton.hidden = status.phase !== "error";
    elements.loginResetForm.hidden = status.phase !== "error";
    elements.loginProgress.hidden = status.verification_required;

    if (status.qr_ready) {
      elements.loginDescription.textContent =
        "请使用作为 owner 的微信扫描二维码，并在微信中确认登录。";
      elements.loginProgressLabel.textContent = "等待微信扫码确认";
      elements.loginProgress.hidden = false;
      refreshQrImage();
    } else if (status.verification_required) {
      elements.loginDescription.textContent =
        status.error_code === "verification_code_retry"
          ? "配对数字不正确，请输入微信中最新显示的数字。"
          : "微信要求额外确认，请输入微信中显示的配对数字。";
      elements.verificationCode.focus();
    } else if (status.phase === "login_required") {
      elements.loginDescription.textContent =
        "本机尚未保存微信授权，正在安全地申请登录二维码。";
      elements.loginProgressLabel.textContent = "正在生成二维码…";
      elements.loginProgress.hidden = false;
      void requestLogin();
    } else if (status.phase === "logging_in") {
      elements.loginDescription.textContent = "二维码已扫描，请在微信中完成确认。";
      elements.loginProgressLabel.textContent = "等待微信确认登录…";
      elements.loginProgress.hidden = false;
    } else if (status.phase === "error") {
      elements.loginDescription.textContent =
        "登录或连接过程出现异常。可以重试；若本地授权损坏，请清除状态后重新登录。";
      elements.loginProgressLabel.textContent = "连接异常";
      elements.loginProgress.hidden = true;
    } else {
      elements.loginDescription.textContent = "微信连接尚未启动，请稍后刷新页面。";
      elements.loginProgressLabel.textContent = "等待服务启动…";
      elements.loginProgress.hidden = false;
    }
    updateComposer();
  }

  async function requestLogin() {
    if (loginStarting || gatewayReady) return;
    loginStarting = true;
    elements.startLoginButton.disabled = true;
    try {
      const status = await requestJson("/admin/api/login", {
        method: "POST",
        headers: { "X-Admin-Token": adminToken },
      });
      updateGateway(status);
    } catch (error) {
      elements.startLoginButton.hidden = false;
      showToast(error.message, "error");
    } finally {
      loginStarting = false;
      elements.startLoginButton.disabled = false;
    }
  }

  async function refreshStatus({ quiet = false } = {}) {
    try {
      updateGateway(await requestJson("/admin/api/status"));
    } catch (error) {
      elements.gatewayBadge.textContent = "状态获取失败";
      elements.gatewayBadge.classList.add("warning");
      if (!quiet) showToast(error.message, "error");
    }
  }

  // WebSocket 只负责低延迟提示，断线后仍由历史接口恢复完整状态。
  function scheduleReconnect() {
    window.clearTimeout(reconnectTimer);
    const delay = Math.min(1000 * 2 ** reconnectAttempts, 15000);
    reconnectAttempts += 1;
    reconnectTimer = window.setTimeout(connectWebSocket, delay);
  }

  function connectWebSocket() {
    if (websocket && websocket.readyState < WebSocket.CLOSING) return;
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${window.location.host}/admin/ws`;
    // 令牌通过子协议头传递，避免查询串进入反向代理和访问日志。
    websocket = new WebSocket(url, ["wbb-admin", `wbb-token.${adminToken}`]);

    websocket.addEventListener("open", () => {
      reconnectAttempts = 0;
      updateConnection(true, "实时连接正常");
    });
    websocket.addEventListener("message", (event) => {
      const payload = JSON.parse(event.data);
      if (payload.type === "connected") {
        updateGateway(payload.status);
      } else if (payload.type === "message_upsert") {
        upsertMessage(payload.message);
      } else if (payload.type === "typing") {
        elements.typingIndicator.hidden = !payload.active;
        if (!payload.active && payload.origin === "web_admin") {
          typingActive = false;
          typingLastRenewalAt = 0;
          window.clearTimeout(typingTimer);
          typingTimer = null;
        }
        if (payload.active) {
          elements.messageScroll.scrollTop = elements.messageScroll.scrollHeight;
        }
      } else if (payload.type === "account_reset") {
        resetConversationView();
        updateGateway({
          phase: "login_required",
          authenticated: false,
          listener_running: false,
          qr_ready: false,
          verification_required: false,
          error_code: null,
        });
      }
    });
    websocket.addEventListener("close", (event) => {
      if (event.code === 4401) {
        updateConnection(false, "管理令牌已过期，请刷新页面");
        showToast("管理令牌已过期，请刷新页面重新连接", "error");
        return;
      }
      updateConnection(false, "实时连接已断开，正在重连…");
      scheduleReconnect();
    });
    websocket.addEventListener("error", () => websocket.close());
  }

  // 附件和输入状态。
  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.addEventListener("load", () => {
        const result = String(reader.result);
        resolve(result.slice(result.indexOf(",") + 1));
      });
      reader.addEventListener("error", () => reject(new Error("读取附件失败")));
      reader.readAsDataURL(file);
    });
  }

  function clearAttachment() {
    selectedFile = null;
    elements.attachmentInput.value = "";
    elements.attachmentPreview.hidden = true;
    updateComposer();
  }

  function enqueueTypingAction(type) {
    typingQueue = typingQueue
      .then(() =>
        requestJson("/admin/api/messages", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Admin-Token": adminToken,
          },
          body: JSON.stringify({ type }),
        }),
      )
      .catch(() => {
        if (type === "typing_start") {
          typingActive = false;
          typingLastRenewalAt = 0;
        }
      });
    return typingQueue;
  }

  function stopTypingStatus({ notifyServer = true } = {}) {
    window.clearTimeout(typingTimer);
    typingTimer = null;
    const wasActive = typingActive;
    typingActive = false;
    typingLastRenewalAt = 0;
    if (wasActive && notifyServer) enqueueTypingAction("typing_cancel");
  }

  function updateTypingStatus() {
    window.clearTimeout(typingTimer);
    typingTimer = null;
    if (!gatewayReady) {
      stopTypingStatus({ notifyServer: false });
      return;
    }
    if (!elements.messageInput.value) {
      stopTypingStatus();
      return;
    }
    if (!typingActive) {
      typingActive = true;
      typingLastRenewalAt = Date.now();
      enqueueTypingAction("typing_start");
    } else if (
      Date.now() - typingLastRenewalAt >=
      Math.max(250, typingTimeoutMilliseconds / 2)
    ) {
      // 节流刷新服务端租约；服务端不会为续租重复调用 iLink。
      typingLastRenewalAt = Date.now();
      enqueueTypingAction("typing_start");
    }
    typingTimer = window.setTimeout(stopTypingStatus, typingTimeoutMilliseconds);
  }

  function updateComposer() {
    const text = elements.messageInput.value;
    elements.characterCount.textContent = `${text.length} / ${messageMaxChars}`;
    elements.sendButton.disabled =
      !gatewayReady || sending || (!text.trim() && !selectedFile);
    elements.messageInput.disabled = !gatewayReady;
    elements.attachmentInput.disabled = !gatewayReady;
    elements.messageInput.style.height = "auto";
    elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 170)}px`;
  }

  async function sendMessage(event) {
    event.preventDefault();
    const text = elements.messageInput.value.trim();
    if (!gatewayReady || sending || (!text && !selectedFile)) return;

    sending = true;
    // 正文发送接口会在服务端取消租约，这里只清理本地计时，避免重复请求。
    stopTypingStatus({ notifyServer: false });
    updateComposer();
    const originalButtonLabel = elements.sendButton.querySelector("span").textContent;
    elements.sendButton.querySelector("span").textContent = "发送中";
    try {
      let body;
      if (selectedFile) {
        const content = await fileToBase64(selectedFile);
        body = selectedFile.type.startsWith("image/")
          ? { type: "image", content_base64: content, ...(text ? { caption: text } : {}) }
          : {
              type: "file",
              content_base64: content,
              file_name: selectedFile.name,
              ...(text ? { caption: text } : {}),
            };
      } else {
        body = { type: "text", text };
      }
      // 等待尚在途中的 typing_start 完成，确保随后正文请求的服务端取消不会被旧请求反向覆盖。
      await typingQueue;
      await requestJson("/admin/api/messages", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Admin-Token": adminToken,
        },
        body: JSON.stringify(body),
      });
      elements.messageInput.value = "";
      clearAttachment();
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      sending = false;
      elements.sendButton.querySelector("span").textContent = originalButtonLabel;
      updateComposer();
      elements.messageInput.focus();
    }
  }

  // 页面事件绑定与启动。
  elements.composer.addEventListener("submit", sendMessage);
  elements.messageInput.addEventListener("input", () => {
    updateComposer();
    updateTypingStatus();
  });
  elements.messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      elements.composer.requestSubmit();
    }
  });
  elements.attachmentInput.addEventListener("change", () => {
    const file = elements.attachmentInput.files?.[0];
    if (!file) return;
    if (file.size > mediaMaxBytes) {
      showToast(`附件不能超过 ${formatBytes(mediaMaxBytes)}`, "error");
      clearAttachment();
      return;
    }
    selectedFile = file;
    elements.attachmentName.textContent = file.name;
    elements.attachmentSize.textContent = formatBytes(file.size);
    elements.attachmentPreview.hidden = false;
    updateComposer();
  });
  elements.removeAttachment.addEventListener("click", clearAttachment);
  elements.loadOlder.addEventListener("click", async () => {
    elements.loadOlder.disabled = true;
    try {
      await loadMessages({ older: true });
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      elements.loadOlder.disabled = false;
    }
  });
  elements.refreshButton.addEventListener("click", async () => {
    if (!gatewayReady) return;
    elements.refreshButton.classList.add("loading");
    try {
      messages.clear();
      nextCursor = null;
      await Promise.all([loadMessages(), refreshStatus()]);
      showToast("消息已刷新");
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      elements.refreshButton.classList.remove("loading");
    }
  });
  elements.startLoginButton.addEventListener("click", requestLogin);
  elements.verificationForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = elements.verificationCode.value.trim();
    if (!code) return;
    const button = elements.verificationForm.querySelector("button");
    button.disabled = true;
    try {
      const status = await requestJson("/admin/api/login/verification", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Admin-Token": adminToken,
        },
        body: JSON.stringify({ code }),
      });
      elements.verificationCode.value = "";
      updateGateway(status);
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
  elements.loginQr.addEventListener("error", () => {
    elements.qrFrame.hidden = true;
    elements.loginProgressLabel.textContent = "二维码正在刷新…";
  });

  function confirmLogout(event) {
    const confirmed = window.confirm(
      "退出后将删除全部聊天记录、媒体、owner 上下文、游标和微信授权信息。确定继续吗？",
    );
    if (!confirmed) event.preventDefault();
  }

  elements.logoutForm.addEventListener("submit", confirmLogout);
  elements.loginResetForm.addEventListener("submit", confirmLogout);

  async function start() {
    connectWebSocket();
    try {
      await refreshStatus();
    } catch (error) {
      showToast(error.message, "error");
      elements.emptyState.hidden = false;
    }
    window.clearInterval(statusPollTimer);
    statusPollTimer = window.setInterval(
      () => refreshStatus({ quiet: true }),
      statusPollIntervalMilliseconds,
    );
    updateComposer();
  }

  start();
})();
