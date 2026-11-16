'use strict';
// Egress-Proxy für `legal` (E-1, TM-AC-7). Nur Node-Standardbibliothek.
// Erlaubt ausschließlich CONNECT auf Port 443 zu exakten Domains (oder deren Subdomains)
// aus LEGAL_ALLOW. Alles andere: 403 und eine Logzeile
// "[SEC] event=legal.egress.denied role=<LEGAL_PROXY_ROLE> domain=<host>" (ohne Pfad/Query).

const http = require('node:http');
const net = require('node:net');
const dns = require('node:dns');

const PORT = 3128;
const HOST_RE = /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;
const ROLLEN = new Set(['api', 'recherche']);

function parseAllow(text) {
  return String(text || '')
    .split(',')
    .map((d) => d.trim().toLowerCase())
    .filter((d) => HOST_RE.test(d));
}

function bereinigeHost(host) {
  const h = String(host || '').toLowerCase().replace(/\.$/, '');
  return HOST_RE.test(h) ? h : 'invalid';
}

function istErlaubt(host, allow) {
  const h = bereinigeHost(host);
  if (h === 'invalid') return false;
  return allow.some((d) => h === d || h.endsWith('.' + d));
}

// CONNECT-Ziel "host:port" zerlegen; nur Hostnamen (keine IP-Literale), nur Port 443.
function parseZiel(ziel) {
  const m = /^([^:\s/]{1,253}):(\d{1,5})$/.exec(String(ziel || ''));
  if (!m) return null;
  return { host: m[1].toLowerCase(), port: Number(m[2]) };
}

// Private, Loopback- und Link-Local-Adressen nach der Auflösung sperren (DNS-Rebinding).
function istPrivat(ip) {
  if (net.isIPv4(ip)) {
    const [a, b] = ip.split('.').map(Number);
    return a === 10 || a === 127 || a === 0 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127) || a >= 224;
  }
  const v = ip.toLowerCase();
  return v === '::1' || v === '::' || v.startsWith('fc') || v.startsWith('fd') || v.startsWith('fe80') ||
    v.startsWith('::ffff:');
}

function standardLog(zeile) {
  process.stdout.write(zeile);
}

function erstelleServer(allow, rolle, log = standardLog, lookup = dns.lookup) {
  const logDenied = (r, host) => log(`[SEC] event=legal.egress.denied role=${r} domain=${bereinigeHost(host)}\n`);
  const server = http.createServer((req, res) => {
    // Kein Klartext-HTTP-Forwarding: nur CONNECT ist erlaubt.
    let host = 'invalid';
    try {
      host = new URL(req.url).hostname;
    } catch (_e) {
      host = String(req.headers.host || '').split(':')[0];
    }
    logDenied(rolle, host);
    res.writeHead(403, { 'Content-Type': 'text/plain' });
    res.end('Forbidden\n');
  });

  server.on('connect', (req, clientSocket, head) => {
    // Ein Verbindungsabbruch (z. B. ECONNRESET) während 403 oder DNS darf den Proxy nicht beenden.
    clientSocket.on('error', () => clientSocket.destroy());
    const ziel = parseZiel(req.url);
    const verweigern = (host) => {
      logDenied(rolle, host);
      clientSocket.end('HTTP/1.1 403 Forbidden\r\n\r\n');
    };
    if (!ziel || ziel.port !== 443 || !istErlaubt(ziel.host, allow)) {
      verweigern(ziel ? ziel.host : 'invalid');
      return;
    }
    lookup(ziel.host, { all: false }, (err, address) => {
      if (err || !address || istPrivat(address)) {
        verweigern(ziel.host);
        return;
      }
      const upstream = net.connect({ host: address, port: 443 }, () => {
        clientSocket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
        if (head && head.length) upstream.write(head);
        upstream.pipe(clientSocket);
        clientSocket.pipe(upstream);
      });
      upstream.setTimeout(600000, () => upstream.destroy());
      clientSocket.setTimeout(600000, () => clientSocket.destroy());
      upstream.on('error', () => clientSocket.destroy());
      clientSocket.on('error', () => upstream.destroy());
      clientSocket.on('close', () => upstream.destroy());
    });
  });
  server.on('clientError', (_err, socket) => socket.destroy());
  return server;
}

module.exports = { parseAllow, bereinigeHost, istErlaubt, parseZiel, istPrivat, erstelleServer };

if (require.main === module) {
  const rolle = ROLLEN.has(process.env.LEGAL_PROXY_ROLE) ? process.env.LEGAL_PROXY_ROLE : 'api';
  const allow = parseAllow(process.env.LEGAL_ALLOW);
  erstelleServer(allow, rolle).listen(PORT, '0.0.0.0');
}
