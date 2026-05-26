# Worker VLA Go2/D1 — EC2 g5.xlarge (A10G 24GB) o g5.2xlarge.
#
# Build (dalla cartella external/openvla_worker):
#   docker build -f aws/Dockerfile -t go2-vla-worker:aws .
#
# Run:
#   docker run --gpus all --rm -p 8765:8765 --env-file aws/.env go2-vla-worker:aws

FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3-pip \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

WORKDIR /app

COPY requirements.txt requirements-openvla.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu124 \
    && pip install --no-cache-dir -r requirements.txt -r requirements-openvla.txt

COPY app.py openvla_runtime.py planner_runtime.py ./
COPY aws/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV WORKER_BIND_HOST=0.0.0.0
ENV WORKER_PORT=8765
ENV GO2_GRASP_WORKER_BACKEND=openvla
ENV OPENVLA_USE_HF=1
ENV OPENVLA_UNNORM_KEY=bridge_orig
ENV OPENVLA_DEFAULT_INSTRUCTION=pick up the object
ENV HF_HOME=/data/huggingface
ENV TRANSFORMERS_CACHE=/data/huggingface

RUN mkdir -p /data/huggingface

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=5)"

ENTRYPOINT ["/entrypoint.sh"]
