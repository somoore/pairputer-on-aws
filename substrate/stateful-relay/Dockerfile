# Digest-pinned (supply chain): node:20-bookworm-slim arm64.
# To bump: docker buildx imagetools inspect node:20-bookworm-slim (arm64 digest).
FROM node:20-bookworm-slim@sha256:10fc5f5f33cba34a4befa58fcf95f724e67707fab7c32fb8cd3fcf90ebcc20df

WORKDIR /app
ENV NODE_ENV=production

COPY package.json package-lock.json* ./
RUN npm ci --omit=dev

COPY index.mjs ./

EXPOSE 8080
CMD ["node", "index.mjs"]
