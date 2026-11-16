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
