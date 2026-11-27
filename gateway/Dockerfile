# Builder is separate from runtime. npm and build tools never enter production image.
FROM docker.io/node:22-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS dependencies
WORKDIR /app
COPY gateway/package.json gateway/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-fund && npm cache clean --force \
    && mkdir -p /app/data

# Minimal non-root runtime: no shell, no npm, no package manager.
# Temporary candidate tag for security scan; pin immutable digest after verification.
FROM gcr.io/distroless/nodejs22-debian13:nonroot
WORKDIR /app
ENV NODE_ENV=production PORT=3000 WA_DATA_DIR=/data
COPY --from=dependencies --chown=65532:65532 /app/node_modules ./node_modules
COPY --from=dependencies --chown=65532:65532 /app/data /data
COPY --chown=65532:65532 gateway/src ./src
USER 65532:65532
EXPOSE 3000
CMD ["src/server.mjs"]
