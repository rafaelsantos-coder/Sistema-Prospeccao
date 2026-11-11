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
  const state = {data: null, map: null, geocoder: null, info: null, layers: [], positions: new Map(), mapReady: false, mapLoading:false, revision:0};
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

  function geoDistanceKm(a,b) {
    const r = 6371, toRad = v=>v*Math.PI/180;
    const dLat = toRad(a.lat-b.lat), dLon = toRad(a.lng-b.lng);
    const h = Math.sin(dLat/2)**2 + Math.cos(toRad(b.lat))*Math.cos(toRad(a.lat))*Math.sin(dLon/2)**2;
    return 2*r*Math.atan2(Math.sqrt(h),Math.sqrt(1-h));
  }
  async function loadGoogle() {
    const key = root.dataset.mapsKey;
    if(!key) {showMapPlaceholder('Para ativar o Google Maps, adicione GOOGLE_MAPS_BROWSER_KEY nas variáveis do serviço web. O ranking e os indicadores já funcionam sem o mapa.'); return;}
    if (state.mapLoading || state.mapReady) return;
    state.mapLoading = true;
    await new Promise((resolve, reject) => {
      const script = document.createElement('script');
      window.__sulnetCreditMapInit = () => resolve();
      script.src = 'https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(key) + '&callback=__sulnetCreditMapInit&v=weekly&language=pt-BR&region=BR';
      script.async = true; script.defer = true; script.onerror = () => reject(Error('Falha na API do Google Maps.'));
      document.head.appendChild(script);
    }).then(() => {
      state.map = new google.maps.Map($('cm-map'), {center:{lat:-28.30,lng:-54.26},zoom:12,gestureHandling:'cooperative',streetViewControl:false,fullscreenControl:true,mapTypeControl:false,styles:[{featureType:'poi',stylers:[{visibility:'off'}]},{featureType:'water',elementType:'geometry',stylers:[{color:'#dcebec'}]},{featureType:'road',elementType:'geometry',stylers:[{color:'#ffffff'}]},{featureType:'landscape',elementType:'geometry',stylers:[{color:'#eff2ef'}]}]});
      state.geocoder = new google.maps.Geocoder();state.info = new google.maps.InfoWindow();state.mapReady=true;
      $('cm-map-empty').hidden=true;
    }).catch(err => { showMapPlaceholder(err.message); $('cm-geocode-status').textContent = 'Google Maps indisponível. Confira chave, faturamento e restrição de domínio.'; });
    state.mapLoading = false;
  }
  function clearLayers(){ for(const circle of state.layers) circle.setMap(null);state.layers=[];if(state.info)state.info.close(); }
  function locationLooksRight(result, name, cityCenter) {
    const nameFold = folded(name).replace(/^BAIRRO\s+/,'');
    const address = folded(result.formatted_address || '');
    const componentMatches = (result.address_components || []).some(c=>folded(c.long_name)===nameFold);
    const plausibleName = componentMatches || address.includes(nameFold);
    const loc = result.geometry?.location;
    return !!(loc && plausibleName && geoDistanceKm({lat:loc.lat(),lng:loc.lng()},cityCenter) <= 32);
  }
  async function geocodeNeighborhood(name, cityCenter) {
    const key = `${$('cm-city').value}::${name}`;
    if (state.positions.has(key)) return state.positions.get(key);
    try {
      const city = $('cm-city').value;
      const res = await state.geocoder.geocode({address:`Bairro ${name}, ${city}, Rio Grande do Sul, Brasil`,region:'BR'});
      const found = (res.results || []).find(x => locationLooksRight(x, name, cityCenter));
      const point = found ? {lat:found.geometry.location.lat(),lng:found.geometry.location.lng()} : null;
      state.positions.set(key, point);
      return point;
    } catch {state.positions.set(key,null);return null;}
  }
  function infoHtml(row) {
    return `<div class="cm-map-info"><strong>${esc(row.name)}</strong><br><small>${fmt(row.total)} consultas no período</small><br>Aprovados: <b>${fmt(row.approved)}</b> · ${pct(row.approval_rate)}<br>Reprovados: <b>${fmt(row.rejected)}</b><br>Contratos assinados: <b>${fmt(row.signed_approved)}</b> · ${pct(row.signed_rate)}</div>`;
  }
  function plotRow(row, point, max){
    const v = Number(metricValue(row) || 0);
    const intensity = Math.sqrt(v / Math.max(1,max));
    const radius = 130 + 580*intensity;
    const color = layerColor(row);
    const outer=new google.maps.Circle({map:state.map,center:point,radius,fillColor:color,fillOpacity:.20,strokeColor:color,strokeOpacity:.25,strokeWeight:1,zIndex:10});
    const inner=new google.maps.Circle({map:state.map,center:point,radius:Math.max(70,radius*.45),fillColor:color,fillOpacity:.38,strokeOpacity:0,zIndex:11});
    const click = () => {state.map.panTo(point); state.info.setContent(infoHtml(row));state.info.setPosition(point);state.info.open(state.map);};
    outer.addListener('click',click);inner.addListener('click',click);
    state.layers.push(outer,inner);
  }
  async function refreshMap() {
    if (!state.data) return;
    await loadGoogle();
    if(!state.mapReady) return;
    const ticket=++state.revision;
    clearLayers();
    const rows=state.data.neighborhoods || [];
    if(!rows.length) {$('cm-geocode-status').textContent='Nenhum bairro com amostra suficiente neste período.';return;}
    let cityCenter={lat:-28.30,lng:-54.26};
    try {
      const res=await state.geocoder.geocode({address:`${$('cm-city').value}, Rio Grande do Sul, Brasil`,region:'BR'});
      if (res.results?.[0]?.geometry?.location) {
        const location = res.results[0].geometry.location;
        cityCenter={lat:location.lat(),lng:location.lng()};
      }
    } catch { /* mantém apenas coordenada de centralização; não fabrica bairros */ }
    if(ticket!==state.revision) return;
    state.map.setCenter(cityCenter); state.map.setZoom(12);
    const max=Math.max(1,...rows.map(r=>Number(metricValue(r)||0)));
    let located=0; const allBounds=new google.maps.LatLngBounds();
    for (const row of rows) {
      if(ticket!==state.revision) return;
      const p=await geocodeNeighborhood(row.name,cityCenter);
      if(p) {plotRow(row,p,max);allBounds.extend(p);located++;}
      $('cm-geocode-status').textContent = `${located} de ${rows.length} bairros localizados · mapas representam centros aproximados`;
    }
    if(located>=2) {state.map.fitBounds(allBounds,62);const once=google.maps.event.addListenerOnce(state.map,'idle',()=>{if(state.map.getZoom()>13) state.map.setZoom(13);});}
    if(!located) $('cm-geocode-status').textContent='Bairros sem geocodificação confiável. O ranking continua válido; revise as posições antes de usar o mapa.';
  }
  function locateRow(name) {
    if(!state.mapReady) {showMessage('O mapa precisa da chave Google Maps para localizar bairros.');return;}
    const key=$('cm-city').value+'::'+name;
    const point=state.positions.get(key);
    if(!point) {$('cm-geocode-status').textContent=`Bairro ${name}: localização geográfica não validada; consulte os indicadores no ranking.`;return;}
    const row=(state.data?.neighborhoods||[]).find(x=>x.name===name);
    state.map.panTo(point);state.map.setZoom(Math.max(state.map.getZoom(),13));
    if(row){state.info.setContent(infoHtml(row));state.info.setPosition(point);state.info.open(state.map);}
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
  (async () => {await fetchCities(); await fetchData();})();
})();
