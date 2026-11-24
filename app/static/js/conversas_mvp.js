/* Prospecta Conversas: interface web independente sobre as APIs autorizadas do inbox. */
(() => {
  "use strict";
  const root = document.getElementById("mvp-app");
  if (!root) return;

  const $ = id => document.getElementById(id);
  const ui = {
    list: $("mvp-conversation-list"), listState: $("mvp-inbox-state"),
    unread: $("mvp-unread-count"), search: $("mvp-search"),
    join: $("mvp-join"), presence: $("mvp-presence-label"),
    dot: $("mvp-presence-dot"), back: $("mvp-back"),
    name: $("mvp-chat-name"), meta: $("mvp-chat-meta"),
    avatar: $("mvp-chat-avatar"), chatStatus: $("mvp-chat-status"),
    placeholder: $("mvp-chat-placeholder"), messages: $("mvp-messages"),
    compose: $("mvp-compose"), hint: $("mvp-compose-hint"),
    message: $("mvp-message"), send: $("mvp-send"),
    feedback: $("mvp-chat-feedback"),
    file: $("mvp-attachment"), filePreview: $("mvp-file-preview"),
    fileName: $("mvp-file-name"), fileSend: $("mvp-file-send"),
    fileCancel: $("mvp-file-cancel"), claim: $("mvp-claim"),
    newOpen: $("mvp-new-open"), newDialog: $("mvp-new-dialog"),
    newForm: $("mvp-new-form"), newClose: $("mvp-new-close"),
    newPhone: $("mvp-new-phone"), newQueue: $("mvp-new-queue"),
    newError: $("mvp-new-error"), newSubmit: $("mvp-new-submit")
  };
  const state = {
    csrf: root.dataset.csrf,
    userId: Number(root.dataset.userId || 0),
    role: root.dataset.role,
    canReply: root.dataset.canReply === "true",
    canTransfer: root.dataset.canTransfer === "true",
    selectedId: Number(root.dataset.initialId || 0),
    view: "active",
    query: "",
    items: [],
    online: false,
    signature: "",
    messageLoading: false,
    inboxLoading: false,
    requestSequence: 0,
    sending: false,
    fileSending: false
  };

  function initials(name) {
    return (name || "?").trim().split(/\s+/).slice(0, 2)
      .map(part => part.slice(0, 1).toUpperCase()).join("") || "?";
  }
  function dateOf(value) {
    if (!value) return null;
    const normalized = /[zZ]$|[+-]\d{2}:\d{2}$/.test(value) ? value : value + "Z";
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  function hour(value) {
    const date = dateOf(value);
    return date ? date.toLocaleTimeString("pt-BR", {hour: "2-digit", minute: "2-digit"}) : "";
  }
  function el(tag, className, textContent) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (textContent !== undefined) node.textContent = String(textContent);
    return node;
  }
  function feedback(message) {
    ui.feedback.textContent = message || "";
    ui.feedback.hidden = !message;
  }
  function failure(error) {
    if (error && error.code === "UNAUTHORIZED") {
      window.location.assign("/login");
      return;
    }
    feedback(error && error.message ? error.message : "Não foi possível concluir a operação.");
  }
  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      headers: {Accept: "application/json", ...(options.headers || {})},
      ...options
    });
    const kind = response.headers.get("content-type") || "";
    if (response.redirected && /\/(login|mfa\/verify|change-password)(\?|$)/.test(new URL(response.url).pathname)) {
      const error = new Error("Sessão expirada");
      error.code = "UNAUTHORIZED";
      throw error;
    }
    if (!kind.includes("application/json")) {
      throw new Error("Resposta inesperada. Reabra o atendimento e tente novamente.");
    }
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {
      const error = new Error(payload.error || payload.detail || "O atendimento não está disponível.");
      error.status = response.status;
      if (response.status === 401) error.code = "UNAUTHORIZED";
      throw error;
    }
    return payload;
  }
  function postData(values) {
    return new URLSearchParams({csrf_token: state.csrf, ...values});
  }
  function renderPresence() {
    ui.dot.classList.toggle("online", state.online);
    ui.presence.textContent = state.online ? "Você está disponível para atendimentos" : "Atendimento offline";
    ui.join.hidden = state.online || state.role !== "CONSULTOR";
    if (!state.online && state.role === "CONSULTOR") {
      ui.listState.textContent = "Entre no atendimento para acessar suas conversas.";
    }
  }
  async function checkPresence() {
    try {
      const data = await api("/api/whatsapp-presence");
      state.online = !!data.online;
      renderPresence();
    } catch (err) {
      failure(err);
    }
  }
  async function joinPresence() {
    ui.join.disabled = true;
    try {
      const data = await api("/api/whatsapp-presence/join", {
        method: "POST", body: postData({})
      });
      state.online = !!data.online;
      renderPresence();
      await refreshInbox();
      if (state.selectedId) await refreshMessages();
    } catch (err) {
      failure(err);
    } finally {
      ui.join.disabled = false;
    }
  }
  async function heartbeat() {
    if (!state.online || document.hidden) return;
    try {
      const data = await api("/api/whatsapp-presence/heartbeat", {
        method: "POST", body: postData({})
      });
      state.online = !!data.online;
      renderPresence();
    } catch (err) {
      state.online = false;
      renderPresence();
      failure(err);
    }
  }
  function renderFilters() {
    document.querySelectorAll("[data-view]").forEach(button => {
      const selected = button.dataset.view === state.view;
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }
  function renderList() {
    const nodes = document.createDocumentFragment();
    if (!state.items.length) {
      nodes.append(el("div", "mvp-no-results", "Nenhuma conversa neste filtro."));
    }
    state.items.forEach(item => {
      const row = el("button", "mvp-contact");
      row.type = "button";
      row.dataset.id = String(item.id);
      row.classList.toggle("selected", item.id === state.selectedId);
      const avatar = el("span", "mvp-contact-avatar", initials(item.name));
      avatar.setAttribute("aria-hidden", "true");
      const info = el("span", "mvp-contact-info");
      const top = el("span", "mvp-contact-top");
      top.append(el("span", "mvp-contact-name", item.name || item.phone || "Contato"));
      top.append(el("time", "mvp-contact-time", hour(item.last_message_at)));
      const bottom = el("span", "mvp-contact-bottom");
      bottom.append(el("span", "mvp-contact-preview", item.preview || "Conversa iniciada"));
      if (item.unread_count > 0) {
        bottom.append(el("span", "mvp-unread-bubble", item.unread_count > 99 ? "99+" : item.unread_count));
      }
      info.append(top, bottom, el("span", "mvp-contact-sub", item.queue_name || item.status || ""));
      row.append(avatar, info);
      row.setAttribute("aria-label", (item.name || item.phone || "Contato") +
        (item.unread_count ? ", " + item.unread_count + " não lidas" : ""));
      nodes.append(row);
    });
    ui.list.replaceChildren(nodes);
  }
  async function refreshInbox() {
    if (document.hidden) return;
    if (!state.online && state.role === "CONSULTOR") return;
    const sequence = ++state.requestSequence;
    if (state.inboxLoading) return;
    state.inboxLoading = true;
    try {
      const params = new URLSearchParams({view: state.view});
      if (state.query.trim()) params.set("q", state.query.trim());
      const data = await api("/api/conversations/inbox?" + params.toString());
      if (sequence !== state.requestSequence) return;
      state.items = data.items || [];
      ui.unread.textContent = data.counts && data.counts.unread ? "(" + data.counts.unread + ")" : "";
      document.title = data.counts && data.counts.unread
        ? "(" + data.counts.unread + ") Prospecta Conversas"
        : "Prospecta Conversas · Sulnet";
      ui.listState.textContent = state.items.length + " atendimento(s) no filtro";
      renderList();
      renderChatHeader();
    } catch (err) {
      if (err.status === 403 && state.role === "CONSULTOR") {
        state.online = false;
        renderPresence();
      } else {
        failure(err);
        ui.listState.textContent = "Não foi possível atualizar as conversas.";
      }
    } finally {
      state.inboxLoading = false;
    }
  }
  function selectedItem() {
    return state.items.find(item => item.id === state.selectedId);
  }
  function renderChatHeader() {
    const item = selectedItem();
    if (!item) return;
    ui.name.textContent = item.name || item.phone || "Contato";
    ui.avatar.textContent = initials(item.name || item.phone);
    ui.meta.textContent = [item.phone, item.queue_name].filter(Boolean).join(" · ");
    ui.chatStatus.textContent = item.status ? item.status.replaceAll("_", " ").toLowerCase() : "";
    const unassignedSeller = state.role === "CONSULTOR" && item.owner === "Sem responsável";
    if (ui.claim) ui.claim.hidden = !(unassignedSeller && state.canTransfer);
    const allowedToSend = state.canReply && !unassignedSeller;
    ui.compose.hidden = !allowedToSend;
    ui.hint.hidden = allowedToSend;
    ui.hint.textContent = unassignedSeller
      ? "Este atendimento ainda está na fila. Assuma a conversa para responder."
      : "Você pode consultar este atendimento, mas não possui permissão para responder.";
  }
  function clearChat(message = "") {
    state.selectedId = 0;
    state.signature = "";
    resetFile();
    ui.messages.replaceChildren();
    ui.messages.hidden = true;
    ui.placeholder.hidden = false;
    ui.compose.hidden = true;
    ui.hint.hidden = true;
    if (ui.claim) ui.claim.hidden = true;
    ui.name.textContent = "Selecione uma conversa";
    ui.meta.textContent = "Atendimentos em tempo real";
    ui.chatStatus.textContent = "";
    ui.avatar.textContent = "•";
    document.body.classList.remove("mvp-chat-open");
    window.history.replaceState({}, "", "/conversas");
    renderList();
    feedback(message);
  }
  function openChat(id) {
    if (!Number.isInteger(id) || id <= 0) return;
    state.selectedId = id;
    state.signature = "";
    ui.messages.replaceChildren();
    ui.messages.hidden = false;
    ui.placeholder.hidden = true;
    document.body.classList.add("mvp-chat-open");
    window.history.replaceState({}, "", "/conversas/" + id);
    feedback("");
    resetFile();
    renderList();
    renderChatHeader();
    refreshMessages();
  }
  function createMessage(item) {
    const direction = item.direction === "OUTBOUND" ? "outbound" : item.direction === "INBOUND" ? "inbound" : "internal";
    const row = el("div", "mvp-message-row " + direction);
    const bubble = el("div", "mvp-message-bubble");
    if (direction !== "outbound") {
      bubble.append(el("span", "mvp-message-sender", direction === "internal" ? "Nota interna" : (item.sender || "Cliente")));
    }
    const mediaUrl = /^\/conversations\/messages\/\d+\/media$/.test(item.media_url || "") ? item.media_url : "";
    const type = String(item.type || "").toUpperCase();
    if (mediaUrl && type === "IMAGE") {
      const img = el("img", "mvp-message-media");
      img.src = mediaUrl;
      img.loading = "lazy";
      img.alt = "Imagem da conversa";
      bubble.append(img);
    } else if (mediaUrl && type === "VIDEO") {
      const video = el("video", "mvp-message-media");
      video.src = mediaUrl;
      video.controls = true;
      video.preload = "none";
      bubble.append(video);
    } else if (mediaUrl && type === "AUDIO") {
      const audio = el("audio", "mvp-message-media");
      audio.src = mediaUrl;
      audio.controls = true;
      audio.preload = "none";
      audio.setAttribute("aria-label", "Áudio da conversa");
      bubble.append(audio);
    } else if (mediaUrl) {
      const link = el("a", "mvp-message-file", "Abrir documento");
      link.href = mediaUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      bubble.append(link);
    }
    const messageText = (item.body || "").trim();
    if (messageText && (!mediaUrl || !/^\[(imagem|video|áudio|audio)\]$/i.test(messageText))) {
      bubble.append(el("p", "mvp-message-content", messageText));
    }
    const foot = el("div", "mvp-message-foot");
    foot.append(el("time", "", hour(item.created_at)));
    if (direction === "outbound") {
      const status = String(item.status || "").toUpperCase();
      const indicator = status === "READ" ? "✓✓ Lida"
        : status === "DELIVERED" ? "✓✓ Entregue"
        : status === "FAILED" ? "Falha"
        : status === "PENDING" ? "Pendente" : "✓ Enviada";
      foot.append(el("span", "", indicator));
    }
    bubble.append(foot);
    row.append(bubble);
    return row;
  }
  async function refreshMessages() {
    const conversationId = state.selectedId;
    if (!conversationId || state.messageLoading || document.hidden) return;
    if (!state.online && state.role === "CONSULTOR") return;
    state.messageLoading = true;
    try {
      // Releitura completa para que a politica de corte apos transferencia
      // seja aplicada mesmo com o chat ja aberto no navegador.
      const data = await api("/api/conversations/" + conversationId + "/messages?after_id=0");
      if (conversationId !== state.selectedId) return;
      const items = data.items || [];
      const signature = items.map(item => item.id + ":" + item.status).join("|");
      if (signature !== state.signature) {
        const nearBottom = ui.messages.scrollHeight - ui.messages.scrollTop - ui.messages.clientHeight < 120;
        const firstRender = !state.signature;
        const fragment = document.createDocumentFragment();
        if (!items.length) fragment.append(el("div", "mvp-no-results", "Nenhuma mensagem disponível nesta etapa do atendimento."));
        else items.forEach(item => fragment.append(createMessage(item)));
        ui.messages.replaceChildren(fragment);
        state.signature = signature;
        if (nearBottom || firstRender) ui.messages.scrollTop = ui.messages.scrollHeight;
      }
    } catch (err) {
      if (conversationId !== state.selectedId) return;
      if (err.status === 404 || err.status === 403) {
        clearChat("Este atendimento não está mais disponível para sua conta.");
        await refreshInbox();
      } else failure(err);
    } finally {
      state.messageLoading = false;
    }
  }
  async function sendText(event) {
    event.preventDefault();
    const message = ui.message.value.trim();
    if (!message || !state.selectedId || state.sending || !state.canReply) return;
    if (state.role === "CONSULTOR" && selectedItem()?.owner === "Sem responsável") {
      feedback("Assuma o atendimento antes de responder.");
      return;
    }
    state.sending = true;
    ui.send.disabled = true;
    feedback("");
    try {
      await api("/conversations/" + state.selectedId + "/send", {
        method: "POST", body: postData({message})
      });
      ui.message.value = "";
      await refreshMessages();
      await refreshInbox();
    } catch (err) {
      failure(err);
    } finally {
      state.sending = false;
      ui.send.disabled = false;
    }
  }
  function resetFile() {
    ui.file.value = "";
    ui.filePreview.hidden = true;
  }
  async function sendFile() {
    const file = ui.file.files && ui.file.files[0];
    if (!file || !state.selectedId || state.fileSending || !state.canReply) return;
    if (state.role === "CONSULTOR" && selectedItem()?.owner === "Sem responsável") {
      feedback("Assuma o atendimento antes de enviar anexos.");
      return;
    }
    state.fileSending = true;
    ui.fileSend.disabled = true;
    feedback("");
    try {
      const data = new FormData();
      data.append("csrf_token", state.csrf);
      data.append("media", file, file.name);
      await api("/conversations/" + state.selectedId + "/media", {method: "POST", body: data});
      resetFile();
      await refreshMessages();
      await refreshInbox();
    } catch (err) {
      failure(err);
    } finally {
      state.fileSending = false;
      ui.fileSend.disabled = false;
    }
  }
  async function claim() {
    const id = state.selectedId;
    if (!id || !state.canTransfer || state.role !== "CONSULTOR") return;
    ui.claim.disabled = true;
    try {
      const response = await fetch("/conversations/" + id + "/assign", {
        method: "POST", credentials: "same-origin", redirect: "follow",
        body: postData({assigned_user_id: String(state.userId)})
      });
      if (!response.ok || !response.redirected || !new URL(response.url).pathname.startsWith("/conversations")) {
        throw new Error("Não foi possível assumir esta conversa.");
      }
      state.view = "active";
      renderFilters();
      await refreshInbox();
      renderChatHeader();
      await refreshMessages();
    } catch (err) {
      failure(err);
    } finally {
      ui.claim.disabled = false;
    }
  }

  function newConversationError(message) {
    if (!ui.newError) return;
    ui.newError.textContent = message || "";
    ui.newError.hidden = !message;
  }
  function closeNewConversation() {
    if (ui.newDialog?.open) ui.newDialog.close();
    newConversationError("");
  }
  async function createNewConversation(event) {
    event.preventDefault();
    if (!ui.newForm || !ui.newPhone || !ui.newQueue || !state.canReply) return;
    newConversationError("");
    const rawPhone = ui.newPhone.value.trim();
    const digits = rawPhone.replace(/\\D/g, "");
    const validLength = digits.startsWith("55") ? [12, 13].includes(digits.length) : [10, 11].includes(digits.length);
    if (!validLength) {
      newConversationError("Informe um número de WhatsApp válido com DDD.");
      return;
    }
    if (!ui.newQueue.value) {
      newConversationError("Selecione uma fila autorizada para seu atendimento.");
      return;
    }
    if (state.role === "CONSULTOR" && !state.online) {
      newConversationError("Entre no atendimento antes de iniciar uma nova conversa.");
      return;
    }
    ui.newSubmit.disabled = true;
    try {
      const result = await api("/conversations/start", {
        method: "POST",
        body: postData({phone: rawPhone, queue_id: ui.newQueue.value})
      });
      if (!Number.isInteger(Number(result.conversation_id)) || Number(result.conversation_id) <= 0) {
        throw new Error("O servidor não retornou a conversa criada.");
      }
      ui.newForm.reset();
      closeNewConversation();
      ui.search.value = "";
      state.query = "";
      state.view = "active";
      renderFilters();
      await refreshInbox();
      openChat(Number(result.conversation_id));
      if (result.notice) feedback(result.notice);
      ui.message.focus();
    } catch (error) {
      newConversationError(error?.message || "Não foi possível abrir a conversa.");
    } finally {
      ui.newSubmit.disabled = false;
    }
  }

  if (ui.newOpen) ui.newOpen.addEventListener("click", () => {
    newConversationError("");
    if (state.role === "CONSULTOR" && !state.online) {
      feedback("Entre no atendimento para iniciar novas conversas.");
      return;
    }
    if (ui.newDialog?.showModal) {
      ui.newDialog.showModal();
      ui.newPhone?.focus();
    }
  });
  ui.newClose?.addEventListener("click", closeNewConversation);
  ui.newForm?.addEventListener("submit", createNewConversation);
  ui.join.addEventListener("click", joinPresence);
  ui.back.addEventListener("click", () => clearChat());
  ui.list.addEventListener("click", event => {
    const row = event.target.closest("[data-id]");
    if (row) openChat(Number(row.dataset.id));
  });
  ui.compose.addEventListener("submit", sendText);
  ui.message.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      ui.compose.requestSubmit();
    }
  });
  ui.file.addEventListener("change", () => {
    const file = ui.file.files && ui.file.files[0];
    ui.filePreview.hidden = !file;
    ui.fileName.textContent = file ? file.name : "";
  });
  ui.fileCancel.addEventListener("click", resetFile);
  ui.fileSend.addEventListener("click", sendFile);
  if (ui.claim) ui.claim.addEventListener("click", claim);
  document.querySelectorAll("[data-view]").forEach(button => {
    button.addEventListener("click", () => {
      state.view = button.dataset.view;
      renderFilters();
      refreshInbox();
    });
  });
  let searchDebounce = 0;
  ui.search.addEventListener("input", () => {
    window.clearTimeout(searchDebounce);
    searchDebounce = window.setTimeout(() => {
      state.query = ui.search.value;
      refreshInbox();
    }, 300);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      checkPresence().then(() => {
        heartbeat();
        refreshInbox();
        refreshMessages();
      });
    }
  });
  window.addEventListener("online", () => { checkPresence(); refreshInbox(); refreshMessages(); });

  async function start() {
    renderFilters();
    await checkPresence();
    await refreshInbox();
    if (state.selectedId) openChat(state.selectedId);
  }
  start();
  window.setInterval(refreshInbox, 4000);
  window.setInterval(refreshMessages, 4500);
  window.setInterval(heartbeat, 25000);
})();
