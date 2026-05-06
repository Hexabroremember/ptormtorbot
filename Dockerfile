# syntax=docker/dockerfile:1
# Frontend (Vite → dist/). Vite + tooling live in dependencies so `npm ci` always installs them.
FROM node:22-alpine AS frontend
WORKDIR /build

COPY package.json package-lock.json vite.config.js postcss.config.js tailwind.config.cjs index.html ./
COPY src ./src

ARG VITE_API_BASE_URL=
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

RUN npm ci --no-audit --no-fund && npm run build

# API + serve built SPA from ./dist
FROM python:3.12-slim-bookworm
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY assets ./assets
COPY fonts ./fonts
COPY static ./static
COPY --from=frontend /build/dist ./dist

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/docker-entrypoint.sh"]
