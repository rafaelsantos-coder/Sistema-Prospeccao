# One-shot diagnostic image; cannot originate a telephone call or use a CRM.
FROM python:3.12.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    LIA_VOZ_LAB_REALTIME_ENABLED=false
WORKDIR /app
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 libgcc-s1 libasound2 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-lia-voz.txt ./
RUN pip install --no-cache-dir pjsua2-python==2.15.1.post1 -r requirements-lia-voz.txt
COPY app/services/lia_voz_realtime.py app/services/lia_voz_media.py ./app/services/
COPY scripts/lia_voz_native_bench.py scripts/lia_voz_lab.py ./scripts/
RUN addgroup --system lialab && adduser --system --ingroup lialab lialab
USER lialab
CMD ["python", "-m", "scripts.lia_voz_lab"]
