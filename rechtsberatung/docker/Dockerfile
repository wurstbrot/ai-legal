# Image "legal:1" für die Rollen-Container und den Egress-Proxy von `legal` (E-8, TM-AC-25).
# docker.md: non-root, Basis per Digest, HEALTHCHECK, keine Secrets in Layern.
#
# Basis: Docker Official Image node:22.23.4-bookworm-slim, per Digest gepinnt
# (supply-chain.md; Prüfung 2026-11-16 siehe README). Ein Tag allein ist beweglich.
FROM node:22.23.4-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

ENV NODE_ENV=production \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

WORKDIR /opt/legal

# CLIs exakt gepinnt aus package-lock.json (Version + Integrity). `npm ci` bricht ab, wenn
# Lockfile und package.json nicht übereinstimmen. Kein `curl | sh`, kein ssh, kein sudo.
# Hinweis: @anthropic-ai/claude-code benötigt sein postinstall (install.cjs) zum Verlinken
# des plattformspezifischen Binärpakets; daher kein --ignore-scripts.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev \
    && npm cache clean --force \
    && rm -rf /root/.npm /tmp/*

COPY egress-proxy.js /opt/legal/egress-proxy.js

# python3 für Codex (Hilfsskripte beim Lesen der Akte; ohne python3 weicht Codex auf node aus).
# Debian-Paket aus dem Bookworm-Archiv der Basis (apt prüft die Archivsignatur), Versionen exakt
# gepinnt; ein Debian-Sicherheitsupdate lässt den Build scheitern -> Pin bewusst anheben.
# Keine Recommends, Paketlisten im selben Layer entfernt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3=3.11.2-1+b1 \
       python3.11=3.11.2-6+deb12u9 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb

# System-CA-Bundle: Das slim-Image hat kein ca-certificates. Node bringt eigene Wurzeln mit,
# die Codex-CLI (Rust-Binary) liest dagegen das System-Bundle; ohne es scheitert jede
# TLS-Verbindung ("error sending request", Fehlerbild 2026-11-16). Statt eines zusätzlichen
# apt-Pakets wird Nodes eingebauter Mozilla-Wurzelbestand exportiert: keine neue Abhängigkeit,
# kein Netzabruf im Build. Root-eigen, nur lesbar (Laufzeitnutzer kann keine CA einschleusen).
RUN mkdir -p /etc/ssl/certs \
    && node -e "process.stdout.write(require('tls').rootCertificates.join('\n') + '\n')" \
       > /etc/ssl/certs/ca-certificates.crt \
    && grep -q 'BEGIN CERTIFICATE' /etc/ssl/certs/ca-certificates.crt \
    && chmod 0444 /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# setuid/setgid-Bits entfernen (docker.md, TM-AC-25); ssh-Client und sudo dürfen nicht vorhanden sein.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
    && ! command -v ssh \
    && ! command -v sudo \
    && chmod -R a-w /opt/legal

ENV PATH=/opt/legal/node_modules/.bin:$PATH

# Nicht-root (node, UID 1000, im Basis-Image vorhanden).
USER node
WORKDIR /out

HEALTHCHECK --interval=60s --timeout=10s --retries=2 CMD ["node", "-e", "process.exit(0)"]

# Kein ENTRYPOINT: Der Conductor wählt `claude …`, `codex …` bzw. den Proxy.
CMD ["node", "--version"]
