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

# Minimal transparent PNG so builds succeed when the repo has no custom watermark.png.
# Replace at runtime (volume mount) or add COPY watermark.png before this RUN to override in image layers.
RUN python -c "import base64, pathlib; pathlib.Path('watermark.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='))"

COPY app ./app
COPY assets ./assets
COPY fonts ./fonts
COPY static ./static
COPY --from=frontend /build/dist ./dist

ENV PYTHONUNBUFFERED=1

# PORT is set by Railway/etc.; read in Python (avoids shell / $PORT literal issues).
CMD ["python", "-m", "app"]
