# ---- STAGE 1: BUILD FRONTEND ----
FROM node:18-alpine AS frontend-builder
WORKDIR /app/frontend
# Copy frontend package files
COPY package*.json ./
RUN npm install
# Copy frontend source code (assuming it's in /src)
COPY . .
# Build the React frontend
RUN npm run build

# ---- STAGE 2: BUILD BACKEND ----
FROM node:18-alpine AS backend-builder
WORKDIR /app/backend
# Copy backend package files
COPY backend/package*.json ./
RUN npm install
# Copy backend source code
COPY backend .
# Build the backend TypeScript
RUN npm run build

# ---- STAGE 3: FINAL RUNNING IMAGE ----
FROM node:18-alpine
WORKDIR /app

# Install only production dependencies for backend to keep image small
COPY --from=backend-builder /app/backend/package*.json ./backend/
RUN cd backend && npm install --only=production

# Copy the compiled backend code into /app/backend/dist
COPY --from=backend-builder /app/backend/dist ./backend/dist

# Copy the built React frontend into /app/dist
# (This is IMPORTANT: the backend looks for frontend files at ../dist)
COPY --from=frontend-builder /app/dist ./dist

# Expose the port
EXPOSE 10000

# Start the server
CMD ["node", "backend/dist/server.js"]
