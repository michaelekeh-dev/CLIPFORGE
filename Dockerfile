# CLIPFORGE: one container runs the web app and the render jobs (CPU).
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DEBIAN_FRONTEND=noninteractive \
    CLIPFORGE_DATA_DIR=/data OMP_NUM_THREADS=4

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg libgl1 libglib2.0-0 libegl1 libgles2 fonts-noto-color-emoji curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fs http://localhost:${PORT:-8000}/health || exit 1
# PORT is set by hosts like Railway; defaults to 8000
CMD ["sh", "-c", "python -m clipforge serve --host 0.0.0.0 --port ${PORT:-8000}"]
