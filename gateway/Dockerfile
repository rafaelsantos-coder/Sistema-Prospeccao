# Only our source code is used. Baileys is an audited, separately versioned library.
FROM docker.io/node:22-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee
WORKDIR /app
ENV NODE_ENV=production PORT=3000 WA_DATA_DIR=/data
COPY gateway/package*.json ./
RUN npm install --omit=dev --ignore-scripts --no-fund && npm cache clean --force
COPY gateway/src ./src
RUN mkdir /data && chown -R node:node /app /data
USER node
EXPOSE 3000
CMD ["node", "src/server.mjs"]
