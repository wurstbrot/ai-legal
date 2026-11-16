'use strict';
// Unit-Tests der Allowlist des Egress-Proxys (node --test), inkl. Negativfällen (TM-AC-7).

const test = require('node:test');
const assert = require('node:assert');
const http = require('node:http');
const { parseAllow, bereinigeHost, istErlaubt, parseZiel, istPrivat, erstelleServer } = require('./egress-proxy.js');

const allow = parseAllow('api.anthropic.com, dejure.org,ungültig!,');

test('Allowlist parst nur gültige Domains', () => {
  assert.deepStrictEqual(allow, ['api.anthropic.com', 'dejure.org']);
});

test('exakte Domain und Subdomain sind erlaubt', () => {
  assert.ok(istErlaubt('api.anthropic.com', allow));
  assert.ok(istErlaubt('www.dejure.org', allow));
  assert.ok(istErlaubt('DEJURE.ORG.', allow));
});

test('fremde Domains und Suffix-Tricks sind verboten', () => {
  assert.ok(!istErlaubt('example.org', allow));
  assert.ok(!istErlaubt('evildejure.org', allow));
  assert.ok(!istErlaubt('dejure.org.evil.com', allow));
  assert.ok(!istErlaubt('anthropic.com', allow));
  assert.ok(!istErlaubt('127.0.0.1', allow));
  assert.ok(!istErlaubt('', allow));
});

test('Recherche-Proxy "*" erlaubt jeden gültigen Hostnamen, aber keine IP-Literale', () => {
  const frei = parseAllow(' * ');
  assert.deepStrictEqual(frei, ['*']);
  assert.ok(istErlaubt('example.org', frei));
  assert.ok(istErlaubt('www.lto.de', frei));
  assert.ok(!istErlaubt('127.0.0.1', frei));
  assert.ok(!istErlaubt('x.org/?d=geheim', frei));
  assert.ok(!istErlaubt('', frei));
  // "*" nur als alleiniger Wert; in einer Liste ist es ungültig und wird verworfen.
  assert.deepStrictEqual(parseAllow('dejure.org,*'), ['dejure.org']);
});

test('Host-Bereinigung für Logs entfernt alles außer Hostnamen', () => {
  assert.strictEqual(bereinigeHost('Example.ORG'), 'example.org');
  assert.strictEqual(bereinigeHost('x.org/?d=geheim'), 'invalid');
  assert.strictEqual(bereinigeHost('a\nb.org'), 'invalid');
});

test('CONNECT-Ziel: nur host:port', () => {
  assert.deepStrictEqual(parseZiel('dejure.org:443'), { host: 'dejure.org', port: 443 });
  assert.strictEqual(parseZiel('dejure.org'), null);
  assert.strictEqual(parseZiel('http://dejure.org:443'), null);
});

test('private Adressen werden erkannt', () => {
  for (const ip of ['10.0.0.1', '127.0.0.1', '192.168.1.1', '172.16.0.1', '169.254.169.254', '::1', 'fd00::1']) {
    assert.ok(istPrivat(ip), ip);
  }
  assert.ok(!istPrivat('8.8.8.8'));
});

function connect(port, ziel) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method: 'CONNECT', path: ziel });
    req.on('connect', (res, socket) => { socket.destroy(); resolve(res.statusCode); });
    req.on('response', (res) => resolve(res.statusCode));
    req.on('error', reject);
    req.end();
  });
}

test('Server verweigert nicht gelistete Domain und Port ≠ 443 mit 403 und SEC-Logzeile', async () => {
  const zeilen = [];
  const server = erstelleServer(allow, 'api', (z) => zeilen.push(z));
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const { port } = server.address();
  try {
    assert.strictEqual(await connect(port, 'example.org:443'), 403);
    assert.strictEqual(await connect(port, 'dejure.org:80'), 403);
  } finally {
    server.close();
  }
  assert.ok(zeilen.includes('[SEC] event=legal.egress.denied role=api domain=example.org\n'));
  assert.ok(zeilen.includes('[SEC] event=legal.egress.denied role=api domain=dejure.org\n'));
  assert.ok(zeilen.every((z) => /^\[SEC\] event=legal\.egress\.denied role=api domain=[a-z0-9.-]+\n$/.test(z)));
});

test('erlaubte Domain, die auf eine private Adresse auflöst, wird mit 403 verweigert', async () => {
  const zeilen = [];
  const lookup = (host, _opts, cb) => cb(null, '127.0.0.1', 4);
  const server = erstelleServer(allow, 'recherche', (z) => zeilen.push(z), lookup);
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const { port } = server.address();
  try {
    assert.strictEqual(await connect(port, 'dejure.org:443'), 403);
  } finally {
    server.close();
  }
  assert.deepStrictEqual(zeilen, ['[SEC] event=legal.egress.denied role=recherche domain=dejure.org\n']);
});

test('offener Recherche-Proxy verweigert private Zieladressen und Port ≠ 443', async () => {
  const zeilen = [];
  const lookup = (host, _opts, cb) => cb(null, '192.168.1.10', 4);
  const server = erstelleServer(parseAllow('*'), 'recherche', (z) => zeilen.push(z), lookup);
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const { port } = server.address();
  try {
    assert.strictEqual(await connect(port, 'fritz.box.example.org:443'), 403);
    assert.strictEqual(await connect(port, 'example.org:80'), 403);
    assert.strictEqual(await connect(port, '10.0.0.1:443'), 403);
  } finally {
    server.close();
  }
  assert.strictEqual(zeilen.length, 3);
});

test('Verbindungsabbruch des Clients beendet den Proxy nicht', async () => {
  const net = require('node:net');
  const lookup = (host, _opts, cb) => setTimeout(() => cb(null, '127.0.0.1', 4), 20);
  const server = erstelleServer(allow, 'api', () => {}, lookup);
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const { port } = server.address();
  try {
    const s = net.connect(port, '127.0.0.1', () => {
      s.write('CONNECT dejure.org:443 HTTP/1.1\r\nHost: dejure.org:443\r\n\r\n');
      s.resetAndDestroy();
    });
    await new Promise((r) => setTimeout(r, 80));
    assert.strictEqual(await connect(port, 'example.org:443'), 403);
  } finally {
    server.close();
  }
});
