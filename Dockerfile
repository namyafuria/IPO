# ---- STAGE 1: BUILD FRONTEND (React) ----
# Switched FROM node:18-alpine to node:18-slim to fix native module compilation failures
FROM node:18-slim AS frontend-builder
WORKDIR /app
# Copy the single root package.json
COPY package*.json ./
RUN npm install
# Copy frontend source code
COPY src ./src
# Copy config files needed for React build (ignores errors if they don't exist)
COPY tsconfig.json vite.config.js public ./ 2>/dev/null || true
# Build the React app (should output to /app/dist)
RUN npm run build

# ---- STAGE 2: BUILD BACKEND (Node/Express) ----
FROM node:18-slim AS backend-builder
WORKDIR /app
# Copy the same root package.json for backend dependencies
COPY package*.json ./
RUN npm install
# Copy the entire backend source folder
COPY backend ./backend
# Compile the backend TypeScript
# It tries using backend/tsconfig.json first, then falls back to root tsconfig.json
RUN npx tsc -p backend/tsconfig.json || npx tsc -p tsconfig.json --outDir ./backend/dist

# ---- STAGE 3: FINAL RUNTIME IMAGE ----
FROM node:18-slim
WORKDIR /app

# Copy backend production dependencies
COPY --from=backend-builder /app/node_modules ./node_modules
# Copy compiled backend code
COPY --from=backend-builder /app/backend/dist ./backend/dist
# Copy compiled frontend assets (into /app/dist)
COPY --from=frontend-builder /app/dist ./dist

EXPOSE 10000
CMD ["node", "backend/dist/server.js"]
