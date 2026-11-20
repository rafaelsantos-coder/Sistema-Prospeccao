'use strict';
const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const script = fs.readFileSync(path.join(__dirname, '../../app/static/js/credit_map_heat.js'), 'utf8');
const sandbox = {window: {}};
vm.runInNewContext(script, sandbox);
const heat = sandbox.window.SulnetCreditHeat;
const network = require('../../app/static/js/credit_map_network.js');
const feasibility = require('../../app/static/js/feasibility_map.js');

test('viabilidade aceita coordenadas brasileiras e copiadas do Google Maps', () => {
  const point = {lat: -28.3005, lng: -54.2633};
  assert.deepEqual(feasibility.parseCoordinates('-28.3005, -54.2633'), point);
  assert.deepEqual(feasibility.parseCoordinates('-28,3005; -54,2633'), point);
  assert.deepEqual(feasibility.parseCoordinates('-28,3005 -54,2633'), point);
  assert.deepEqual(feasibility.parseCoordinates('-28,3005, -54,2633'), point);
  assert.equal(feasibility.parseCoordinates('91, -54.2633'), null);
  assert.equal(feasibility.parseCoordinates('-28.3, -190'), null);
  assert.equal(feasibility.parseCoordinates('Rua Brasil, 120'), null);
});

test('viabilidade mede pelas coordenadas originais e prioriza caixas livres dentro do raio', () => {
  const make = (id, lat, livres, status = 'Online') => ({
    id, lat: lat + .01, lng: -54.26, original_lat: lat, original_lng: -54.26, livres, status,
  });
  const boxes = [
    make(1, -28.3001, 2, 'Offline'),
    make(2, -28.3002, 0),
    make(3, -28.3010, 4),
    make(4, -28.3008, 8, 'Vazia'),
    make(5, -28.3040, 4),
  ];
  const result = feasibility.nearbyBoxes(boxes, {lat: -28.30, lng: -54.26}, 250);
  assert.deepEqual(result.map(row => row.nap.id), [4, 3, 1, 2]);
  assert.deepEqual(result.map(row => network.available(row.nap)), [true, true, false, false]);
  assert.ok(result[0].meters > 85 && result[0].meters < 93);
});

const hoods = [
  {name: 'CENTRO', approved: 100, rejected: 25},
  {name: 'VILA', approved: 8, rejected: 50},
];
const positions = new Map([
  ['CENTRO', {lat: -28.30, lng: -54.26}],
  ['VILA', {lat: -28.32, lng: -54.28}],
]);
const streets = [
  {neighborhood: 'CENTRO', street: 'RUA UM', approved: 30, rejected: 10, lat: -28.301, lng: -54.261},
  {neighborhood: 'CENTRO', street: 'RUA DOIS', approved: 20, rejected: 5, lat: -28.302, lng: -54.262},
];

test('rede cruza GPS e ruas por proximidade sem duplicar consultas entre caixas', () => {
  const nap = (id, lat, livres, status = 'Online') => ({
    id, lat: lat + .0002, lng: -54.26, original_lat: lat, original_lng: -54.26, livres, status,
  });
  const naps = [nap(1, -28.300, 0), nap(2, -28.3001, 1, 'Offline'), nap(3, -28.301, 4)];
  const address = (count, signed, extra = {}) => ({
    lat: -28.300, lng: -54.26, count, signed, source_coordinates: true, ...extra,
  });
  const addresses = [
    address(2, false), address(3, true),
    address(99, false, {source_coordinates: false}), address(50, false, {lat: -28.31}),
  ];
  const streets = [{lat: -28.3, lng: -54.26, rejected: 7}];
  const matches = network.cross(naps, addresses, streets, 250);
  assert.deepEqual(matches.get(1), {approved: 3, signed: 3, unsigned: 0, opportunities: 0, rejected: 7});
  assert.deepEqual(matches.get(3), {approved: 2, signed: 0, unsigned: 2, opportunities: 2, rejected: 0});
  assert.equal(matches.has(2), false);
  assert.equal(network.cross(naps, addresses, streets, 100).get(1).opportunities, 0);
});

test('mapa verde usa GPS de aprovados e saldo aproximado do bairro sem duplicar consultas', () => {
  const gpsRows = [
    {neighborhood:'CENTRO', lat:-28.301, lng:-54.261, count:30, source_coordinates:true},
    {neighborhood:'VILA', lat:-28.321, lng:-54.281, count:5, source_coordinates:true},
  ];
  const rows = heat.makePoints(hoods, positions, streets, 'approved', gpsRows);
  assert.equal(rows.length, 4);
  assert.equal(rows.reduce((sum, row) => sum + row.count, 0), 108);
  assert.equal(rows.filter(row => row.source === 'gps').reduce((n, row) => n + row.count, 0), 35);
  assert.equal(rows.find(row => row.source === 'bairro' && row.label === 'CENTRO').count, 70);
  assert.equal(rows.find(row => row.source === 'bairro' && row.label === 'VILA').count, 3);
  assert.equal(rows.filter(row => row.source === 'rua').length, 0);
});

test('GPS de aprovados pode gerar calor mesmo sem centro de bairro localizado', () => {
  const rows = heat.makePoints([hoods[0]], new Map(), [], 'approved', [
    {neighborhood:'CENTRO', lat:-28.301, lng:-54.261, count:6, source_coordinates:true},
    {neighborhood:'CENTRO', lat:-28.302, lng:-54.262, count:5, source_coordinates:false},
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].count, 6);
  assert.equal(rows[0].source, 'gps');
  const refused = heat.makePoints([hoods[0]], new Map(), [], 'rejected');
  assert.equal(refused.length, 0);
});

test('mapa vermelho conta reprovacoes das mesmas regioes sem geolocalizar clientes', () => {
  const rows = heat.makePoints(hoods, positions, streets, 'rejected');
  assert.equal(rows.reduce((sum, row) => sum + row.count, 0), 75);
  assert.equal(rows.find(row => row.label === 'CENTRO').count, 10);
  assert.equal(rows.find(row => row.label === 'VILA').count, 50);
  assert.ok(rows.every(row => !('cpf' in row) && !('phone' in row)));
});

test('comparativo usa apenas bairros com pelo menos 20 decisoes e cor por taxa', () => {
  const rows = heat.makePoints([
    {name: 'CENTRO', approved: 9, rejected: 7},
    {name: 'VILA', approved: 16, rejected: 4},
  ], positions, streets, 'compare');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].label, 'VILA');
  assert.equal(rows[0].count, 20);
  assert.equal(rows[0].rate, 80);
  assert.equal(heat.modeColor('compare', 80), '#16a66f');
  assert.equal(heat.modeColor('compare', 60), '#e9aa35');
  assert.equal(heat.modeColor('compare', 30), '#e35b53');
});

test('bairros de mesmo nome de municipios diferentes nao sao fundidos', () => {
  const rows = [
    {name: 'CENTRO · Santa Rosa', hood: 'CENTRO', city: 'SANTA ROSA', approved: 50, rejected: 5},
    {name: 'CENTRO · Santo Ângelo', hood: 'CENTRO', city: 'SANTO ÂNGELO', approved: 30, rejected: 7},
  ];
  const coords = new Map([
    [rows[0].name, {lat: -27.87, lng: -54.48}],
    [rows[1].name, {lat: -28.30, lng: -54.26}],
  ]);
  const heatPoints = heat.makePoints(rows, coords, [], 'approved');
  assert.equal(heatPoints.length, 2);
  assert.equal(heatPoints.reduce((sum, x) => sum + x.count, 0), 80);
});

test('triangulos sao camada separada e coordenadas ausentes nao geram hotspots', () => {
  assert.equal(heat.makePoints(hoods, positions, streets, 'triangles').length, 0);
  const coords = new Map([['CENTRO', {lat: NaN, lng: -54.26}]]);
  assert.equal(heat.makePoints(hoods, coords, [], 'rejected').length, 0);
});

test('ruas nao podem exceder o total do bairro', () => {
  const tooLarge = [{neighborhood: 'CENTRO', street:'RUA TESTE', approved:900, rejected:800, lat:-28.3, lng:-54.25}];
  assert.equal(heat.makePoints([hoods[0]], positions, tooLarge, 'approved').reduce((a,b)=>a+b.count,0),100);
  assert.equal(heat.makePoints([hoods[0]], positions, tooLarge, 'rejected').reduce((a,b)=>a+b.count,0),25);
});


function fakeCanvasHarness() {
  let arcs = 0, removed = false;
  const queue = [];
  const ctx = {
    setTransform() {}, clearRect() {}, beginPath() {},
    createRadialGradient() {return {addColorStop() {}};},
    arc() {arcs++;}, fill() {},
  };
  const canvas = {
    width: 0, height: 0, hidden: false,
    setAttribute() {}, getContext() {return ctx;},
    remove() {removed = true;},
  };
  const container = {clientWidth: 800, clientHeight: 440, appendChild() {}};
  sandbox.window.devicePixelRatio = 1;
  sandbox.window.requestAnimationFrame = cb => {queue.push(cb);return queue.length;};
  sandbox.window.cancelAnimationFrame = () => {};
  function flush() {for (const cb of queue.splice(0)) cb();}
  return {canvas,container,flush,get arcs() {return arcs;},get removed(){return removed;}};
}

test('camada Leaflet redesenha o calor e limpa corretamente quando desativada', () => {
  const x = fakeCanvasHarness();
  const listeners = [];
  sandbox.window.document = {createElement: () => x.canvas};
  // O modulo usa document global do contexto, nao window.document.
  sandbox.document = sandbox.window.document;
  const code = fs.readFileSync(path.join(__dirname,'../../app/static/js/credit_map_heat.js'),'utf8');
  const env = {window: sandbox.window, document: sandbox.document};
  vm.runInNewContext(code, env);
  const map = {
    on(name, fn) {listeners.push(['on', name, fn]);},
    off(name, fn) {listeners.push(['off', name, fn]);},
    getZoom() {return 13;},
    latLngToContainerPoint() {return {x:400,y:210};},
  };
  const layer = env.window.SulnetCreditHeat.attach(map, 'osm', x.container);
  layer.set([{lat:-28.3,lng:-54.26,count:20,rate:90}], 'approved');
  x.flush();
  assert.equal(x.arcs, 1);
  assert.ok(x.canvas.width > 0);
  layer.clear();
  x.flush();
  assert.equal(x.canvas.hidden, true);
  layer.destroy();
  assert.equal(x.removed, true);
  assert.ok(listeners.some(x=>x[0]==='off'));
});

test('camada Google Maps usa projeção do mapa e reage aos eventos do zoom', () => {
  const x = fakeCanvasHarness();
  const callbacks = new Map();
  const api = {
    maps: {
      LatLng: class LatLng {constructor(lat,lng) {this.lat=lat;this.lng=lng;}},
      event: {addListener(_map,name,fn) {callbacks.set(name,fn);return {remove(){callbacks.delete(name);}};}},
    },
  };
  sandbox.window.google = api;
  const env = {window:sandbox.window,document:{createElement:()=>x.canvas}};
  const code = fs.readFileSync(path.join(__dirname,'../../app/static/js/credit_map_heat.js'),'utf8');
  vm.runInNewContext(code, env);
  const native = {
    getZoom() {return 12;},
    getCenter() {return {lat:-28.3,lng:-54.26};},
    getProjection() {return {fromLatLngToPoint(p) {return {x:(p.lng||-54.26)/360+0.5,y:(p.lat||-28.3)/360+0.5};}};},
  };
  const layer = env.window.SulnetCreditHeat.attach({native}, 'google', x.container);
  layer.set([{lat:-28.3,lng:-54.26,count:30,rate:25}], 'rejected');
  x.flush();
  assert.equal(x.arcs, 1);
  assert.ok(callbacks.has('zoom_changed'));
  callbacks.get('zoom_changed')();
  x.flush();
  assert.equal(x.arcs, 2);
  layer.destroy();
  assert.equal(callbacks.size,0);
});
