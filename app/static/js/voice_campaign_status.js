/* Prospecta VOZ: linhas compactas; detalhes e eventos carregados sob demanda. */
(() => {
  "use strict";
  const items = Array.from(document.querySelectorAll("[data-voice-campaign-item]"));
  if (!items.length) return;
  const restoreKey = "prospecta-voz-open-campaign";
  const formatter = new Intl.DateTimeFormat("pt-BR", {
    timeZone: "America/Sao_Paulo",
    dateStyle: "short",
    timeStyle: "medium"
  });
  let refreshing = false;

  function remember(id) {
    try { sessionStorage.setItem(restoreKey, String(id)); } catch (_) { /* private mode */ }
  }
  function rememberAndReload(id) {
    remember(id);
    window.location.reload();
  }
  function when(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : formatter.format(date);
  }
  function setText(root, selector, value) {
    const element = root.querySelector(selector);
    if (element) element.textContent = value == null ? "" : String(value);
  }
  function openedItem() {
    return items.find((item) =>
      item.querySelector("[data-voice-campaign-toggle]")?.getAttribute("aria-expanded") === "true"
    );
  }

  function close(item) {
    const button = item.querySelector("[data-voice-campaign-toggle]");
    const target = item.querySelector("[data-voice-campaign-details]");
    if (button) button.setAttribute("aria-expanded", "false");
    if (target) target.hidden = true;
    item.classList.remove("is-expanded");
  }

  async function open(item) {
    const id = item.dataset.campaignId;
    const button = item.querySelector("[data-voice-campaign-toggle]");
    const target = item.querySelector("[data-voice-campaign-details]");
    if (!id || !button || !target) return;
    for (const other of items) if (other !== item) close(other);
    button.setAttribute("aria-expanded", "true");
    target.hidden = false;
    item.classList.add("is-expanded");
    if (target.dataset.loaded === "true") {
      void refresh();
      return;
    }
    if (target.dataset.loading === "true") return;
    target.dataset.loading = "true";
    target.replaceChildren();
    const loading = document.createElement("p");
    loading.className = "voice-help";
    loading.setAttribute("role", "status");
    loading.textContent = "Carregando detalhes da campanha...";
    target.appendChild(loading);
    try {
      const response = await fetch("/voice/campaigns/" + encodeURIComponent(id) + "/details", {
        method: "GET", credentials: "same-origin", cache: "no-store",
        headers: { Accept: "text/html" }
      });
      const contentType = response.headers.get("content-type") || "";
      if (!response.ok || !contentType.includes("text/html") || response.redirected) {
        throw new Error("Acesso ou detalhes indisponíveis");
      }
      const html = await response.text();
      if (!html.includes('data-voice-campaign="' + id + '"')) {
        throw new Error("Detalhes inválidos");
      }
      // Fragmento HTML de rota autenticada do mesmo servidor, escapado por Jinja2.
      target.innerHTML = html;
      target.dataset.loaded = "true";
      if (!target.hidden) void refresh();
    } catch (_) {
      target.replaceChildren();
      const message = document.createElement("p");
      message.className = "voice-start-warning";
      message.setAttribute("role", "alert");
      message.textContent = "Não foi possível abrir a campanha. Feche e clique novamente para tentar.";
      target.appendChild(message);
    } finally {
      target.dataset.loading = "false";
    }
  }

  function fillHistory(root, selector, entries, type) {
    const target = root.querySelector(selector);
    if (!target) return;
    target.replaceChildren();
    if (!Array.isArray(entries) || !entries.length) {
      const empty = document.createElement("p");
      empty.className = "voice-help";
      empty.textContent = type === "attempt" ? "Nenhuma tentativa registrada." : "Nenhum evento SIP registrado.";
      target.appendChild(empty);
      return;
    }
    entries.forEach((entry) => {
      const line = document.createElement("p");
      line.className = "voice-event-line";
      const title = document.createElement("strong");
      title.textContent = type === "attempt"
        ? entry.name || "Contato" : String(entry.event || "").replaceAll("_", " ");
      line.appendChild(title);
      const suffix = entry.phone_suffix || "****";
      const secondary = type === "attempt"
        ? " · final " + suffix + " · " + String(entry.status || "").replaceAll("_", " ")
        : " · " + (entry.contact || "Contato") + " · final " + suffix;
      line.appendChild(document.createTextNode(
        secondary + (when(entry.at) ? " · " + when(entry.at) : "")
      ));
      if (entry.detail) {
        const small = document.createElement("small");
        small.textContent = entry.detail;
        line.appendChild(small);
      }
      target.appendChild(line);
    });
  }

  function fillReadiness(root, reasons) {
    const target = root.closest("[data-voice-campaign-details]")?.querySelector("[data-voice-readiness]");
    if (!target) return;
    target.replaceChildren();
    if (!Array.isArray(reasons) || !reasons.length) {
      const ready = document.createElement("span");
      ready.className = "voice-pill is-ready";
      ready.textContent = "Pré-requisitos verificados";
      target.appendChild(ready);
      return;
    }
    const ul = document.createElement("ul");
    for (const reason of reasons) {
      const li = document.createElement("li");
      li.textContent = reason;
      ul.appendChild(li);
    }
    target.appendChild(ul);
  }

  function render(panel, data, item) {
    if (panel.dataset.voiceStatusCode && panel.dataset.voiceStatusCode !== data.status) {
      // Recarrega somente quando a fase mudar, mantendo a campanha aberta.
      rememberAndReload(item.dataset.campaignId);
      return;
    }
    setText(panel, "[data-voice-headline]", data.headline);
    const command = data.last_command || {};
    setText(panel, "[data-voice-last-command]", command.label);
    setText(panel, "[data-voice-last-time]", when(command.at));
    setText(panel, "[data-voice-last-detail]", command.detail);
    for (const [name, value] of Object.entries(data.stats || {})) {
      setText(panel, '[data-voice-stat="' + name + '"]', value);
    }
    const empty = panel.querySelector("[data-voice-no-attempts]");
    if (empty) empty.hidden = Number(data.stats?.attempted || 0) > 0;
    fillHistory(panel, "[data-voice-attempts]", data.last_attempts, "attempt");
    fillHistory(panel, "[data-voice-events]", data.events, "event");
    fillReadiness(panel, data.readiness);
    setText(panel, "[data-voice-updated]",
      "Atualizado às " + formatter.format(new Date()) + " · atualização automática a cada 10 segundos");
  }

  async function refresh() {
    const item = openedItem();
    const panel = item?.querySelector("[data-voice-campaign]");
    if (refreshing || document.hidden || !panel) return;
    refreshing = true;
    try {
      const id = item.dataset.campaignId;
      const response = await fetch(
        "/voice/campaigns/status?campaign_id=" + encodeURIComponent(id), {
          method: "GET", credentials: "same-origin", cache: "no-store",
          headers: { Accept: "application/json" }
        });
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) {
        throw new Error("Status indisponível");
      }
      const result = await response.json();
      const data = result.campaigns?.[id];
      if (!data) throw new Error("Campanha indisponível");
      if (openedItem() === item) render(panel, data, item);
    } catch (_) {
      if (openedItem() === item) setText(
        panel, "[data-voice-updated]",
        "Não foi possível atualizar agora. Os dados exibidos são os últimos recebidos."
      );
    } finally {
      refreshing = false;
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-voice-campaign-toggle]");
    if (button) {
      const item = button.closest("[data-voice-campaign-item]");
      if (!item) return;
      if (button.getAttribute("aria-expanded") === "true") close(item);
      else void open(item);
      return;
    }
    if (event.target.closest("[data-voice-refresh]")) void refresh();
  });

  document.addEventListener("submit", (event) => {
    const item = event.target.closest("[data-voice-campaign-item]");
    if (item && item.dataset.campaignId) remember(item.dataset.campaignId);
  });

  let restored = null;
  try {
    restored = sessionStorage.getItem(restoreKey);
    sessionStorage.removeItem(restoreKey);
  } catch (_) { /* private mode */ }
  if (restored) {
    const match = items.find((item) => item.dataset.campaignId === restored);
    if (match) void open(match);
  }
  window.setInterval(() => { void refresh(); }, 10000);
})();
