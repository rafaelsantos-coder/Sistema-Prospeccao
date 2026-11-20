(() => {
  'use strict';
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
  function loadGoogleMaps({key, nonce, language, region}) {
    if (!key) return Promise.reject(new Error('Chave de mapas não configurada.'));
    if (window.google?.maps?.Map) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const callback = '__sulnetGoogleMapsInit';
      const timeout = setTimeout(() => finish(new Error('Google Maps não respondeu.')), 12000);
      let completed = false;
      const finish = (error) => {
        if (completed) return;
        completed = true;
        clearTimeout(timeout);
        delete window[callback];
        if (error) reject(error); else resolve();
      };
      window[callback] = () => finish();
      const src = new URL('https://maps.googleapis.com/maps/api/js');
      src.searchParams.set('key', key);
      src.searchParams.set('v', 'weekly');
      src.searchParams.set('loading', 'async');
      src.searchParams.set('callback', callback);
      if (language) src.searchParams.set('language', language);
      if (region) src.searchParams.set('region', region);
      const script = document.createElement('script');
      script.src = src.toString();
      script.async = true;
      if (nonce) script.nonce = nonce;
      script.onerror = () => finish(new Error('Não foi possível carregar Google Maps.'));
      document.head.appendChild(script);
    });
  }
  window.SulnetMapAssets = {loadGoogleMaps, loadLeaflet};
})();
