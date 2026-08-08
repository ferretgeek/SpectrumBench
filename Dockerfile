FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    SPECTRUMBENCH_DATA_DIR=/data

WORKDIR /app

RUN groupadd --system --gid 10001 spectrumbench \
    && useradd --system --uid 10001 --gid spectrumbench --home-dir /nonexistent --shell /usr/sbin/nologin spectrumbench \
    && install -d -m 0750 -o spectrumbench -g spectrumbench /data

COPY requirements.txt ./
RUN python -m pip install --requirement requirements.txt

COPY --chown=spectrumbench:spectrumbench stress_tool ./stress_tool
COPY --chown=spectrumbench:spectrumbench token_stress_test.py pricing_table.json ./

USER spectrumbench:spectrumbench
EXPOSE 18976

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:18976/healthz',timeout=3)); raise SystemExit(0 if d.get('app')=='SpectrumBench' else 1)"]

CMD ["python", "token_stress_test.py", "--host", "0.0.0.0", "--port", "18976", "--no-browser"]
