# CLIPFORGE: one container runs the web app and the render jobs (CPU).
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV DEBIAN_FRONTEND=noninteractive
ENV CLIPFORGE_DATA_DIR=/data
ENV OMP_NUM_THREADS=4

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg libgl1 libglib2.0-0 libegl1 libgles2 fonts-noto-color-emoji curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime for YouTube (deno is the one it recommends)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- --yes \
    && deno --version

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
EXPOSE 8000
# PORT is set by hosts like Railway; defaults to 8000
CMD ["sh", "-c", "python -m clipforge serve --host 0.0.0.0 --port ${PORT:-8000}"]
