FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/cache/huggingface \
    HUGGINGFACE_HUB_CACHE=/cache/huggingface/hub

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir huggingface_hub hf_xet

WORKDIR /app

COPY docker/model-downloader/download-models.py /app/download-models.py

CMD ["python", "/app/download-models.py"]
