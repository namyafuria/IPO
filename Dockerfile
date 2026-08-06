# --- Build stage -------------------------------------------------------
FROM node:22-bookworm-slim AS build
WORKDIR /app

# System deps needed to compile better-sqlite3's native bindings
RUN apt-get update && apt-get install -y python3 make g++ && rm -rf /var/lib/apt/lists/*

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# --- Runtime stage -------------------------------------------------------
FROM node:22-bookworm-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV DB_DIR=/data

# Same native build deps are needed here since better-sqlite3 ships as a
# native module and `npm ci --omit=dev` will rebuild/relink it for this image.
RUN apt-get update && apt-get install -y python3 make g++ && rm -rf /var/lib/apt/lists/*

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY --from=build /app/dist ./dist

# Persistent volume for the SQLite database -- mount this at your host's
# persistent disk/volume, or the DB resets on every deploy/restart.
VOLUME /data

EXPOSE 3000
CMD ["node", "dist/server.cjs"]
