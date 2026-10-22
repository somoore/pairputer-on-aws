# Digest-pinned (supply chain): node:24-bookworm-slim (24.22.1, Oct-2026 security release).
# To bump: docker buildx imagetools inspect node:24-bookworm-slim.
FROM node:24-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205

WORKDIR /app
ENV NODE_ENV=production

COPY package.json package-lock.json* ./
RUN npm ci --omit=dev

COPY index.mjs ./

EXPOSE 8080
CMD ["node", "index.mjs"]
