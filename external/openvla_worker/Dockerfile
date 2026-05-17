# Build sul PC con GPU (RTX 5070). Esempio:
#   docker build -t go2-openvla-worker external/openvla_worker
#   docker run --gpus all -p 8765:8765 -e WORKER_BIND_HOST=0.0.0.0 go2-openvla-worker
#
# Immagine leggera: solo stub Flask. Per CUDA+PyTorch sostituire FROM con
# nvidia/cuda:12.x.x-runtime-ubuntu22.04 e aggiungere pip torch (vedi README).

FROM python:3.11-slim-bookworm

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV WORKER_BIND_HOST=0.0.0.0
ENV WORKER_PORT=8765
EXPOSE 8765

CMD ["python", "-u", "app.py"]
