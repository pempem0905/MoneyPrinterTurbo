# Video Engine cloud image — keep upstream app code, use current Debian base.
FROM python:3.11-slim-bookworm

WORKDIR /MoneyPrinterTurbo
ENV PYTHONPATH="/MoneyPrinterTurbo"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# FFmpeg is required by the render pipeline. Bookworm avoids the expired
# bullseye-security metadata that can break fresh cloud builds.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --retries 3 --timeout 90 -r requirements.txt

COPY . .
RUN mkdir -p /MoneyPrinterTurbo/storage && chmod -R 755 /MoneyPrinterTurbo

# API-only engine defaults. Railway overrides the command explicitly too.
EXPOSE 8080
CMD ["python", "engine.py"]
