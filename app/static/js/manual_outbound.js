/* Shared manual channel selector for desktop and Prospecta Conversas. */
(() => {
  function setup(form) {
    const queue = form.querySelector('[name="queue_id"]');
    const internal = form.querySelector('[data-internal-template]');
    const meta = form.querySelector('[data-meta-template]');
    const select = meta?.querySelector('[name="meta_template_id"]');
    const parameters = meta?.querySelector('[data-meta-parameters]');
    const hidden = meta?.querySelector('[name="meta_parameters"]');
    const consent = meta?.querySelector('[name="meta_consent"]');
    const note = meta?.querySelector('[data-meta-note]');
    const preview = meta?.querySelector('[data-meta-preview]');
    if (!queue || !internal || !meta || !select || !parameters || !hidden || !consent || !note || !preview) return;
    let serial = 0, rows = [];
    function setSection(section, enabled) {
      section.hidden = !enabled;
      section.disabled = !enabled;
    }
    function values() {
      hidden.value = JSON.stringify(Array.from(parameters.querySelectorAll('input')).map(el => el.value.trim()));
      const row = rows.find(item => item.id === select.value);
      const vals = JSON.parse(hidden.value);
      preview.textContent = row ? row.body.replace(/\{\{([1-9][0-9]*)\}\}/g, (all, n) => vals[Number(n)-1] || all) : '';
    }
    function selected() {
      parameters.replaceChildren();
      const row = rows.find(item => item.id === select.value);
      consent.required = Boolean(row);
      consent.checked = false;
      if (row) for (const index of row.variables) {
        const label = document.createElement('label');
        label.textContent = 'Variável ' + index;
        const input = document.createElement('input');
        input.type = 'text'; input.required = true; input.maxLength = 1024;
        input.placeholder = 'Valor para o cliente';
        input.addEventListener('input', values);
        label.append(input); parameters.append(label);
      }
      values();
    }
    async function refresh() {
      const current = ++serial;
      const isMeta = queue.selectedOptions[0]?.dataset.channel === 'META';
      setSection(internal, !isMeta);
      setSection(meta, isMeta);
      if (!isMeta) {
        rows = []; select.replaceChildren(new Option('Selecione um template aprovado', ''));
        selected(); return;
      }
      const internalSelect = internal.querySelector('[name="template_id"]');
      if (internalSelect) internalSelect.value = '';
      rows = []; select.replaceChildren(new Option('Sem template — somente conversa ativa em 24h', ''));
      selected();
      note.textContent = 'Consultando templates oficiais...';
      try {
        const response = await fetch('/conversations/start/meta-templates?queue_id=' + encodeURIComponent(queue.value), {
          headers: {'Accept': 'application/json'}, credentials: 'same-origin', cache: 'no-store'
        });
        if (!response.ok) throw new Error('Consulta indisponível');
        const data = await response.json();
        if (current !== serial) return;
        rows = data.templates || [];
        for (const row of rows) select.add(new Option(row.name + ' · ' + row.language, row.id));
        note.textContent = rows.length ? 'Novo contato ou janela encerrada: escolha um template aprovado.' : 'Nenhum template aprovado compatível disponível. Consulte o gestor.';
      } catch (_) {
        if (current === serial) note.textContent = 'Não foi possível consultar a Meta. Troque a fila ou tente novamente.';
      }
    }
    queue.addEventListener('change', refresh);
    select.addEventListener('change', selected);
    form.addEventListener('submit', values, true);
    const dialog = form.closest('dialog');
    if (dialog) new MutationObserver(() => {
      if (dialog.open) refresh();
    }).observe(dialog, {attributes: true, attributeFilter: ['open']});
    setSection(internal, queue.selectedOptions[0]?.dataset.channel !== 'META');
    setSection(meta, queue.selectedOptions[0]?.dataset.channel === 'META');
  }
  function init() { document.querySelectorAll('[data-manual-channel-form]').forEach(setup); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
