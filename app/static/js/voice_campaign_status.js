/* Atualizacao em tempo real somente leitura: nunca aciona discagem. */
(() => {
  "use strict";
  const panels = Array.from(document.querySelectorAll("[data-voice-campaign]"));
  if (!panels.length) return;

  const formatter = new Intl.DateTimeFormat("pt-BR", {
    timeZone: "America/Sao_Paulo",
    dateStyle: "short",
    timeStyle: "medium"
  });
  let refreshing = false;

  function when(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : formatter.format(date);
  }

  function setText(root, selector, value) {
    const element = root.querySelector(selector);
    if (element) element.textContent = value == null ? "" : String(value);
  }

  function fillHistory(root, selector, items, type) {
    const target = root.querySelector(selector);
    if (!target) return;
    target.replaceChildren();
    if (!Array.isArray(items) || items.length === 0) {
      const empty = document.createElement("p");
      empty.className = "voice-help";
      empty.textContent = type === "attempt" ? "Nenhuma tentativa registrada." : "Nenhum evento SIP registrado.";
      target.appendChild(empty);
      return;
    }
    items.forEach((item) => {
      const line = document.createElement("p");
      line.className = "voice-event-line";
      const title = document.createElement("strong");
      title.textContent = type === "attempt"
        ? item.name || "Contato"
        : String(item.event || "").replaceAll("_", " ");
      line.appendChild(title);
      const suffix = item.phone_suffix || "****";
      const secondary = type === "attempt"
        ? " · final " + suffix + " · " + String(item.status || "").replaceAll("_", " ")
        : " · " + (item.contact || "Contato") + " · final " + suffix;
      line.appendChild(document.createTextNode(secondary + (when(item.at) ? " · " + when(item.at) : "")));
      if (item.detail) {
        const small = document.createElement("small");
        small.textContent = item.detail;
        line.appendChild(small);
      }
      target.appendChild(line);
    });
  }

  function fillReadiness(root, reasons) {
    const target = root.closest(".voice-panel")?.querySelector("[data-voice-readiness]");
    if (!target) return;
    target.replaceChildren();
    if (!Array.isArray(reasons) || reasons.length === 0) {
      const ready = document.createElement("span");
      ready.className = "voice-pill is-ready";
      ready.textContent = "Pré-requisitos verificados";
      target.appendChild(ready);
      return;
    }
    const ul = document.createElement("ul");
    reasons.forEach((reason) => {
      const li = document.createElement("li");
      li.textContent = reason;
      ul.appendChild(li);
    });
    target.appendChild(ul);
  }

  function render(panel, data) {
    if (panel.dataset.voiceStatusCode && panel.dataset.voiceStatusCode !== data.status) {
      // Recarrega apenas ao mudar de fase: exibe controles certos sem duplicar formulários.
      window.location.reload();
      return;
    }
    setText(panel, "[data-voice-headline]", data.headline);
    const command = data.last_command || {};
    setText(panel, "[data-voice-last-command]", command.label);
    setText(panel, "[data-voice-last-time]", when(command.at));
    setText(panel, "[data-voice-last-detail]", command.detail);
    Object.entries(data.stats || {}).forEach(([name, value]) => {
      setText(panel, '[data-voice-stat="' + name + '"]', value);
    });
    const empty = panel.querySelector("[data-voice-no-attempts]");
    if (empty) empty.hidden = Number(data.stats?.attempted || 0) > 0;
    fillHistory(panel, "[data-voice-attempts]", data.last_attempts, "attempt");
    fillHistory(panel, "[data-voice-events]", data.events, "event");
    fillReadiness(panel, data.readiness);
    setText(panel, "[data-voice-updated]", "Atualizado às " +
      formatter.format(new Date()) + " · atualização automática a cada 10 segundos");
  }

  async function refresh() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    try {
      const response = await fetch("/voice/campaigns/status", {
        method: "GET", credentials: "same-origin", cache: "no-store",
        headers: { Accept: "application/json" }
      });
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) {
        throw new Error("Status indisponível");
      }
      const result = await response.json();
      if (!result.campaigns) throw new Error("Resposta inválida");
      panels.forEach((panel) => {
        const data = result.campaigns[panel.dataset.voiceCampaign];
        if (data) render(panel, data);
      });
    } catch (_) {
      panels.forEach((panel) => setText(
        panel, "[data-voice-updated]",
        "Não foi possível atualizar agora. Os dados exibidos são os últimos recebidos."
      ));
    } finally {
      refreshing = false;
    }
  }

  document.querySelectorAll("[data-voice-refresh]").forEach((button) => {
    button.addEventListener("click", () => { void refresh(); });
  });
  window.setInterval(() => { void refresh(); }, 10000);
  void refresh();
})();
