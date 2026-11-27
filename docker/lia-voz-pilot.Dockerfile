# Separate native PJSUA2 runtime: upstream wheel supports CPython 3.12, not 3.14.
# Do not use this image for web/WhatsApp workers.
FROM python:3.12.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VOICE_ENGINE_WORKER_ENABLED=false \
    VOICE_CAMPAIGN_OUTBOUND_ENABLED=false \
    VOICE_TRANSFER_TESTS_ENABLED=false
WORKDIR /app
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 libgcc-s1 libasound2 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-voice.txt requirements-lia-voz.txt ./
RUN pip install --no-cache-dir -r requirements-voice.txt -r requirements-lia-voz.txt \
    && python -c "import pjsua2; print('PJSUA2 native import OK')"
COPY app/ ./app/
RUN addgroup --system sulnetvoice && adduser --system --ingroup sulnetvoice sulnetvoice \
    && mkdir -p /app/data && chown -R sulnetvoice:sulnetvoice /app
USER sulnetvoice
CMD ["python", "-m", "app.workers.voice_engine_worker"]
