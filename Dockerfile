# syntax=docker/dockerfile:1.6
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# System deps:
#   - poppler-utils      → pdf2image (PDF page rendering)
#   - libjpeg / zlib     → Pillow
#   - libgl1 / glib      → opencv-python-headless
#   - build-essential + libpq-dev → psycopg2-binary wheel is available
#     but keep libpq-dev around in case a version rebuild is needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        poppler-utils \
        libjpeg-dev \
        zlib1g-dev \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && pip install gunicorn

# Copy the rest of the backend
COPY . .

# Non-root
RUN adduser --disabled-password --gecos '' app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Default: production-ish gunicorn. docker-compose overrides for dev.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
