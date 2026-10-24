# Elefant diagram validator: Mermaid's parser in plain Node, for the MCP
# diagram tools. No browser: parsing needs no layout. See server.mjs.
FROM node:22.23.3-trixie-slim

# Fixed, non-root UID/GID 10001, like the other services. It owns no volume.
RUN groupadd --system --gid 10001 elefant \
 && useradd --system --uid 10001 --gid 10001 --home-dir /home/elefant --create-home --shell /usr/sbin/nologin elefant

WORKDIR /app
ENV NODE_ENV=production

COPY package.json package-lock.json ./
COPY scripts/ scripts/

# One layer, so what is deleted is really gone from the image. The license
# scan runs against what was just installed, and fails the build on anything
# outside the allow list; its output is this image's notices. Then drop what
# Node never loads: source maps, and mermaid's browser bundles (`import
# 'mermaid'` resolves to dist/mermaid.core.mjs and its own chunks). License
# files stay.
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force \
 && node scripts/license-scan.mjs --write THIRD_PARTY_NOTICES.md \
 && find node_modules -name '*.map' -delete \
 && rm -rf node_modules/mermaid/dist/mermaid.js node_modules/mermaid/dist/mermaid.min.js \
           node_modules/mermaid/dist/mermaid.esm.mjs node_modules/mermaid/dist/mermaid.esm.min.mjs \
           node_modules/mermaid/dist/chunks/mermaid.esm node_modules/mermaid/dist/chunks/mermaid.esm.min

COPY mermaid-errors.mjs parser.mjs parse-worker.mjs lib.mjs server.mjs ./

USER 10001:10001

# Parse for real, as the runtime user, before the image exists.
RUN node scripts/selftest.mjs

EXPOSE 8005

CMD ["node", "server.mjs"]
