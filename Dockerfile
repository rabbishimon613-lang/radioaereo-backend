# Rádio Aéreo backend — slim image for small free hosts (Render free: 512 MB RAM).
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 HF_HUB_DISABLE_PROGRESS_BARS=1 HOST=0.0.0.0 PORT=10000 GRU_BOARD=0 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install uv && uv pip install --system -e .
COPY config ./config
COPY web/sbgr.json web/app.html ./web/
RUN mkdir -p data
EXPOSE 10000
CMD ["atc-br"]
