FROM python:3.11-slim

WORKDIR /app

# System deps for Ray
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements-k8s.txt .
RUN pip install --no-cache-dir -r requirements-k8s.txt

# Install PyTorch CPU-only
RUN pip install --no-cache-dir \
    torch==2.2.2 \
    --index-url https://download.pytorch.org/whl/cpu

COPY ray_app.py .

# Ray Serve HTTP
EXPOSE 8000
# Ray dashboard
EXPOSE 8265
# Ray internal metrics (Prometheus)
EXPOSE 9999

ENV TRANSFORMERS_CACHE=/app/model-cache
ENV HF_HOME=/app/model-cache
# Disable Ray usage stats prompt
ENV RAY_USAGE_STATS_ENABLED=0

CMD ["python", "ray_app.py"]
