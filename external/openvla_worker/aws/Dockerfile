# Worker VLA Go2/D1 — EC2 g5.xlarge (A10G 24GB) o g5.2xlarge.
#
# Build dalla root del repo:
#   docker build -f external/openvla_worker/aws/Dockerfile -t go2-vla-worker:aws .
#
# Run:
#   docker run --gpus all --rm -p 8765:8765 --env-file external/openvla_worker/aws/.env go2-vla-worker:aws

FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3-pip \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

WORKDIR /app

COPY external/openvla_worker/requirements.txt external/openvla_worker/requirements-openvla.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu124 \
    && pip install --no-cache-dir -r requirements.txt -r requirements-openvla.txt

COPY external/openvla_worker/app.py external/openvla_worker/openvla_runtime.py external/openvla_worker/planner_runtime.py external/openvla_worker/graspgen_runtime.py external/openvla_worker/rgbd_pointcloud.py ./
COPY scripts/box_grasp_planner.py scripts/box_object_detector.py scripts/arm_kinematics_d1_template.py /app/scripts/
# Vendored lightweight GraspGen ZMQ client (no torch) so the worker can call the GraspGen server.
COPY external/openvla_worker/grasp_gen/ /app/grasp_gen/
COPY external/openvla_worker/aws/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV WORKER_BIND_HOST=0.0.0.0
ENV WORKER_PORT=8765
ENV GO2_REPO_ROOT=/app
ENV GO2_GRASP_WORKER_BACKEND=auto
ENV GO2_GRASP_GEN_ZMQ=tcp://127.0.0.1:5556
ENV OPENVLA_USE_HF=0
ENV OPENVLA_RUNTIME_STUB=1
ENV OPENVLA_UNNORM_KEY=bridge_orig
ENV OPENVLA_DEFAULT_INSTRUCTION="pick up the object"
ENV HF_HOME=/data/huggingface
ENV TRANSFORMERS_CACHE=/data/huggingface

RUN mkdir -p /data/huggingface

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=5)"

ENTRYPOINT ["/entrypoint.sh"]
