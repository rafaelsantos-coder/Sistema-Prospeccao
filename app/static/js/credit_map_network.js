/* Cruzamento geográfico de caixas NAP e consultas já autorizadas no painel. */
(function (root, factory) {
  const network = factory();
  if (typeof module === 'object' && module.exports) module.exports = network;
  else root.SulnetCreditNetwork = network;
})(typeof window === 'undefined' ? globalThis : window, function () {
  'use strict';
  const DEG = Math.PI / 180;
  const EARTH_METERS = 6371008.8;
  const CELL = .01;
  const available = nap => nap.livres > 0 && nap.status !== 'Offline';
  const key = (lat, lng) => Math.floor(lat / CELL) + ',' + Math.floor(lng / CELL);

  function distance(lat, lng, otherLat, otherLng) {
    const a = Math.sin((otherLat - lat) * DEG / 2) ** 2 +
      Math.cos(lat * DEG) * Math.cos(otherLat * DEG) * Math.sin((otherLng - lng) * DEG / 2) ** 2;
    return 2 * EARTH_METERS * Math.asin(Math.sqrt(Math.min(1, a)));
  }

  function index(naps) {
    const grid = new Map();
    for (const nap of naps) {
      const cell = key(nap.original_lat, nap.original_lng);
      if (!grid.has(cell)) grid.set(cell, []);
      grid.get(cell).push(nap);
    }
    return grid;
  }

  function candidates(grid, lat, lng, radius) {
    const dy = radius / 110000;
    const dx = dy / Math.max(.01, Math.cos(lat * DEG));
    let nearest = null, free = null, shortest = radius, shortestFree = radius;
    for (let y = Math.floor((lat - dy) / CELL); y <= Math.floor((lat + dy) / CELL); y += 1) {
      for (let x = Math.floor((lng - dx) / CELL); x <= Math.floor((lng + dx) / CELL); x += 1) {
        for (const nap of grid.get(y + ',' + x) || []) {
          const meters = distance(lat, lng, nap.original_lat, nap.original_lng);
          if (meters <= radius && (!nearest || meters < shortest || (meters === shortest && nap.id < nearest.id))) {
            nearest = nap;
            shortest = meters;
          }
          if (available(nap) && meters <= radius &&
              (!free || meters < shortestFree || (meters === shortestFree && nap.id < free.id))) {
            free = nap;
            shortestFree = meters;
          }
        }
      }
    }
    return {nearest, free};
  }

  function cross(naps, addresses, streets, radius) {
    const grid = index(naps);
    const matches = new Map();
    const counts = nap => {
      if (!matches.has(nap.id)) {
        matches.set(nap.id, {approved: 0, signed: 0, unsigned: 0, opportunities: 0, rejected: 0});
      }
      return matches.get(nap.id);
    };
    const usable = row => Number.isFinite(row.lat) && Number.isFinite(row.lng);

    for (const row of addresses) {
      if (!row.source_coordinates || !usable(row)) continue;
      const {nearest, free} = candidates(grid, row.lat, row.lng, radius);
      const target = row.signed ? nearest : free || nearest;
      if (!target) continue;
      const match = counts(target);
      match.approved += row.count;
      if (row.signed) match.signed += row.count;
      else {
        match.unsigned += row.count;
        if (free) match.opportunities += row.count;
      }
    }
    for (const row of streets) {
      if (!row.rejected || !usable(row)) continue;
      const {nearest} = candidates(grid, row.lat, row.lng, radius);
      if (nearest) counts(nearest).rejected += row.rejected;
    }
    return matches;
  }

  return {available, distance, cross};
});
