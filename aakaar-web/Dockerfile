# syntax=docker/dockerfile:1.7
# ---- Aakaar frontend image ---------------------------------------------------
# Stage 1: build the Vite bundle. Stage 2: serve static assets from nginx.

# ----- builder ----------------------------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /app

# VITE_API_BASE is baked into the bundle at build time. Leave empty so the
# bundle calls /api and let nginx (next stage) reverse-proxy to the backend.
ARG VITE_API_BASE=/api
ENV VITE_API_BASE=$VITE_API_BASE

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY tsconfig*.json vite.config.ts postcss.config.js tailwind.config.ts index.html ./
COPY public ./public
COPY src ./src
RUN npm run build

# ----- runtime ----------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

# AAKAAR_BACKEND_URL is read by the entrypoint to template nginx.conf.
ENV AAKAAR_BACKEND_URL=http://aakaar-api:8000

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/templates/default.conf.template

# nginx:alpine already ships docker-entrypoint.sh that envsubst's any file
# under /etc/nginx/templates into /etc/nginx/conf.d/.

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/healthz >/dev/null || exit 1
