# Sulnet WA: Node dependencies are built separately from a minimal runtime.
FROM docker.io/node:22-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS dependencies
WORKDIR /app
COPY gateway/package.json gateway/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-fund \
    && mkdir -p /app/data /node-libs \
    && ldd /usr/local/bin/node | awk '/libstdc\+\+|libgcc_s|libatomic/ {print $3}' \
       | while read f; do cp -L "$f" /node-libs/; done

# The "nossl" base avoids libssl3t64's unpatched CVEs present in the full
# Debian 13 distroless image. Node has its own bundled crypto implementation.
# Digest will be frozen after the security scan and runtime smoke test.
FROM gcr.io/distroless/base-nossl-debian13:nonroot
WORKDIR /app
ENV NODE_ENV=production PORT=3000 WA_DATA_DIR=/data LD_LIBRARY_PATH=/node-libs
COPY --from=dependencies /usr/local/bin/node /usr/local/bin/node
COPY --from=dependencies --chown=65532:65532 /node-libs /node-libs
COPY --from=dependencies --chown=65532:65532 /app/node_modules ./node_modules
COPY --from=dependencies --chown=65532:65532 /app/data /data
COPY --chown=65532:65532 gateway/src ./src
USER 65532:65532
EXPOSE 3000
ENTRYPOINT ["/usr/local/bin/node"]
CMD ["src/server.mjs"]
