# syntax=docker/dockerfile:1
# Frontend (Vite → dist/) — amd64 required by Render
FROM --platform=linux/amd64 node:22-alpine AS frontend
WORKDIR /build
COPY package.json package-lock.json vite.config.js postcss.config.js tailwind.config.cjs index.html ./
COPY src ./src
# Same host as API on Render → relative /generate-pdf and /redeem-payment-code
ARG VITE_API_BASE_URL=
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# API + serve built SPA from ./dist
FROM --platform=linux/amd64 python:3.12-slim-bookworm
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

ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
