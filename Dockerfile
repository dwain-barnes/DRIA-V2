FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:/app/node_modules/.bin:${PATH}" \
    FASTRTC_HOST=0.0.0.0 \
    FASTRTC_PORT=7860 \
    FASTRTC_MODE=ui

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      ffmpeg \
      git \
      libgomp1 \
      libsndfile1 \
      python3 \
      python3-pip \
      python3-venv \
    && python3 -m venv /opt/venv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt package.json package-lock.json ./
RUN pip install --no-cache-dir -r requirements.txt \
    && npm ci --omit=dev
RUN python - <<'PY'
from pathlib import Path

path = Path("/opt/venv/lib/python3.11/site-packages/fastrtc/webrtc_connection_mixin.py")
source = path.read_text()
source = source.replace(
    'if len(parts) >= 10 and parts[0].startswith("candidate:"):',
    'if len(parts) >= 8 and parts[0].startswith("candidate:") and "typ" in parts:',
)
source = source.replace("protocol = parts[2]", "protocol = parts[2].lower()")
path.write_text(source)
PY

COPY . .
ARG FIRECRAWL_WEB_AGENT_REF=f023adf1cd1f731e27fdc844af62996f6c2a41c4
ENV FIRECRAWL_WEB_AGENT_REF=${FIRECRAWL_WEB_AGENT_REF}
RUN node dria-stack/bootstrap-agent-core.mjs
RUN mkdir -p /app/data /app/research_results

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=3).read()" || exit 1

CMD ["python", "app.py"]
