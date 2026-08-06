# ---- STAGE 1: BUILD FRONTEND & BACKEND ----
# We use the FULL node:18 image here to ensure we have g++, make, and python3 
# to successfully compile native modules like better-sqlite3.
FROM node:18 AS builder
WORKDIR /app

# Copy the single root package.json and install ALL dependencies (dev + prod) once.
# This avoids running 'npm install' twice, preventing Out of Memory (OOM) errors on Render's free tier.
COPY package*.json ./
RUN npm install

# --- Build Frontend ---
COPY src ./src
# Copy config files needed for React build (ignores errors if they don't exist)
COPY tsconfig.json vite.config.js public ./ 2>/dev/null || true
RUN npm run build

# --- Build Backend ---
COPY backend ./backend
# Compile the backend TypeScript
RUN npx tsc -p backend/tsconfig.json || npx tsc -p tsconfig.json --outDir ./backend/dist


# ---- STAGE 2: FINAL RUNTIME IMAGE ----
# We use node:18-slim here because the runtime does NOT need compilers.
FROM node:18-slim
WORKDIR /app

# Copy production dependencies from the builder
COPY --from=builder /app/node_modules ./node_modules
# Copy compiled backend code
COPY --from=builder /app/backend/dist ./backend/dist
# Copy compiled frontend assets (into /app/dist)
COPY --from=builder /app/dist ./dist

EXPOSE 10000
CMD ["node", "backend/dist/server.js"]
