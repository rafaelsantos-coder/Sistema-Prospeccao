/* Sulnet Prospecta — calor territorial aproximado. Nenhuma localizacao individual de reprovados. */
(() => {
  'use strict';
  const MIN_COMPARE_DECISIONS = 20;
  const clamp = (x, low, high) => Math.max(low, Math.min(high, x));
  const normalized = value => String(value || '').normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '').trim().toUpperCase();

  function modeColor(mode, rate) {
    if (mode === 'approved') return '#16a66f';
    if (mode === 'rejected') return '#e35b53';
    if (rate >= 75) return '#16a66f';
    if (rate >= 50) return '#e9aa35';
    return '#e35b53';
  }

  /* Cada agregado entra uma vez: ruas localizadas nos seus pontos e o saldo
   * no centro aproximado do bairro. Comparativo exige 20+ decisoes. */
  function makePoints(neighborhoods, positions, streets, mode, gpsRows = []) {
    if (mode === 'triangles') return [];
    const eligible = new Map();
    for (const row of neighborhoods || []) {
      const point = positions.get(row.name);
      const located = point && Number.isFinite(+point.lat) && Number.isFinite(+point.lng);
      // Aprovados com GPS valido independem da geocodificacao do centro do bairro.
      if (!located && mode !== 'approved') continue;
      const approved = Math.max(0, Number(row.approved) || 0);
      const rejected = Math.max(0, Number(row.rejected) || 0);
      const decided = approved + rejected;
      if (mode === 'compare' && decided < MIN_COMPARE_DECISIONS) continue;
      eligible.set(normalized(row.city ? row.name : (row.hood || row.name)), {
        position: located ? point : null, approved, rejected, decided, name: row.name,
        rate: decided ? approved / decided * 100 : 0,
        remaining: mode === 'rejected' ? rejected : approved,
      });
    }
    const points = [];
    if (mode === 'approved') {
      // GPS da coluna Q tem prioridade: nao usar, em paralelo, os agregados
      // de rua que incluem os mesmos clientes e poderiam distorcer a posicao.
      for (const gps of gpsRows || []) {
        if (!gps.source_coordinates || !Number.isFinite(+gps.lat) || !Number.isFinite(+gps.lng)) continue;
        const hood = eligible.get(normalized(gps.neighborhood));
        if (!hood) continue;
        const count = Math.min(hood.remaining, Math.max(0, Number(gps.count) || 0));
        if (!count) continue;
        hood.remaining -= count;
        points.push({lat: +gps.lat, lng: +gps.lng, count,
          rate: hood.rate, source: 'gps', label: hood.name});
      }
    } else if (mode === 'rejected') {
      for (const street of streets || []) {
        const hood = eligible.get(normalized(street.neighborhood));
        if (!hood || !Number.isFinite(+street.lat) || !Number.isFinite(+street.lng)) continue;
        const count = Math.min(hood.remaining, Math.max(0, Number(street.rejected) || 0));
        if (!count) continue;
        hood.remaining -= count;
        points.push({lat: +street.lat, lng: +street.lng, count,
          rate: hood.rate, source: 'rua', label: street.street});
      }
    }
    for (const hood of eligible.values()) {
      const count = mode === 'compare' ? hood.decided : hood.remaining;
      if (!count || !hood.position) continue;
      points.push({lat: +hood.position.lat, lng: +hood.position.lng, count,
        rate: hood.rate, source: 'bairro', label: hood.name});
    }
    return points;
  }

  function hexRgba(hex, opacity) {
    const value = hex.replace('#', '');
    return 'rgba(' + parseInt(value.slice(0,2),16) + ',' +
      parseInt(value.slice(2,4),16) + ',' + parseInt(value.slice(4,6),16) + ',' +
      clamp(opacity, 0, 1) + ')';
  }

  function attach(map, provider, container) {
    const canvas = document.createElement('canvas');
    canvas.className = 'cm-heat-canvas';
    canvas.setAttribute('aria-hidden', 'true');
    container.appendChild(canvas);
    let points = [], mode = 'approved', destroyed = false, frame = 0;
    const native = provider === 'google' ? map.native : map;
    const listeners = [];

    function project(lat, lng, w, h) {
      if (provider === 'google') {
        const projection = native.getProjection?.();
        const center = native.getCenter?.();
        const zoom = native.getZoom?.();
        if (!projection || !center || !Number.isFinite(zoom)) return null;
        const c = projection.fromLatLngToPoint(center);
        const p = projection.fromLatLngToPoint(new window.google.maps.LatLng(lat, lng));
        if (!c || !p) return null;
        const scale = Math.pow(2, zoom);
        return {x: (p.x - c.x) * scale + w/2, y: (p.y - c.y) * scale + h/2};
      }
      const p = native.latLngToContainerPoint([lat, lng]);
      return {x: p.x, y: p.y};
    }

    function paint() {
      if (destroyed) return;
      const w = container.clientWidth, h = container.clientHeight;
      if (!w || !h) return;
      const dpr = clamp(window.devicePixelRatio || 1, 1, 2);
      const pw = Math.round(w * dpr), ph = Math.round(h * dpr);
      if (canvas.width !== pw || canvas.height !== ph) {
        canvas.width = pw; canvas.height = ph;
      }
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      if (mode === 'triangles' || !points.length) return;
      const max = Math.max(1, ...points.map(x => x.count));
      const zoom = provider === 'google' ? (native.getZoom() || 12) : native.getZoom();
      // Raio visual em pixels, sem os antigos circulos de 130 a 710 metros.
      const radius = clamp(43 + (zoom - 12) * 4, 35, 65);
      ctx.globalCompositeOperation = 'source-over';
      for (const point of points) {
        const pixel = project(point.lat, point.lng, w, h);
        if (!pixel || !Number.isFinite(pixel.x) || !Number.isFinite(pixel.y)) continue;
        if (pixel.x < -radius || pixel.y < -radius || pixel.x > w+radius || pixel.y > h+radius) continue;
        const strength = clamp(Math.sqrt(point.count / max), .12, 1);
        const color = modeColor(mode, point.rate);
        const alpha = (mode === 'compare' ? .68 : .8) * strength;
        const gradient = ctx.createRadialGradient(pixel.x, pixel.y, 0, pixel.x, pixel.y, radius);
        gradient.addColorStop(0, hexRgba(color, alpha));
        gradient.addColorStop(.24, hexRgba(color, alpha * .83));
        gradient.addColorStop(.64, hexRgba(color, alpha * .34));
        gradient.addColorStop(1, hexRgba(color, 0));
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(pixel.x, pixel.y, radius, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    function schedule() {
      if (destroyed || frame) return;
      frame = window.requestAnimationFrame(() => {frame = 0; paint();});
    }

    const resizeObserver = typeof window.ResizeObserver === 'function'
      ? new window.ResizeObserver(schedule) : null;
    resizeObserver?.observe(container);
    if (provider === 'google') {
      for (const event of ['idle', 'zoom_changed', 'center_changed', 'projection_changed']) {
        listeners.push(window.google.maps.event.addListener(native, event, schedule));
      }
    } else native.on('move zoom resize', schedule);
    schedule();

    return {
      set(nextPoints, nextMode) {
        points = (nextPoints || []).filter(x =>
          Number.isFinite(x.lat) && Number.isFinite(x.lng) && x.count > 0);
        mode = nextMode;
        canvas.hidden = mode === 'triangles';
        schedule();
      },
      clear() {points = []; mode = 'triangles'; canvas.hidden = true; schedule();},
      redraw: schedule,
      destroy() {
        destroyed = true;
        if (frame) window.cancelAnimationFrame(frame);
        resizeObserver?.disconnect();
        if (provider === 'google') listeners.forEach(x => x.remove());
        else native.off('move zoom resize', schedule);
        canvas.remove();
      },
    };
  }

  window.SulnetCreditHeat = {makePoints, modeColor, attach, MIN_COMPARE_DECISIONS};
})();
