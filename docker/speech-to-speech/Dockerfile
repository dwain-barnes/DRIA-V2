FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cu130 \
    HF_HOME=/models/huggingface \
    HUGGINGFACE_HUB_CACHE=/models/huggingface/hub \
    TORCH_HOME=/models/torch \
    NLTK_DATA=/models/nltk

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash \
      build-essential \
      ca-certificates \
      curl \
      ffmpeg \
      git \
      libgomp1 \
      libportaudio2 \
      libsndfile1 \
      portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu130 \
      torch==2.11.0+cu130 \
      torchaudio==2.11.0+cu130 \
    && python -m pip install --no-cache-dir speech-to-speech==0.2.6

RUN apt-get update \
    && apt-get install -y --no-install-recommends sox \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY docker/speech-to-speech/start-realtime.sh /usr/local/bin/start-dria-s2s
RUN chmod +x /usr/local/bin/start-dria-s2s \
    && mkdir -p /models/huggingface /models/torch /models/nltk

EXPOSE 8765

CMD ["start-dria-s2s"]
