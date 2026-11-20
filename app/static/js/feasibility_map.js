(function (root) {
  'use strict';
  const isNode = typeof module === 'object' && module.exports;
  const network = isNode ? require('./credit_map_network.js') : root.SulnetCreditNetwork;
  const initialCenter = {lat: -27.87, lng: -54.48};
  const colors = {free: '#2563eb', unavailable: '#64748b', target: '#e9681b'};
  const coordinate = '[+-]?\\d+(?:[.,]\\d+)?';
  const localizedPair = new RegExp('^(' + coordinate + ')(?:\\s*;\\s*|\\s*,\\s+|\\s+)(' + coordinate + ')$');
  const decimalPair = /^([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)$/;

  function parseCoordinates(text) {
    const input = text.trim();
    const match = decimalPair.exec(input) || localizedPair.exec(input);
    if (!match) return null;
    const [lat, lng] = match.slice(1).map(value => Number(value.replace(',', '.')));
    return Math.abs(lat) <= 90 && Math.abs(lng) <= 180 ? {lat, lng} : null;
  }

  function nearbyBoxes(naps, point, radius) {
    return naps
      .map(nap => ({nap, meters: network.distance(point.lat, point.lng, nap.original_lat, nap.original_lng)}))
      .filter(row => row.meters <= radius)
      .sort((a, b) => Number(network.available(b.nap)) - Number(network.available(a.nap))
        || a.meters - b.meters || a.nap.id - b.nap.id);
  }

  function boundsOf(naps) {
    if (!naps.length) return null;
    const bounds = {north: -90, south: 90, east: -180, west: 180};
    for (const nap of naps) {
      bounds.north = Math.max(bounds.north, nap.lat);
      bounds.south = Math.min(bounds.south, nap.lat);
      bounds.east = Math.max(bounds.east, nap.lng);
      bounds.west = Math.min(bounds.west, nap.lng);
    }
    return bounds;
  }

  if (isNode) {
    module.exports = {parseCoordinates, nearbyBoxes, boundsOf};
    return;
  }

  const $ = id => document.getElementById(id);
  const page = $('feasibility-map-app');
  if (!page) return;
  const state = {naps: null, map: null, provider: null, search: null, cities: new Map()};
  const city = $('vm-city');
  const addressInput = $('vm-address');
  const addressButton = $('vm-address-submit');
  const addressStatus = $('vm-address-status');
  let addressWidget = null;
  let requestVersion = 0;
  let googleDenied = false;
  const fmt = number => number.toLocaleString('pt-BR');
  const plural = (number, singular, pluralForm) => fmt(number) + ' ' + (number === 1 ? singular : pluralForm);
  const pointOf = nap => ({lat: nap.lat, lng: nap.lng});
  const colorOf = nap => network.available(nap) ? colors.free : colors.unavailable;
  const visibleBoxes = () => city.value ? state.cities.get(city.value) || [] : state.naps || [];

  function boxInfo(nap, meters) {
    const content = document.createElement('div');
    content.className = 'vm-popup';
    const title = document.createElement('strong');
    title.textContent = nap.nome;
    const detail = document.createElement('span');
    const ports = plural(nap.livres, 'porta livre', 'portas livres');
    detail.textContent = [nap.city, ports + ' de ' + fmt(nap.portas), nap.status].filter(Boolean).join(' · ');
    content.append(title, detail);
    if (meters != null) {
      const distance = document.createElement('span');
      distance.textContent = fmt(Math.round(meters)) + ' m do local pesquisado';
      content.append(distance);
    }
    return content;
  }

  function googleMap() {
    const maps = root.google.maps;
    const map = new maps.Map($('vm-map'), {
      center: initialCenter, zoom: 10, mapId: page.dataset.googleMapId,
      mapTypeControl: true, streetViewControl: false, gestureHandling: 'cooperative',
    });
    const boxes = new maps.Data({map});
    const pin = new maps.Data({map});
    const popup = new maps.InfoWindow();
    const icon = (color, scale) => ({
      path: maps.SymbolPath.CIRCLE, scale, fillColor: color, fillOpacity: .6,
      strokeColor: color, strokeOpacity: .9, strokeWeight: 1.25,
    });
    const availableIcon = icon(colors.free, 4);
    const unavailableIcon = icon(colors.unavailable, 4);
    boxes.setStyle(feature => ({
      icon: network.available(feature.getProperty('nap')) ? availableIcon : unavailableIcon,
      title: feature.getProperty('nap').nome,
    }));
    pin.setStyle({icon: icon(colors.target, 8), zIndex: 10, clickable: false});
    boxes.addListener('click', ({feature}) => openBox(feature.getProperty('nap')));
    let features = [];
    let target = null;

    return {
      showBoxes(naps) {
        popup.close();
        for (const feature of features) boxes.remove(feature);
        features = naps.map(nap => boxes.add({
          geometry: new maps.Data.Point(pointOf(nap)),
          properties: {nap},
        }));
      },
      fit(bounds) {
        if (!bounds) return;
        map.fitBounds(bounds, 40);
        maps.event.addListenerOnce(map, 'idle', () => {
          if (map.getZoom() > 16) map.setZoom(16);
        });
      },
      focus(point, zoom) { map.setCenter(point); map.setZoom(zoom); },
      target(point) {
        if (target) pin.remove(target);
        target = point ? pin.add({geometry: new maps.Data.Point(point)}) : null;
      },
      popup(point, content) { popup.setPosition(point); popup.setContent(content); popup.open({map}); },
    };
  }

  function osmMap() {
    const L = root.L;
    const map = L.map($('vm-map'), {center: initialCenter, zoom: 10, preferCanvas: true, scrollWheelZoom: false});
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" rel="noopener">OpenStreetMap</a>',
    }).addTo(map);
    const boxes = L.layerGroup().addTo(map);
    const target = L.layerGroup().addTo(map);
    return {
      showBoxes(naps) {
        map.closePopup();
        boxes.clearLayers();
        for (const nap of naps) {
          const color = colorOf(nap);
          L.circleMarker(pointOf(nap), {radius: 4, color, fillColor: color, fillOpacity: .6, weight: 1.25})
            .on('click', () => openBox(nap)).addTo(boxes);
        }
      },
      fit(bounds) {
        if (bounds) map.fitBounds([[bounds.south, bounds.west], [bounds.north, bounds.east]], {padding: [40, 40], maxZoom: 16});
      },
      focus(point, zoom) { map.setView(point, zoom); },
      target(point) {
        target.clearLayers();
        if (point) L.circleMarker(point, {radius: 8, color: colors.target, fillOpacity: .6, interactive: false}).addTo(target);
      },
      popup(point, content) { L.popup().setLatLng(point).setContent(content).openOn(map); },
    };
  }

  function openBox(nap, meters) {
    if (!state.map) return;
    const point = pointOf(nap);
    state.map.focus(point, 17);
    state.map.popup(point, boxInfo(nap, meters));
  }

  function renderResults() {
    if (!state.naps) return;
    const {search} = state;
    const radius = Number($('vm-radius').value);
    const rows = search ? nearbyBoxes(state.naps, search.point, radius) : [];
    const area = search ? rows.map(row => row.nap) : visibleBoxes();
    const available = area.filter(network.available);
    const ports = available.reduce((total, nap) => total + nap.livres, 0);
    const list = $('vm-results');
    list.replaceChildren();
    $('vm-result-target').textContent = search?.label || city.value;
    $('vm-result-target').hidden = !search && !city.value;
    $('vm-search-radius').hidden = !search;
    $('vm-radius-note').hidden = !search || area.length === 0;

    let summary;
    if (!state.naps.length) {
      summary = 'Nenhuma caixa NAP na base importada.';
    } else if (search) {
      if (!area.length) summary = 'Nenhuma caixa da base a até ' + radius + ' m.';
      else if (!available.length) summary = 'Sem caixas com portas livres a até ' + radius + ' m.';
      else summary = plural(ports, 'porta livre', 'portas livres') + ' em ' +
        plural(available.length, 'caixa', 'caixas') + ' a até ' + radius + ' m.';
    } else {
      summary = plural(area.length, 'caixa importada', 'caixas importadas') + ' · ' +
        plural(ports, 'porta livre', 'portas livres');
    }
    $('vm-summary').textContent = summary;
    for (const {nap, meters} of rows.slice(0, 12)) {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'vm-result-button' + (network.available(nap) ? ' is-available' : '');
      const content = boxInfo(nap, meters);
      button.append(...content.children);
      button.addEventListener('click', () => openBox(nap, meters));
      item.append(button);
      list.append(item);
    }
  }

  function showScope() {
    const naps = visibleBoxes();
    const bounds = boundsOf(naps);
    const {map, search} = state;
    if (addressWidget) addressWidget.locationBias = bounds;
    if (map) {
      map.showBoxes(naps);
      map.target(search?.point);
      if (search) map.focus(search.point, 16);
      else map.fit(bounds);
    }
    renderResults();
  }

  function showPoint(point, label, source, query = '') {
    const wasFiltered = Boolean(city.value);
    city.value = '';
    state.search = {point, label, source, query};
    if (wasFiltered) showScope();
    else {
      state.map?.target(point);
      state.map?.focus(point, 16);
      renderResults();
    }
  }

  function usePlainAddress(message) {
    if (addressWidget) {
      addressInput.value = addressWidget.value || '';
      addressWidget.remove();
      addressWidget = null;
    }
    addressInput.hidden = false;
    $('vm-address-label').htmlFor = addressInput.id;
    addressStatus.textContent = message;
  }

  async function locateAddress(query, lookup) {
    const ticket = ++requestVersion;
    addressStatus.textContent = 'Localizando endereço…';
    try {
      const {location, label} = await lookup();
      if (ticket !== requestVersion || state.provider !== 'google') return;
      if (!location) {
        addressStatus.textContent = 'Endereço não encontrado. Informe rua, número e cidade.';
        return;
      }
      const point = {lat: location.lat(), lng: location.lng()};
      showPoint(point, label || query, 'address', query);
      addressStatus.textContent = 'Endereço localizado.';
    } catch {
      if (ticket === requestVersion) addressStatus.textContent = 'Não foi possível localizar o endereço. Tente novamente ou informe as coordenadas.';
    }
  }

  async function setupAddress() {
    addressInput.disabled = false;
    addressButton.disabled = false;
    try {
      const {PlaceAutocompleteElement} = await root.google.maps.importLibrary('places');
      if (googleDenied) return;
      const widget = new PlaceAutocompleteElement({
        includedRegionCodes: ['br'], requestedRegion: 'br', requestedLanguage: 'pt-BR',
        locationBias: boundsOf(visibleBoxes()) || undefined,
        placeholder: 'Rua, número e cidade',
        description: 'Endereço para consultar a viabilidade',
        maxlength: 220,
      });
      widget.id = 'vm-google-address';
      widget.addEventListener('gmp-error', () => {
        usePlainAddress('Sugestões do Google indisponíveis. Digite o endereço completo e clique em Buscar.');
      });
      widget.addEventListener('gmp-select', ({placePrediction}) => {
        const query = (widget.value || placePrediction.text.toString()).trim();
        void locateAddress(query, async () => {
          const place = placePrediction.toPlace();
          await place.fetchFields({fields: ['formattedAddress', 'location']});
          return {location: place.location, label: place.formattedAddress};
        });
      });
      widget.value = addressInput.value;
      addressWidget = widget;
      addressInput.hidden = true;
      $('vm-address-label').htmlFor = widget.id;
      $('vm-address-control').append(widget);
      addressStatus.textContent = 'Escolha uma sugestão do Google ou clique em Buscar.';
    } catch {
      if (!googleDenied) usePlainAddress('Digite o endereço completo e clique em Buscar.');
    }
  }

  async function setupMap() {
    const status = $('vm-map-status');
    try {
      if (page.dataset.googleMapsKey) {
        root.gm_authFailure = () => {
          googleDenied = true;
          requestVersion += 1;
          usePlainAddress('Busca por endereço indisponível. Use cidade ou coordenadas.');
          addressInput.disabled = true;
          addressButton.disabled = true;
          if (state.provider === 'google') {
            state.provider = null;
            state.map = null;
            if (state.search?.source === 'address') state.search = null;
            $('vm-map').replaceChildren();
            status.hidden = false;
            status.textContent = 'Carregando mapa…';
            void setupMap();
          }
        };
        if (!googleDenied) {
          try {
            await root.SulnetMapAssets.loadGoogleMaps({
              key: page.dataset.googleMapsKey, nonce: page.dataset.mapNonce, language: 'pt-BR', region: 'BR',
            });
            if (!googleDenied) {
              state.map = googleMap();
              state.provider = 'google';
              void setupAddress();
            }
          } catch {
            $('vm-map').replaceChildren();
            usePlainAddress('Busca por endereço indisponível. Use cidade ou coordenadas.');
          }
        }
      }
      if (!state.map) {
        await root.SulnetMapAssets.loadLeaflet();
        state.map = osmMap();
        state.provider = 'osm';
      }
      showScope();
      status.hidden = true;
    } catch {
      status.textContent = 'Mapa indisponível. A consulta de caixas continua disponível.';
    }
  }

  async function loadSnapshot() {
    try {
      const response = await fetch(page.dataset.networkUrl, {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('Base indisponível');
      const data = await response.json();
      state.naps = data.naps;
      for (const nap of data.naps) {
        if (!nap.city) continue;
        if (!state.cities.has(nap.city)) state.cities.set(nap.city, []);
        state.cities.get(nap.city).push(nap);
      }
      for (const name of [...state.cities.keys()].sort((a, b) => a.localeCompare(b, 'pt-BR'))) {
        city.add(new Option(name, name));
      }
      city.disabled = state.cities.size === 0;
      const [date, time] = (data.reference_at || '').split('T');
      if (date && time) {
        $('vm-reference').textContent = 'Base de ' + date.split('-').reverse().join('/') + ' às ' + time.slice(0, 5);
        $('vm-reference').hidden = false;
      }
      showScope();
    } catch {
      $('vm-summary').textContent = 'Base de caixas indisponível agora.';
    }
  }

  city.addEventListener('change', () => {
    requestVersion += 1;
    state.search = null;
    showScope();
  });
  $('vm-radius').addEventListener('change', renderResults);
  $('vm-coordinate-form').addEventListener('submit', event => {
    event.preventDefault();
    const input = $('vm-coordinate');
    const point = parseCoordinates(input.value);
    input.setAttribute('aria-invalid', String(!point));
    const status = $('vm-coordinate-status');
    if (!point) {
      status.textContent = 'Informe latitude e longitude válidas, como -28.3005, -54.2633.';
      return;
    }
    requestVersion += 1;
    status.textContent = 'Coordenadas localizadas.';
    showPoint(point, input.value.trim(), 'coordinates');
  });
  $('vm-address-form').addEventListener('submit', event => {
    event.preventDefault();
    if (state.provider !== 'google') return;
    const query = (addressWidget?.value ?? addressInput.value).trim();
    if (!query) {
      addressStatus.textContent = 'Informe rua, número e cidade.';
      return;
    }
    if (state.search?.source === 'address' && state.search.query === query) {
      state.map.focus(state.search.point, 16);
      return;
    }
    const bounds = boundsOf(visibleBoxes());
    void locateAddress(query, async () => {
      const {Geocoder} = await root.google.maps.importLibrary('geocoding');
      const {results} = await new Geocoder().geocode({
        address: query, bounds: bounds || undefined, region: 'br',
        componentRestrictions: {country: 'BR'}, fulfillOnZeroResults: true,
      });
      const result = results[0];
      const approximate = result?.partial_match || ['APPROXIMATE', 'GEOMETRIC_CENTER'].includes(result?.geometry.location_type);
      return {
        location: result?.geometry.location,
        label: result ? result.formatted_address + (approximate ? ' · Posição aproximada' : '') : '',
      };
    });
  });

  void loadSnapshot();
  void setupMap();
})(typeof window === 'undefined' ? globalThis : window);
