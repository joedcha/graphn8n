FROM node:20-alpine

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund || true
COPY src ./src

ENV STATE_FILE=/data/state.json
VOLUME ["/data"]

CMD ["node", "src/index.js"]
