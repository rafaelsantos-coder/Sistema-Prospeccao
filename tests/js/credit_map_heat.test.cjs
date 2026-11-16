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

test('mapa verde reparte volume entre ruas e saldo do bairro sem duplicar consultas', () => {
  const rows = heat.makePoints(hoods, positions, streets, 'approved');
  assert.equal(rows.length, 4);
  assert.equal(rows.reduce((sum, row) => sum + row.count, 0), 108);
  assert.equal(rows.filter(row => row.source === 'rua').reduce((n, row) => n + row.count, 0), 50);
  assert.equal(rows.find(row => row.label === 'CENTRO').count, 50);
  assert.equal(rows.find(row => row.label === 'VILA').count, 8);
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
