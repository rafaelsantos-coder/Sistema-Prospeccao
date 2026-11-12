/* Sulnet Prospecta — inteligência territorial sem exposição de dados pessoais. */
(() => {
  'use strict';
  const root = document.getElementById('credit-map-app');
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Number(n || 0).toLocaleString('pt-BR');
  const pct = (n) => n == null ? '—' : Number(n).toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + '%';
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
  const folded = (s) => String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toUpperCase().trim();
  const monthLabel = (m) => { const [y, mm] = (m || '').split('-'); const months=['jan','fev','mar','abr','mai','jun','jul','ago','set','out','nov','dez']; return (months[Number(mm)-1] || mm) + '/' + y?.slice(2); };
  const metrics = {
    approved: {label: 'Aprovados', unit:'', short:'aprovações'},
    approval_rate: {label: 'Aprovação', unit:'%', short:'taxa de aprovação'},
    rejected: {label: 'Reprovados', unit:'', short:'reprovações'},
    signed_approved: {label: 'Assinados', unit:'', short:'contratos assinados'},
    signed_rate: {label: 'Conversão', unit:'%', short:'conversão em contrato'},
    total: {label: 'Consultas', unit:'', short:'consultas'},
  };
  const state = {data: null, map: null, layers: [], positions: new Map(), mapReady: false, mapLoading:false, revision:0};
  const metric = () => $('cm-metric').value;
  const metricValue = (row) => row[metric()] == null ? 0 : row[metric()];
  const metricDisplay = (row) => ['approval_rate','signed_rate'].includes(metric()) ? pct(row[metric()]) : fmt(row[metric()]);
  const approvalColor = (rate) => rate >= 83 ? '#16a66f' : rate >= 70 ? '#e5a238' : '#e35b53';
  const layerColor = (row) => metric() === 'rejected' ? '#dc655c' : ['approved','approval_rate','signed_approved','signed_rate'].includes(metric()) ? approvalColor(row.approval_rate || 0) : '#f36f21';
  const showMessage = (message) => { const el = $('cm-message'); el.textContent = message; el.hidden = !message; };
  const showMapPlaceholder = (message) => { $('cm-map-empty').hidden = false; if(message) $('cm-map-empty').querySelector('p').textContent = message; };
  const sync = (message) => { $('cm-sync').innerHTML = `<span></span>${esc(message)}`; };

  function sortedRows() {
    const rows = [...(state.data?.neighborhoods || [])];
    const q = folded($('cm-search').value);
    const selected = metric();
    rows.sort((a,b) => (Number(b[selected] || 0) - Number(a[selected] || 0)) || (b.total - a.total) || a.name.localeCompare(b.name));
    return rows.filter(row => !q || folded(row.name).includes(q));
  }
  function drawRanking() {
    const rows = sortedRows(); const total = rows.length;
    const max = Math.max(1, ...rows.map(x => Number(metricValue(x) || 0)));
    $('cm-rank-col').textContent = metrics[metric()]?.label.toUpperCase() || 'RESULTADO';
    $('cm-ranking').innerHTML = total ? rows.map((row,i) => {
      const progress = Math.max(3, Math.round(Number(metricValue(row) || 0) / max * 100));
      return `<button class="cm-rank-row" type="button" data-bairro="${esc(row.name)}" title="Localizar ${esc(row.name)} no mapa"><span class="cm-rank-pos">${String(i+1).padStart(2,'0')}</span><span><span class="cm-rank-name">${esc(row.name)}</span><span class="cm-rank-sub"><span class="cm-rank-track"><span class="cm-rank-progress" style="display:block;width:${progress}%;background:${layerColor(row)}"></span></span><span class="cm-rank-detail">${fmt(row.total)} cons.</span></span></span><span class="cm-rank-value">${metricDisplay(row)}<small>${metric()==='approval_rate' ? fmt(row.approved)+' aprov.' : pct(row.approval_rate)+' aprov.'}</small></span></button>`;
    }).join('') : '<div class="cm-loading">Nenhum bairro encontrado neste filtro.</div>';
    $('cm-rank-foot').textContent = `${fmt(total)} bairros · ${fmt(state.data?.suppressed_neighborhoods || 0)} pequenos/sem bairro não detalhados`;
    $('cm-ranking').querySelectorAll('button[data-bairro]').forEach(button => button.addEventListener('click', () => locateRow(button.dataset.bairro)));
  }
  function drawMonths() {
    const months = state.data?.months || [];
    const max = Math.max(1, ...months.map(m => m.total));
    $('cm-months').innerHTML = months.length ? months.map(m => {
      const app = (100 * m.approved / max).toFixed(1);
      const rest = (100 * (m.total - m.approved) / max).toFixed(1);
      return `<div class="cm-month-line"><span>${esc(monthLabel(m.month))}</span><span class="cm-month-bars" title="${fmt(m.total)} consultas, ${fmt(m.approved)} aprovados"><span class="cm-month-approved" style="width:${app}%"></span><span class="cm-month-rejected" style="width:${rest}%"></span></span><span>${fmt(m.total)}</span></div>`;
    }).join('') : '<p class="cm-loading">Sem consultas no período.</p>';
  }
  function drawKPIs() {
    const t = state.data?.totals || {};
    $('cm-kpi-total').textContent = fmt(t.total);
    $('cm-kpi-approved').textContent = fmt(t.approved);
    $('cm-kpi-rejected').textContent = fmt(t.rejected);
    $('cm-kpi-signed').textContent = fmt(t.signed_approved);
    $('cm-kpi-approved-rate').textContent = pct(t.approval_rate);
    $('cm-kpi-signed-rate').textContent = pct(t.signed_rate);
    const rows = state.data?.neighborhoods || [];
    if(!t.total) {
      $('cm-insight').textContent = 'Não há dados de CPF neste período. Importe o CSV completo para iniciar sua análise.';
    } else {
      const top = [...rows].sort((a,b)=>b.approved-a.approved)[0];
      const note = top ? `O bairro ${top.name} concentrou ${fmt(top.approved)} aprovações em ${fmt(top.total)} consultas (${pct(top.approval_rate)}). ` : '';
      $('cm-insight').textContent = `${note}No total, ${fmt(t.signed_approved)} de ${fmt(t.approved)} aprovações estão marcadas como contratos assinados (${pct(t.signed_rate)}). Use estes dados para organizar ações comerciais e avaliar perdas no funil, não para definir crédito individual.`;
    }
    const warnings = [];
    if(t.signed_rejected_inconsistent) warnings.push(`${fmt(t.signed_rejected_inconsistent)} registros reprovados estão marcados como assinados (verificar no sistema).`);
    if(t.pending) warnings.push(`${fmt(t.pending)} registro(s) ainda sem decisão.`);
    showMessage(warnings.join(' '));
    $('cm-map-title').textContent = `Intensidade de ${metrics[metric()]?.short || 'consultas'}`;
  }
  function drawAll() { drawKPIs(); drawRanking(); drawMonths(); refreshMap(); }

  async function fetchData() {
    const city = $('cm-city').value;
    const from = $('cm-from').value, to = $('cm-to').value;
    if(!from || !to || from > to) { showMessage('Selecione um intervalo de meses válido.'); return; }
    $('cm-apply').disabled = true; sync('Atualizando indicadores...');
    try {
      let data;
      if(root.dataset.preview === 'true') {
        const element = document.getElementById('cm-preview-data');
        if (!element) throw new Error('Prévia local sem base agregada');
        // Prévia contém apenas dados agrupados de um período fechado.
        data = JSON.parse(element.textContent);
      } else {
        const query = new URLSearchParams({city, date_from:from, date_to:to});
        const r = await fetch('/api/reports/credit-map?' + query, {credentials:'same-origin',cache:'no-store'});
        if(r.status === 401) {window.location.assign('/login');return;}
        const payload = await r.json();
        if(!r.ok) throw new Error(payload.detail || 'Falha ao carregar a base');
        data = payload;
      }
      state.data = data; state.revision += 1; sync('Dados agregados carregados'); drawAll();
    } catch(err) { showMessage('Não foi possível carregar os indicadores: ' + err.message); sync('Erro ao carregar'); }
    finally { $('cm-apply').disabled = false; }
  }
  async function fetchCities() {
    if(root.dataset.preview === 'true') return;
    try {
      const r = await fetch('/api/reports/credit-map/cities', {credentials:'same-origin'});
      if (!r.ok) return;
      const cities = (await r.json()).cities || [];
      const el = $('cm-city');const prev = el.value;
      if (!cities.length) return;
      el.innerHTML = cities.map(city=> `<option value="${esc(city)}">${esc(city)}</option>`).join('');
      el.value = cities.includes(prev) ? prev : cities[0];
    } catch { /* Métricas ainda disponíveis mesmo sem lista de cidades */ }
  }

  const LEAFLET = {
    version: '1.9.4',
    primary: {
      js: 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
      css: 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
      jsIntegrity: 'sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=',
      cssIntegrity: 'sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=',
    },
    fallback: {
      js: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js',
      css: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css',
    },
  };
  function loadAsset(tag, url, integrity) {
    return new Promise((resolve, reject) => {
      const el = document.createElement(tag);
      if (tag === 'script') { el.src = url; el.async = true; } else { el.rel = 'stylesheet'; el.href = url; }
      if (integrity) { el.integrity = integrity; el.crossOrigin = ''; }
      el.onload = () => resolve();
      el.onerror = () => { el.remove(); reject(new Error('Falha ao carregar a biblioteca do mapa.')); };
      document.head.appendChild(el);
    });
  }
  async function loadLeaflet() {
    if (window.L) return;
    // Primeiro com verificação de integridade (SRI); se o hash não bater, usa a cópia do cdnjs.
    try {
      await Promise.all([
        loadAsset('link', LEAFLET.primary.css, LEAFLET.primary.cssIntegrity),
        loadAsset('script', LEAFLET.primary.js, LEAFLET.primary.jsIntegrity),
      ]);
    } catch {
      await Promise.all([loadAsset('link', LEAFLET.fallback.css), loadAsset('script', LEAFLET.fallback.js)]);
    }
  }
  async function loadMap() {
    if (state.mapLoading || state.mapReady) return;
    state.mapLoading = true;
    try {
      await loadLeaflet();
      state.map = L.map($('cm-map'), {center: [-28.30, -54.26], zoom: 12, scrollWheelZoom: false});
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 18,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a>',
      }).addTo(state.map);
      state.layerGroup = L.layerGroup().addTo(state.map);
      state.mapReady = true;
      $('cm-map-empty').hidden = true;
    } catch (err) {
      showMapPlaceholder(err.message);
      $('cm-geocode-status').textContent = 'Mapa indisponível agora. O ranking e os indicadores continuam funcionando.';
    }
    state.mapLoading = false;
  }
  function clearLayers() { if (state.layerGroup) state.layerGroup.clearLayers(); state.layers = []; }
  function infoHtml(row) {
    return `<div class="cm-map-info"><strong>${esc(row.name)}</strong><br><small>${fmt(row.total)} consultas no período</small><br>Aprovados: <b>${fmt(row.approved)}</b> · ${pct(row.approval_rate)}<br>Reprovados: <b>${fmt(row.rejected)}</b><br>Contratos assinados: <b>${fmt(row.signed_approved)}</b> · ${pct(row.signed_rate)}</div>`;
  }
  function plotRow(row, point, max) {
    const v = Number(metricValue(row) || 0);
    const intensity = Math.sqrt(v / Math.max(1, max));
    const radius = 130 + 580 * intensity;
    const color = layerColor(row);
    const outer = L.circle([point.lat, point.lng], {radius, color, weight: 1, opacity: .25, fillColor: color, fillOpacity: .20});
    const inner = L.circle([point.lat, point.lng], {radius: Math.max(70, radius * .45), stroke: false, fillColor: color, fillOpacity: .38});
    for (const circle of [outer, inner]) { circle.bindPopup(infoHtml(row)); circle.addTo(state.layerGroup); state.layers.push(circle); }
  }
  async function fetchGeo(ticket) {
    // O servidor localiza alguns bairros por chamada (limite de 1 consulta/s do serviço público) e guarda o resultado.
    const city = $('cm-city').value, from = $('cm-from').value, to = $('cm-to').value;
    const query = new URLSearchParams({city, date_from: from, date_to: to});
    let last = null;
    let errors = 0;
    for (let round = 0; round < 120; round += 1) {
      if (ticket !== state.revision) return null;
      const r = await fetch('/api/reports/credit-map/geo?' + query, {credentials: 'same-origin', cache: 'no-store'});
      if (r.status === 401) { window.location.assign('/login'); return null; }
      if (!r.ok) throw new Error('Falha ao localizar bairros');
      last = await r.json();
      const total = (state.data?.neighborhoods || []).length;
      const done = Object.keys(last.positions || {}).length + (last.not_found || []).length;
      if (!last.pending) break;
      if (last.error) {
        // O serviço público de endereços limita o ritmo: espera e tenta de novo (o que já foi localizado fica guardado).
        errors += 1;
        if (errors > 8) break;
        $('cm-geocode-status').textContent = `Serviço de endereços ocupado; tentando de novo… ${done} de ${total} bairros`;
        await new Promise(resolve => setTimeout(resolve, 10000));
        continue;
      }
      errors = 0;
      $('cm-geocode-status').textContent = `Localizando bairros no mapa… ${done} de ${total}`;
    }
    return last;
  }
  async function refreshMap() {
    if (!state.data) return;
    await loadMap();
    if (!state.mapReady) return;
    const ticket = ++state.revision;
    clearLayers();
    const rows = state.data.neighborhoods || [];
    if (!rows.length) { $('cm-geocode-status').textContent = 'Nenhum bairro com amostra suficiente neste período.'; return; }
    let geo;
    try { geo = await fetchGeo(ticket); } catch (err) { $('cm-geocode-status').textContent = err.message; return; }
    if (!geo || ticket !== state.revision) return;
    state.positions = new Map(Object.entries(geo.positions || {}));
    const max = Math.max(1, ...rows.map(r => Number(metricValue(r) || 0)));
    const bounds = [];
    let located = 0;
    for (const row of rows) {
      const point = state.positions.get(row.name);
      if (point) { plotRow(row, point, max); bounds.push([point.lat, point.lng]); located += 1; }
    }
    if (bounds.length >= 2) state.map.fitBounds(bounds, {padding: [40, 40], maxZoom: 14});
    else if (geo.city_center) state.map.setView([geo.city_center.lat, geo.city_center.lng], 12);
    let status = `${located} de ${rows.length} bairros localizados · centros aproximados, não indicam domicílios`;
    if (geo.error && geo.pending) status += ' · alguns bairros ainda não foram localizados; atualize a página para continuar.';
    else if (!geo.city_center) status = 'Não foi possível localizar a cidade no mapa. O ranking continua válido.';
    $('cm-geocode-status').textContent = status;
  }
  function locateRow(name) {
    if (!state.mapReady) { showMessage('O mapa ainda não carregou; o ranking continua disponível.'); return; }
    const point = state.positions.get(name);
    if (!point) { $('cm-geocode-status').textContent = `Bairro ${name}: localização geográfica não validada; consulte os indicadores no ranking.`; return; }
    const row = (state.data?.neighborhoods || []).find(x => x.name === name);
    state.map.setView([point.lat, point.lng], Math.max(state.map.getZoom(), 14));
    if (row) L.popup().setLatLng([point.lat, point.lng]).setContent(infoHtml(row)).openOn(state.map);
  }
  function initFullscreen() {
    const wrap = document.querySelector('.cm-map-wrap');
    const button = $('cm-fullscreen');
    if (!wrap || !button) return;
    const isOpen = () => document.fullscreenElement === wrap || wrap.classList.contains('cm-map-expanded');
    const refresh = () => {
      const open = isOpen();
      button.textContent = open ? '✕ Sair da tela cheia' : '⛶ Tela cheia';
      if (state.map) {
        if (open) state.map.scrollWheelZoom.enable(); else state.map.scrollWheelZoom.disable();
        setTimeout(() => state.map.invalidateSize(), 150);
      }
    };
    button.addEventListener('click', async () => {
      if (document.fullscreenElement) { await document.exitFullscreen(); return; }
      if (wrap.classList.contains('cm-map-expanded')) { wrap.classList.remove('cm-map-expanded'); refresh(); return; }
      if (wrap.requestFullscreen) {
        try { await wrap.requestFullscreen(); return; } catch { /* cai no modo ampliado abaixo */ }
      }
      wrap.classList.add('cm-map-expanded'); refresh();
    });
    document.addEventListener('fullscreenchange', refresh);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && wrap.classList.contains('cm-map-expanded')) { wrap.classList.remove('cm-map-expanded'); refresh(); }
    });
  }
  function exportRanking(){
    if(!state.data)return;
    const lines=[['Bairro','Consultas','Aprovados','Reprovados','Pendentes','Contratos assinados','Taxa aprovação (%)','Conversão (%)']];
    for(const row of sortedRows()) lines.push([row.name,row.total,row.approved,row.rejected,row.pending,row.signed_approved,row.approval_rate,row.signed_rate]);
    const safe = x=> {let s=String(x??'');if (/^[\s]*[=+\-@]/.test(s)) s="'"+s;return '"'+s.replace(/"/g,'""')+'"';};
    const csv='\ufeff'+lines.map(r=>r.map(safe).join(';')).join('\r\n');
    const blob=new Blob([csv],{type:'text/csv;charset=utf-8'});const url=URL.createObjectURL(blob);
    const a=document.createElement('a');a.href=url;a.download=`mapa-credito-${$('cm-city').value.toLowerCase().replace(/[^a-z0-9]+/g,'-')}.csv`;a.click();URL.revokeObjectURL(url);
  }
  function initImport(){
    const dialog=$('cm-import-dialog');if(!dialog)return;
    $('cm-import-toggle').addEventListener('click',()=>dialog.showModal());
    $('cm-file').addEventListener('change',()=>{$('cm-file-name').textContent=$('cm-file').files?.[0]?.name||'Nenhum arquivo selecionado';});
    $('cm-import-send').addEventListener('click',async()=>{
      const file=$('cm-file').files?.[0];if(!file){$('cm-import-result').textContent='Selecione um arquivo CSV.';return;}
      if(!window.confirm('Substituir os dados agregados anteriores por este novo CSV?')) return;
      const form=new FormData();form.append('file',file);
      const btn=$('cm-import-send');btn.disabled=true;$('cm-import-result').textContent='Processando arquivo...';
      try{
        const r=await fetch('/api/reports/credit-map/import',{method:'POST',credentials:'same-origin',headers:{'x-csrf-token':root.dataset.csrf},body:form});
        const result=await r.json();if(!r.ok)throw Error(result.detail||'Erro ao importar');
        $('cm-import-result').textContent=`Importação concluída: ${fmt(result.stats.cpf_validos_formato)} consultas de CPF (em todas as cidades), ${fmt(result.aggregates)} agrupamentos.`;
        await fetchCities();await fetchData();
      }catch(err){$('cm-import-result').textContent='Erro: '+err.message;}
      finally{btn.disabled=false;}
    });
  }
  $('cm-apply').addEventListener('click',fetchData);
  $('cm-metric').addEventListener('change',()=>{if(state.data)drawAll();});
  $('cm-search').addEventListener('input',drawRanking);
  $('cm-export').addEventListener('click',exportRanking);
  initImport();
  initFullscreen();
  (async () => {await fetchCities(); await fetchData();})();
})();
