# ------------------------------------------------------------
# Works on Railway, Render, Fly.io, Koyeb, a plain VPS, etc.
#
# ffmpeg is NOT downloaded at runtime. It comes from the pip wheel
# (imageio-ffmpeg) during the build step, and apt ffmpeg is installed
# too when the network allows it. Either way the bot finds it by
# itself - no FFMPEG_LOCATION, no manual download to get killed.
# ------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Best effort: system ffmpeg (brings ffprobe). Never fails the build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && (apt-get install -y --no-install-recommends ffmpeg || true) \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Downloads are temporary working files.
ENV DOWNLOAD_DIR=/tmp/downloads

# Resolve ffmpeg at BUILD time and cache it in the image, so the first
# run never has to fetch anything. Informational only - never fails.
RUN python ffmpeg_setup.py || true

CMD ["python", "main.py"]
