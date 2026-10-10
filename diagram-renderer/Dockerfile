# Elefant diagram renderer: Mermaid in headless Chromium, for the MCP diagram
# tools. OPTIONAL: a deployment can leave this image out, and the MCP server
# still stores and patches diagrams (see server.mjs for what changes).
FROM node:22.22.0-trixie-slim

# Chromium from Debian rather than puppeteer's own download: puppeteer ships no
# linux/arm64 build, and the distro package carries its own security updates
# and its own notices (/usr/share/doc/chromium/copyright). Fonts so text in a
# diagram has metrics to lay out against. Inter is the first face in the
# wiki's own font stack, so the PNG and the wiki draw labels alike (card
# #418). All three are free fonts: Inter and Liberation under OFL-1.1, DejaVu
# under the Bitstream Vera licence, notices in /usr/share/doc/fonts-*/copyright.
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-dejavu-core fonts-liberation fonts-inter \
 && rm -rf /var/lib/apt/lists/*

# Fixed, non-root UID/GID 10001, like the Python services. It owns no volume.
RUN groupadd --system --gid 10001 elefant \
 && useradd --system --uid 10001 --gid 10001 --home-dir /home/elefant --create-home --shell /usr/sbin/nologin elefant

WORKDIR /app
ENV NODE_ENV=production \
    PUPPETEER_SKIP_DOWNLOAD=1 \
    CHROMIUM_PATH=/usr/bin/chromium

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

COPY mermaid-errors.mjs lib.mjs server.mjs ./
COPY scripts/ scripts/

# The license scan runs against what was just installed, and fails the build
# on anything outside the allow list. Its output is this image's notices.
RUN node scripts/license-scan.mjs --write THIRD_PARTY_NOTICES.md

USER 10001:10001

# Render for real, as the runtime user, before the image exists: a Chromium
# that puppeteer-core cannot drive fails here, not on a server.
RUN node scripts/selftest.mjs

EXPOSE 8004

CMD ["node", "server.mjs"]
