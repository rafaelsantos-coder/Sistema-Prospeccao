/* Sulnet Prospecta — inteligência territorial do crédito. Sem nome, CPF ou telefone; só endereço dos aprovados. */
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
  const state = {data: null, map: null, layers: [], positions: new Map(), mapReady: false, mapLoading:false, revision:0, mode:'approved', heatLayer:null, streetHeatRows:[], gpsHeatRows:[], triangleFittedFor:'', heatGpsFittedFor:''};
  state.network = null;
  state.networkMatches = new Map();
  state.networkGpsReady = false;
  state.networkStreetsReady = false;
  // L is Leaflet normally; on Google Maps we supply only the small surface used by this panel.
  let L = window.L;
  let activeMapProvider = 'osm';
  const metric = () => $('cm-metric').value;
  const metricValue = (row) => row[metric()] == null ? 0 : row[metric()];
  const metricDisplay = (row) => ['approval_rate','signed_rate'].includes(metric()) ? pct(row[metric()]) : fmt(row[metric()]);
  // Cor única por território: verde se aprovados >= reprovados (empate fica verde), senão vermelho.
  const GREEN = '#16a66f', RED = '#e35b53', YELLOW = '#f5c518';
  const NAP_COLORS = {free: '#3b82f6', online: '#a3e635', other: '#94a3b8', opportunity: '#a78bfa'};
  const layerColor = (row) => Number(row.approved || 0) >= Number(row.rejected || 0) ? GREEN : RED;
  const rankColor = (row) => state.mode === 'rejected' ? RED
    : state.mode === 'approved' ? GREEN
    : state.mode === 'compare' ? window.SulnetCreditHeat.modeColor('compare', row.approval_rate)
    : layerColor(row);
  const selectedHood = () => $('cm-neighborhood')?.value || '';
  const visibleRows = () => (state.data?.neighborhoods || []).filter(row =>
    (!selectedHood() || row.name === selectedHood()) &&
    (state.mode !== 'compare' || Number(row.approved || 0) + Number(row.rejected || 0) >= 20));
  const showMessage = (message) => { const el = $('cm-message'); el.textContent = message; el.hidden = !message; };
  const showMapPlaceholder = (message) => { $('cm-map-empty').hidden = false; if(message) $('cm-map-empty').querySelector('p').textContent = message; };
  const sync = (message) => { $('cm-sync').innerHTML = `<span></span>${esc(message)}`; };

  function sortedRows() {
    const rows = [...visibleRows()];
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
      return `<button class="cm-rank-row" type="button" data-bairro="${esc(row.name)}" title="Localizar ${esc(row.name)} no mapa"><span class="cm-rank-pos">${String(i+1).padStart(2,'0')}</span><span><span class="cm-rank-name">${esc(row.name)}</span><span class="cm-rank-sub"><span class="cm-rank-track"><span class="cm-rank-progress" style="display:block;width:${progress}%;background:${rankColor(row)}"></span></span><span class="cm-rank-detail">${fmt(row.total)} cons.</span></span></span><span class="cm-rank-value">${metricDisplay(row)}<small>${metric()==='approval_rate' ? fmt(row.approved)+' aprov.' : pct(row.approval_rate)+' aprov.'}</small></span></button>`;
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
      return `<div class="cm-month-line"><span>${esc(monthLabel(m.month))}</span><span class="cm-month-bars" title="${fmt(m.total)} consultas e ${fmt(m.approved)} aprovados"><span class="cm-month-approved" style="width:${app}%"></span><span class="cm-month-rejected" style="width:${rest}%"></span></span><span>${fmt(m.total)} consultas<small>${fmt(m.signed_approved)} assinados</small></span></div>`;
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
    const titles = {approved:'Calor de aprovações', rejected:'Calor de reprovações',
      compare:'Comparativo de crédito por bairro', triangles:'Aprovados com GPS do CRM'};
    $('cm-map-title').textContent = titles[state.mode] || 'Inteligência territorial';
  }
  function updateHoodOptions() {
    const el = $('cm-neighborhood'); if (!el) return;
    const previous = el.value;
    el.replaceChildren(new Option('Todos os bairros', ''));
    for (const row of [...(state.data?.neighborhoods || [])].sort((a,b)=>a.name.localeCompare(b.name))) {
      el.add(new Option(row.name, row.name));
    }
    el.value = [...el.options].some(o=>o.value===previous) ? previous : '';
  }
  function updateLegend() {
    const legend = $('cm-map-legend'); if (!legend) return;
    const legends = {
      approved: '<span><i class="cm-dot cm-dot-green"></i> Verde mais intenso = mais aprovações</span>',
      rejected: '<span><i class="cm-dot cm-dot-red"></i> Vermelho mais intenso = mais reprovações</span>',
      compare: '<span><i class="cm-dot cm-dot-green"></i> Taxa ≥75%</span><span><i class="cm-dot cm-dot-amber"></i> 50–74,9%</span><span><i class="cm-dot cm-dot-red"></i> Abaixo de 50% (mín. 20 decisões)</span>',
      triangles: '<span><i class="cm-tri-key cm-tri-green"></i> Aprovado com contrato</span><span><i class="cm-tri-key cm-tri-yellow"></i> Aprovado sem contrato</span>',
    };
    const gps = '<span><i class="cm-tri-key cm-tri-green"></i> GPS aprovado</span>';
    const naps = networkEnabled() ?
      '<span><i class="cm-dot cm-dot-nap-free"></i> Portas livres</span>' +
      '<span><i class="cm-dot cm-dot-nap-online"></i> Sessões online</span>' +
      '<span><i class="cm-dot cm-dot-nap-other"></i> Sem portas livres nem sessões online</span>' +
      '<span><i class="cm-dot cm-dot-nap-priority"></i> Oportunidade</span>' : '';
    legend.innerHTML = (legends[state.mode] || '') +
      (state.mode !== 'triangles' && $('cm-addresses').checked ? gps : '') + naps;
    document.querySelectorAll('[data-map-mode]').forEach(button =>
      button.setAttribute('aria-pressed', String(button.dataset.mapMode === state.mode)));
  }
  function drawAll() { drawKPIs(); drawRanking(); drawMonths(); updateLegend(); refreshMap(); }

  const ALL_CITIES = '__ALL__';
  const cityLabel = (name) => name.toLowerCase().replace(/(^|\s)\S/g, ch => ch.toUpperCase());
  async function fetchCityData(city, from, to) {
    const query = new URLSearchParams({city, date_from:from, date_to:to});
    const r = await fetch('/api/reports/credit-map?' + query, {credentials:'same-origin',cache:'no-store'});
    if(r.status === 401) {window.location.assign('/login');return null;}
    const payload = await r.json();
    if(!r.ok) throw new Error(payload.detail || 'Falha ao carregar a base');
    return payload;
  }
  function cityList() {
    return [...$('cm-city').options].map(o => o.value).filter(v => v && v !== ALL_CITIES);
  }
  async function fetchAllCities(from, to) {
    const cities = cityList();
    const parts = await Promise.all(cities.map(async city => [city, await fetchCityData(city, from, to)]));
    const totals = {total:0, approved:0, rejected:0, pending:0, signed_approved:0, signed_rejected_inconsistent:0};
    const months = new Map();
    const rows = [];
    let suppressed = 0;
    for (const [city, part] of parts) {
      if (!part) return null;
      for (const key of Object.keys(totals)) totals[key] += Number(part.totals?.[key] || 0);
      suppressed += Number(part.suppressed_neighborhoods || 0);
      for (const hood of part.neighborhoods || []) rows.push({...hood, name: `${hood.name} · ${cityLabel(city)}`, hood: hood.name, city});
      for (const m of part.months || []) {
        const cur = months.get(m.month) || {month:m.month, total:0, approved:0, rejected:0, pending:0, signed_approved:0};
        for (const key of ['total','approved','rejected','pending','signed_approved']) cur[key] += Number(m[key] || 0);
        months.set(m.month, cur);
      }
    }
    const decided = totals.approved + totals.rejected;
    totals.approval_rate = decided ? Math.round(totals.approved / decided * 1000) / 10 : null;
    totals.signed_rate = totals.approved ? Math.round(totals.signed_approved / totals.approved * 1000) / 10 : null;
    return {city: 'TODAS', all: true, cities, totals, neighborhoods: rows, suppressed_neighborhoods: suppressed,
            months: [...months.values()].sort((a,b) => a.month.localeCompare(b.month))};
  }
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
        if(city === ALL_CITIES) {
          data = await fetchAllCities(from, to);
          if(!data) return;
        } else {
          data = await fetchCityData(city, from, to);
          if(!data) return;
        }
      }
      state.data = data; state.streetHeatRows = []; state.gpsHeatRows = []; state.triangleFittedFor = ''; state.heatGpsFittedFor = ''; state.revision += 1; updateHoodOptions(); sync('Dados agregados carregados'); drawAll();
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
      el.innerHTML = `<option value="${ALL_CITIES}">Todas as cidades</option>` + cities.map(city=> `<option value="${esc(city)}">${esc(city)}</option>`).join('');
      el.value = (cities.includes(prev) || prev === ALL_CITIES) ? prev : cities[0];
    } catch { /* Métricas ainda disponíveis mesmo sem lista de cidades */ }
  }

  function googleMapAdapter() {
    const gm = window.google.maps;
    const point = p => Array.isArray(p) ? {lat: Number(p[0]), lng: Number(p[1])} : p;
    const initial = (container, config) => {
      const map = new gm.Map(container, {
        center: point(config.center), zoom: config.zoom, scrollwheel: false,
        mapId: root.dataset.googleMapId || 'DEMO_MAP_ID',
        fullscreenControl: false, mapTypeControl: true,
      });
      return {
        native: map,
        fitBounds(points, opts = {}) {
          const bounds = new gm.LatLngBounds();
          points.forEach(p => bounds.extend(point(p)));
          map.fitBounds(bounds, opts.padding || 40);
          if (opts.maxZoom != null) gm.event.addListenerOnce(map, 'idle', () => {
            if (map.getZoom() > opts.maxZoom) map.setZoom(opts.maxZoom);
          });
        },
        setView(center, zoom) { map.setCenter(point(center)); map.setZoom(zoom); },
        getZoom() { return map.getZoom() || 12; },
        getBounds() {
          const bounds = map.getBounds();
          return bounds ? {contains: position => bounds.contains(point(position))} : null;
        },
        onMoveEnd(callback) { map.addListener('idle', callback); },
        scrollWheelZoom: {
          enable() { map.setOptions({scrollwheel: true}); },
          disable() { map.setOptions({scrollwheel: false}); },
        },
        invalidateSize() { gm.event.trigger(map, 'resize'); },
      };
    };
    const group = () => {
      const items = [];
      return {
        native: null,
        addTo(map) { this.native = map.native; return this; },
        add(item) { items.push(item); item.attach(this.native); },
        clearLayers() { for (const item of items) item.detach(); items.length = 0; },
      };
    };
    const popupFor = (coordinate) => {
      const popup = new gm.InfoWindow();
      return {
        setLatLng(pos) { coordinate = point(pos); return this; },
        setContent(content) { popup.setContent(content); return this; },
        openOn(map) { popup.setPosition(coordinate); popup.open(map.native); return this; },
      };
    };
    const markerOrCircle = (coordinates, options, type) => {
      const position = point(coordinates);
      let map = null, shape = null, popup = null;
      const item = {
        bindPopup(html) { popup = html; return this; },
        attach(native) {
          map = native;
          if (type === 'marker') {
            shape = new gm.Marker({position, map, icon: options.icon, clickable: true});
          } else {
            const style = {
              strokeColor: options.color || options.fillColor,
              strokeWeight: options.weight ?? (options.stroke === false ? 0 : 1),
              strokeOpacity: options.opacity ?? 1,
              fillColor: options.fillColor,
              fillOpacity: options.fill === false ? 0 : options.fillOpacity ?? 0.4,
            };
            if (type === 'circleMarker' && options.fixedSize) {
              shape = new gm.Marker({
                position, map, clickable: options.interactive !== false,
                icon: {...style, path: gm.SymbolPath.CIRCLE, scale: options.radius},
              });
            } else {
              shape = new gm.Circle({
                ...style, center: position, map, radius: type === 'circleMarker' ? 30 : options.radius,
              });
            }
          }
          if (popup) shape.addListener('click', () => {
            const win = new gm.InfoWindow({content: popup, position});
            win.open({map});
          });
        },
        detach() { if(shape) shape.setMap(null); shape = null; map = null; },
        addTo(layer) { layer.add(item); return item; },
      };
      return item;
    };
    return {
      map: initial,
      tileLayer() { return {addTo() { return this; }}; },
      layerGroup: group,
      circle: (coordinates, options) => markerOrCircle(coordinates, options, 'circle'),
      circleMarker: (coordinates, options) => markerOrCircle(coordinates, options, 'circleMarker'),
      divIcon(options) {
        const signed = options.html.includes(GREEN);
        return {
          path: 'M 1.5 1.5 L 16.5 1.5 L 9 14.5 Z',
          anchor: new gm.Point(9, 16), scaledSize: new gm.Size(18, 16),
          fillColor: signed ? GREEN : YELLOW, fillOpacity: 1,
          strokeColor: signed ? '#0b5c3c' : '#7a5a00', strokeWeight: 1.5,
        };
      },
      marker: (coordinates, options) => markerOrCircle(coordinates, options, 'marker'),
      popup: () => popupFor(null),
    };
  }
  async function loadMap() {
    if (state.mapLoading || state.mapReady) return;
    state.mapLoading = true;
    try {
      if (root.dataset.mapProvider === 'google') {
        try {
          await window.SulnetMapAssets.loadGoogleMaps({
            key: root.dataset.googleMapsKey, nonce: root.dataset.googleMapsNonce,
          });
          L = googleMapAdapter();
          activeMapProvider = 'google';
        } catch (error) {
          // A bad demo key or an API restriction must never hide commercial indicators.
          showMessage('Google Maps indisponível. Mapa gratuito ativado: ' + error.message);
          await window.SulnetMapAssets.loadLeaflet();
          L = window.L;
          activeMapProvider = 'osm';
        }
      } else {
        await window.SulnetMapAssets.loadLeaflet();
        L = window.L;
        activeMapProvider = 'osm';
      }
      if (activeMapProvider === 'google') {
        state.map = L.map($('cm-map'), {center: [-28.30, -54.26], zoom: 12});
      } else {
        state.map = L.map($('cm-map'), {center: [-28.30, -54.26], zoom: 12, scrollWheelZoom: false});
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
          maxZoom: 18,
          attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a>',
        }).addTo(state.map);
      }
      $('cm-map-provider-label').textContent = activeMapProvider === 'google' ? 'Google Maps' : 'OpenStreetMap';
      state.layerGroup = L.layerGroup().addTo(state.map);
      if ($('cm-network-layer')) {
        state.networkLayer = L.layerGroup().addTo(state.map);
        if (activeMapProvider === 'google') state.map.onMoveEnd(drawNetwork);
        else {
          state.networkRenderer = L.canvas({padding: .25});
          state.map.on('moveend', drawNetwork);
        }
      }
      if (window.SulnetCreditHeat) {
        state.heatLayer = window.SulnetCreditHeat.attach(state.map, activeMapProvider, $('cm-map'));
      }
      state.mapReady = true;
      $('cm-map-empty').hidden = true;
    } catch (err) {
      showMapPlaceholder(err.message);
      $('cm-geocode-status').textContent = 'Mapa indisponível agora. O ranking e os indicadores continuam funcionando.';
    }
    state.mapLoading = false;
  }
  function clearLayers() { if (state.layerGroup) state.layerGroup.clearLayers(); state.heatLayer?.clear(); state.layers = []; }
  function infoHtml(row) {
    return `<div class="cm-map-info"><strong>${esc(row.name)}</strong><br><small>${fmt(row.total)} consultas no período</small><br>Aprovados: <b>${fmt(row.approved)}</b> · ${pct(row.approval_rate)}<br>Reprovados: <b>${fmt(row.rejected)}</b><br>Contratos assinados: <b>${fmt(row.signed_approved)}</b> · ${pct(row.signed_rate)}</div>`;
  }
  function renderHeat() {
    if (!state.heatLayer || !window.SulnetCreditHeat) return;
    const points = window.SulnetCreditHeat.makePoints(
      visibleRows(), state.positions, state.data?.all ? [] : state.streetHeatRows, state.mode,
      state.data?.all ? [] : state.gpsHeatRows);
    state.heatLayer.set(points, state.mode);
  }

  function mapLocationQuery(query) {
    if (!networkEnabled()) return query;
    const cachedQuery = new URLSearchParams(query);
    cachedQuery.set('cached_only', 'true');
    return cachedQuery;
  }

  async function fetchGeo(ticket, cityName) {
    // O servidor localiza alguns bairros por chamada (limite de 1 consulta/s do serviço público) e guarda o resultado.
    const city = cityName || $('cm-city').value, from = $('cm-from').value, to = $('cm-to').value;
    const query = mapLocationQuery(new URLSearchParams({city, date_from: from, date_to: to}));
    let last = null;
    let errors = 0;
    for (let round = 0; round < 120; round += 1) {
      if (ticket !== state.revision) return null;
      const r = await fetch('/api/reports/credit-map/geo?' + query, {credentials: 'same-origin', cache: 'no-store'});
      if (r.status === 401) { window.location.assign('/login'); return null; }
      if (!r.ok) throw new Error('Falha ao localizar bairros');
      last = await r.json();
      const total = (state.data?.neighborhoods || []).filter(r => !r.city || r.city === city).length;
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
      $('cm-geocode-status').textContent = `Localizando bairros no mapa${state.data?.all ? ' (' + cityLabel(city) + ')' : ''}… ${done} de ${total}`;
    }
    return last;
  }
  async function refreshMap() {
    if (!state.data) return;
    await loadMap();
    if (!state.mapReady) return;
    const ticket = ++state.revision;
    clearLayers();
    state.streetHeatRows = [];
    state.gpsHeatRows = [];
    state.networkGpsReady = false;
    state.networkStreetsReady = false;
    refreshNetwork();
    state.heatGpsFittedFor = '';
    state.positions = new Map();
    const rows = visibleRows();

    if (state.mode === 'triangles') {
      $('cm-geocode-status').textContent = state.data.all
        ? 'Escolha um município para ver os triângulos com coordenadas do CRM.'
        : 'Triângulos posicionados no GPS informado na coluna Q, sem deslocamento artificial.';
      if (state.data.all) {
        if (streetGroup) streetGroup.clearLayers();
        if (addressGroup) addressGroup.clearLayers();
        $('cm-address-status').textContent = 'Selecione uma cidade para exibir os pontos aprovados.';
      } else drawDetailLayers(ticket);
      return;
    }

    if (!rows.length) {
      $('cm-geocode-status').textContent = state.mode === 'compare'
        ? 'Nenhum bairro atingiu o mínimo de 20 decisões nesse filtro.'
        : 'Nenhum bairro com amostra suficiente neste período.';
      if (streetGroup) streetGroup.clearLayers();
      if (addressGroup) addressGroup.clearLayers();
      if (networkEnabled() && !state.data.all) drawDetailLayers(ticket);
      return;
    }
    // Calor verde pode surgir imediatamente da coluna Q; nao precisa aguardar
    // a geocodificacao dos bairros para apresentar os pontos aprovados.
    const gpsStarted = state.mode === 'approved' && !state.data.all;
    if (gpsStarted) drawDetailLayers(ticket);
    const bounds = [], centers = [], positions = new Map();
    const cities = state.data.all ? (state.data.cities || []) : [$('cm-city').value];
    let pending = false;
    for (const city of cities) {
      let geo;
      try { geo = await fetchGeo(ticket, city); }
      catch (err) { $('cm-geocode-status').textContent = err.message; return; }
      if (!geo || ticket !== state.revision) return;
      if (geo.city_center) centers.push([geo.city_center.lat, geo.city_center.lng]);
      if (geo.error && geo.pending) pending = true;
      for (const [name, point] of Object.entries(geo.positions || {})) {
        positions.set(state.data.all ? name + ' · ' + cityLabel(city) : name, point);
      }
      state.positions = positions;
    }
    let located = 0;
    for (const row of rows) {
      const point = positions.get(row.name);
      if (point) { bounds.push([point.lat, point.lng]); located++; }
    }
    renderHeat();
    if (bounds.length >= 2) state.map.fitBounds(bounds, {padding: [40, 40], maxZoom: state.data.all ? 11 : 14});
    else if (bounds.length === 1) state.map.setView(bounds[0], 12);
    else if (centers.length) state.map.setView(centers[0], 12);
    let status = located + ' de ' + rows.length + ' bairros posicionados · ' +
      (state.mode === 'approved' ? 'aprovações por GPS e saldo aproximado por bairro'
        : state.mode === 'rejected' ? 'reprovações agregadas por rua/bairro'
        : 'comparativo aproximado por bairro');
    if (state.mode === 'compare') status += ' · mínimo 20 decisões por bairro';
    if (pending) status += ' · alguns locais ainda não foram encontrados.';
    else if (!centers.length && !state.gpsHeatRows.length)
      status = 'Não foi possível localizar a cidade. Ranking e indicadores continuam disponíveis.';
    $('cm-geocode-status').textContent = status;
    if (state.data.all) {
      if (streetGroup) streetGroup.clearLayers();
      if (addressGroup) addressGroup.clearLayers();
      $('cm-streets-status').textContent = '';
      $('cm-address-status').textContent = '';
    } else if (!gpsStarted) drawDetailLayers(ticket);
  }
  // Ruas e endereços são localizados em rodadas alternadas (o serviço público de endereços aceita
  // uma consulta por segundo), para os triângulos aparecerem desde o começo, sem esperar as ruas.
  let addressGroup = null;
  let streetGroup = null;
  function triangleIcon(signed) {
    const fill = signed ? GREEN : YELLOW, edge = signed ? '#0b5c3c' : '#7a5a00';
    return L.divIcon({className: 'cm-tri', iconSize: [18, 16], iconAnchor: [9, 16], popupAnchor: [0, -14],
      html: `<svg width="18" height="16" viewBox="0 0 18 16" aria-hidden="true"><polygon points="1.5,1.5 16.5,1.5 9,14.5" fill="${fill}" stroke="${edge}" stroke-width="1.5" stroke-linejoin="round"/></svg>`});
  }
  function spread(rows) {
    // Clientes no mesmo ponto (mesmo prédio, ou rua sem número localizado) abrem em espiral de poucos metros.
    const seen = new Map();
    return rows.map(row => {
      const key = row.lat.toFixed(6) + ',' + row.lng.toFixed(6);
      const i = seen.get(key) || 0; seen.set(key, i + 1);
      // Pontos capturados pelo CRM nao devem ser deslocados artificialmente.
      if (!i || row.source_coordinates) return row;
      const meters = 9 * Math.sqrt(i), angle = i * 2.39996;
      return {...row, lat: row.lat + meters * Math.cos(angle) / 111320,
        lng: row.lng + meters * Math.sin(angle) / (111320 * Math.cos(row.lat * Math.PI / 180))};
    });
  }
  // Cada passo faz uma chamada e devolve 'done' (acabou), 'more' (ainda há o que localizar),
  // 'busy' (serviço de endereços ocupado) ou 'stale' (o filtro mudou no meio da chamada).
  async function addressStep(query, ticket) {
    const r = await fetch('/api/reports/credit-map/addresses?' + mapLocationQuery(query), {credentials: 'same-origin', cache: 'no-store'});
    if (!r.ok) return 'done';
    const data = await r.json();
    const needsHeat = state.mode === 'approved';
    if (ticket !== state.revision || (!needsHeat && !$('cm-addresses').checked && !networkEnabled())) return 'stale';
    addressGroup.clearLayers();
    const gpsRows = (data.addresses || []).filter(row =>
      !selectedHood() || folded(row.neighborhood) === folded(selectedHood()));
    state.gpsHeatRows = gpsRows.filter(row => row.source_coordinates);
    state.networkGpsReady = true;
    refreshNetwork();
    if (needsHeat) {
      renderHeat();
      const gpsKey = query.toString() + selectedHood();
      if (state.positions.size === 0 && gpsRows.length && state.heatGpsFittedFor !== gpsKey) {
        const coords = gpsRows.filter(row => row.source_coordinates).map(row => [row.lat, row.lng]);
        if (coords.length >= 2) state.map.fitBounds(coords, {padding:[50, 50], maxZoom:13});
        else if (coords.length) state.map.setView(coords[0], 13);
        state.heatGpsFittedFor = gpsKey;
      }
    }
    for (const row of spread($('cm-addresses').checked ? gpsRows : [])) {
      const where = row.street ? esc(row.street) + (row.number ? ', ' + esc(row.number) : ' (sem número)') : 'Coordenada registrada';
      const status = row.signed ? 'Aprovado · contrato assinado' : 'Aprovado · sem contrato';
      const many = row.count > 1 ? `<br>${fmt(row.count)} registros neste ponto` : '';
      const approx = row.source_coordinates
        ? '<br><small>Coordenada informada pelo sistema de origem; pode representar o local da contratação e não a porta do imóvel.</small>'
        : row.exact ? '' : '<br><small>Posição aproximada: o mapa achou a rua, não o número.</small>';
      L.marker([row.lat, row.lng], {icon: triangleIcon(row.signed), keyboard: false})
        .bindPopup(`<div class="cm-map-info"><strong>${where}</strong><br><small>${esc(row.neighborhood)}</small><br>${status}${many}${approx}</div>`)
        .addTo(addressGroup);
    }
    if (state.mode === 'triangles' && gpsRows.length && state.triangleFittedFor !== query.toString() + selectedHood()) {
      const bounds = gpsRows.map(row => [row.lat, row.lng]);
      if (bounds.length >= 2) state.map.fitBounds(bounds, {padding: [55, 55], maxZoom: 15});
      else state.map.setView(bounds[0], 15);
      state.triangleFittedFor = query.toString() + selectedHood();
    }
    if (!data.total) { $('cm-address-status').textContent = 'Sem triângulos GPS neste período. Verifique a coluna Coordenadas (Q).'; return 'done'; }
    const found = gpsRows.length;
    const gps = Number(data.source_coordinates || 0);
    const suffix = gps ? ` · ${fmt(gps)} coordenadas recebidas do CRM` : '';
    $('cm-address-status').textContent = (state.mode === 'approved' && !$('cm-addresses').checked
      ? `${fmt(found)} pontos GPS incorporados ao calor de aprovações`
      : `${fmt(found)} de ${fmt(data.total)} pontos aprovados no mapa · ${fmt(data.signed)} com contrato, ${fmt(data.unsigned)} sem contrato`) + suffix + (data.pending ? ' · localizando os demais, pode levar alguns minutos…' : '');
    return !data.pending ? 'done' : data.error ? 'busy' : 'more';
  }
  async function streetStep(query, ticket) {
    const needsHeat = state.mode === 'rejected';
    const r = await fetch('/api/reports/credit-map/streets?' + mapLocationQuery(query), {credentials: 'same-origin', cache: 'no-store'});
    if (!r.ok) return 'done';
    const data = await r.json();
    if (ticket !== state.revision || (!needsHeat && !$('cm-streets').checked && !networkEnabled())) return 'stale';
    state.streetHeatRows = data.streets || [];
    state.networkStreetsReady = state.streetHeatRows.length > 0;
    refreshNetwork();
    if (needsHeat) renderHeat();
    streetGroup.clearLayers();
    for (const row of data.streets || []) {
      if (!$('cm-streets').checked) break;
      if (selectedHood() && folded(row.neighborhood) !== folded(selectedHood())) continue;
      const color = state.mode === 'rejected' ? RED : layerColor(row);
      const dot = L.circleMarker([row.lat, row.lng], {radius: 7, color: '#fff', weight: 1.5, fillColor: color, fillOpacity: .9});
      dot.bindPopup(`<div class="cm-map-info"><strong>${esc(row.street)}</strong><br><small>${esc(row.neighborhood)}${row.cep ? ' · CEP ' + esc(row.cep.slice(0,5) + '-' + row.cep.slice(5)) : ''}</small><br>${fmt(row.total)} consultas · aprovados <b>${fmt(row.approved)}</b> (${pct(row.approval_rate)})<br>Assinados: <b>${fmt(row.signed_approved)}</b></div>`);
      dot.addTo(streetGroup);
    }
    if (!data.total) { $('cm-streets-status').textContent = 'Sem ruas na base (importe o CSV com Rua/CEP).'; return 'done'; }
    $('cm-streets-status').textContent = `${(data.streets || []).length} de ${data.total} ruas com 5+ consultas localizadas`;
    return !data.pending ? 'done' : data.error ? 'busy' : 'more';
  }
  async function drawDetailLayers(ticket) {
    if (!state.mapReady) return;
    streetGroup = streetGroup || L.layerGroup().addTo(state.map);
    addressGroup = addressGroup || L.layerGroup().addTo(state.map);
    streetGroup.clearLayers(); addressGroup.clearLayers();
    const query = new URLSearchParams({city: $('cm-city').value, date_from: $('cm-from').value, date_to: $('cm-to').value});
    const layers = [
      {kind:'addresses', step: addressStep, status:'cm-address-status', done: false},
      {kind:'streets', step: streetStep, status:'cm-streets-status', done: false},
    ];
    for (const layer of layers) {
      const enabled = layer.kind === 'addresses' ? ($('cm-addresses').checked || state.mode === 'approved')
        : $('cm-streets').checked || state.mode === 'rejected';
      if (!enabled && !networkEnabled()) {layer.done = true; $(layer.status).textContent = '';}
    }
    let errors = 0;
    for (let round = 0; round < 900 && layers.some(layer => !layer.done); round += 1) {
      let busy = false;
      for (const layer of layers) {
        if (layer.done) continue;
        if (ticket !== state.revision) return;
        let result;
        try { result = await layer.step(query, ticket); } catch { result = 'busy'; }
        if (result === 'stale') return;
        if (result === 'done') layer.done = true;
        if (result === 'busy') busy = true;
      }
      if (busy) { errors += 1; if (errors > 8) return; await new Promise(res => setTimeout(res, 10000)); } else errors = 0;
    }
  }
  function locateRow(name) {
    if (!state.mapReady) { showMessage('O mapa ainda não carregou; o ranking continua disponível.'); return; }
    const point = state.positions.get(name);
    if (!point) { $('cm-geocode-status').textContent = `Bairro ${name}: localização geográfica não validada; consulte os indicadores no ranking.`; return; }
    const row = (state.data?.neighborhoods || []).find(x => x.name === name);
    state.map.setView([point.lat, point.lng], Math.max(state.map.getZoom(), 14));
    if (row) L.popup().setLatLng([point.lat, point.lng]).setContent(infoHtml(row)).openOn(state.map);
  }
  function networkEnabled() {
    return Boolean(state.network?.naps.length && $('cm-network-layer')?.value !== 'off' && window.SulnetCreditNetwork);
  }
  function networkRadius() { return Number($('cm-network-radius')?.value || 250); }
  function refreshNetwork() {
    if (networkEnabled() && state.data && !state.data.all) {
      const hood = selectedHood();
      const streets = state.streetHeatRows.filter(row => !hood || folded(row.neighborhood) === folded(hood));
      state.networkMatches = window.SulnetCreditNetwork.cross(
        state.network.naps, state.gpsHeatRows, streets, networkRadius());
    } else state.networkMatches = new Map();
    drawNetwork();
  }
  function networkPopup(nap) {
    const match = state.networkMatches.get(nap.id);
    const approvals = state.networkGpsReady ?
      `<br>Aprovados próximos <b>${fmt(match?.approved)}</b> · ${fmt(match?.signed)} assinados · ${fmt(match?.unsigned)} sem contrato` : '';
    const rejected = state.networkStreetsReady ?
      `<br>Reprovações em ruas próximas <b>${fmt(match?.rejected)}</b> <small>(aprox.)</small>` : '';
    const proximity = approvals || rejected ? `<br><small>Cruzamento geográfico a até ${networkRadius()} m.</small>` : '';
    return `<div class="cm-map-info"><strong>${esc(nap.nome)}</strong> · ${esc(nap.status)}
      <br>Portas livres <b>${fmt(nap.livres)}</b> de ${fmt(nap.portas)} · ${fmt(nap.usadas)} usadas
      <br>Sessões online <b>${fmt(nap.online)}</b> · offline <b>${fmt(nap.offline)}</b>${approvals}${rejected}${proximity}</div>`;
  }
  function drawNetwork() {
    const status = $('cm-network-status');
    if (!status) return;
    const snapshot = state.network;
    state.networkLayer?.clearLayers();
    $('cm-network-summary').hidden = true;
    $('cm-network-ranking').hidden = true;
    if (!snapshot) return;
    if (!snapshot.naps.length) { status.textContent = 'Base de caixas NAP ainda sem leitura.'; return; }
    const [day, hour] = (snapshot.sessions_at || '').split('T');
    const stamp = hour ? ` · Sessões de ${day.split('-').reverse().join('/')} às ${hour.slice(0, 8)}` : '';
    const base = `${fmt(snapshot.naps.length)} NAPs na base${stamp}`;
    status.textContent = base;
    if (!networkEnabled() || !state.mapReady) return;
    const bounds = state.map.getBounds();
    if (!bounds) return;
    const area = snapshot.naps.filter(nap => bounds.contains([nap.lat, nap.lng]));
    const opportunities = nap => state.networkMatches.get(nap.id)?.opportunities || 0;
    const sum = key => area.reduce((total, nap) => total + nap[key], 0);
    $('cm-network-summary').hidden = false;
    $('cm-network-naps').textContent = fmt(area.length);
    $('cm-network-free').textContent = fmt(sum('livres'));
    $('cm-network-online').textContent = fmt(sum('online'));
    $('cm-network-opportunities').textContent = state.networkGpsReady
      ? fmt(area.reduce((total, nap) => total + opportunities(nap), 0)) : '—';

    const lowZoom = state.map.getZoom() < 12;
    if (state.data?.all) status.textContent += ' · Selecione um município para cruzar o crédito.';
    if (lowZoom && area.length) status.textContent += ' · Aproxime para ver as caixas.';
    const filter = $('cm-network-layer').value;
    if (!lowZoom) {
      const markerOptions = {fixedSize: true, renderer: state.networkRenderer};
      for (const nap of area) {
        const free = nap.livres > 0;
        const online = nap.online > 0;
        const opportunity = opportunities(nap) > 0;
        const matches = {
          all: true, free, full: !free, online, 'no-online': !online, opportunities: opportunity,
        };
        if (!matches[filter]) continue;
        const position = [nap.lat, nap.lng];
        const fillColor = free ? NAP_COLORS.free : online ? NAP_COLORS.online : NAP_COLORS.other;
        if (opportunity) {
          L.circleMarker(position, {
            ...markerOptions, radius: 8, color: NAP_COLORS.opportunity, weight: 1.5,
            opacity: .88, fill: false, interactive: false,
          }).addTo(state.networkLayer);
        }
        L.circleMarker(position, {
          ...markerOptions, radius: 5, color: online ? NAP_COLORS.online : fillColor, weight: 1.5,
          opacity: .88, fillColor, fillOpacity: .24,
        }).bindPopup(networkPopup(nap)).addTo(state.networkLayer);
      }
    }
    const ranking = area.filter(nap => opportunities(nap) > 0)
      .sort((a, b) => opportunities(b) - opportunities(a) || b.livres - a.livres || a.id - b.id).slice(0, 5);
    const list = $('cm-network-ranked-points');
    $('cm-network-ranking').hidden = ranking.length === 0;
    list.innerHTML = ranking.map(nap =>
      `<button type="button" data-nap="${nap.id}"><b>${esc(nap.nome)}</b><span>${fmt(opportunities(nap))} aprovados sem contrato · ${fmt(nap.livres)} portas livres</span></button>`
    ).join('');
    list.querySelectorAll('[data-nap]').forEach(button => {
      button.addEventListener('click', () => {
        const nap = ranking.find(row => row.id === Number(button.dataset.nap));
        const point = [nap.lat, nap.lng];
        state.map.setView(point, Math.max(state.map.getZoom(), 15));
        L.popup().setLatLng(point).setContent(networkPopup(nap)).openOn(state.map);
      });
    });
  }
  async function loadNetwork() {
    if (!$('cm-network-status') || root.dataset.preview === 'true') return;
    try {
      const response = await fetch('/api/reports/credit-map/network', {credentials:'same-origin',cache:'no-store'});
      if (!response.ok) throw new Error('Falha ao consultar a base NAP.');
      state.network = await response.json();
      updateLegend();
      refreshNetwork();
    } catch {
      $('cm-network-status').textContent = 'Base NAP indisponível agora.';
    }
  }
  function initNetwork() {
    $('cm-network-radius')?.addEventListener('change', refreshNetwork);
    const layer = $('cm-network-layer');
    let wasHidden = layer?.value === 'off';
    layer?.addEventListener('change', () => {
      const hidden = layer.value === 'off';
      const visibilityChanged = hidden !== wasHidden;
      wasHidden = hidden;
      updateLegend();
      if (state.data && state.network?.naps.length && visibilityChanged) refreshMap();
      else refreshNetwork();
    });
    const dialog = $('cm-network-dialog');
    if (!dialog) return;
    const input = $('cm-network-file'), result = $('cm-network-result'), button = $('cm-network-upload');
    $('cm-network-import').addEventListener('click', () => dialog.showModal());
    input.addEventListener('change', () => {
      $('cm-network-file-name').textContent = input.files?.[0]?.name || 'Nenhum arquivo selecionado';
    });
    button.addEventListener('click', async () => {
      const file = input.files?.[0];
      if (!file) { result.textContent = 'Escolha o arquivo GeoJSON.'; return; }
      const body = new FormData();
      body.append('file', file);
      button.disabled = true;
      result.textContent = 'Importando caixas…';
      try {
        const response = await fetch('/api/reports/credit-map/network', {
          method:'POST', credentials:'same-origin', headers:{'x-csrf-token':root.dataset.csrf}, body,
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || 'Falha ao importar caixas.');
        result.textContent = `${fmt(payload.naps)} caixas NAP importadas.`;
        await loadNetwork();
        refreshMap();
      } catch (error) { result.textContent = error.message; }
      finally { button.disabled = false; }
    });
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
        setTimeout(() => {state.map.invalidateSize();state.heatLayer?.redraw();}, 150);
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
  async function exportMyMaps(){
    if ($('cm-city').value === ALL_CITIES) { showMessage('Escolha uma cidade para gerar o arquivo do Google My Maps.'); return; }
    const query = new URLSearchParams({city: $('cm-city').value, date_from: $('cm-from').value, date_to: $('cm-to').value});
    const btn = $('cm-export-mymaps'); btn.disabled = true;
    try {
      const r = await fetch('/api/reports/credit-map/export-my-maps?' + query, {credentials: 'same-origin', cache: 'no-store'});
      if (r.status === 401) { window.location.assign('/login'); return; }
      if (!r.ok) { const body = await r.json().catch(() => ({})); throw new Error(body.detail || 'Falha ao gerar o arquivo'); }
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement('a'); a.href = url; a.download = 'mapa-credito-my-maps.csv'; a.click(); URL.revokeObjectURL(url);
      showMessage('Arquivo gerado. No Google My Maps: Importar > escolha este arquivo > coluna Endereço para posicionar > coluna Situação para o título.');
    } catch (err) { showMessage('Não foi possível gerar o arquivo: ' + err.message); }
    finally { btn.disabled = false; }
  }
  function initMyMapsExport(){ const btn = $('cm-export-mymaps'); if (btn) btn.addEventListener('click', exportMyMaps); }
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
  ['cm-city','cm-from','cm-to'].forEach(id => $(id).addEventListener('change', fetchData));
  ['cm-streets','cm-addresses'].forEach(id => { if ($(id)) $(id).addEventListener('change', () => {
    updateLegend();
    if (state.data?.all) return;
    state.revision += 1;
    drawDetailLayers(state.revision);
  }); });
  document.querySelectorAll('button[data-map-mode]').forEach(button => button.addEventListener('click', () => {
    const mode = button.dataset.mapMode;
    if (state.mode === mode || !['approved','rejected','compare','triangles'].includes(mode)) return;
    state.mode = mode;
    $('cm-metric').value = {approved:'approved',rejected:'rejected',compare:'approval_rate',triangles:'signed_approved'}[mode];
    $('cm-addresses').checked = mode === 'triangles';
    state.triangleFittedFor = '';
    if (state.data) drawAll();
  }));
  $('cm-neighborhood').addEventListener('change', () => {
    state.triangleFittedFor = '';
    if (state.data) {drawRanking(); refreshMap();}
  });
  $('cm-metric').addEventListener('change',()=>{if(state.data){drawKPIs();drawRanking();}});

  $('cm-search').addEventListener('input',drawRanking);
  $('cm-export').addEventListener('click',exportRanking);
  initImport();
  initFullscreen();
  initMyMapsExport();
  initNetwork();
  (async () => {await Promise.all([fetchCities(), loadNetwork()]); await fetchData();})();
})();
