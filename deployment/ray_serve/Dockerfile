FROM python:3.11-slim

WORKDIR /app

# System deps for Ray
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install all Python dependencies in one layer:
#   1. PyTorch CPU-only（先装，体积最大，优先缓存该层）
#   2. 其余依赖
# 合并为单层可避免 BuildKit 在中间层保留 ~700MB 的 torch 临时文件
COPY requirements-k8s.txt .
RUN pip install --no-cache-dir \
        torch==2.2.2 \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements-k8s.txt

COPY ray_app.py .

# Ray Serve HTTP
EXPOSE 8000
# Ray dashboard
EXPOSE 8265
# Ray internal metrics (Prometheus)
EXPOSE 9999

# Bug fix 3: TRANSFORMERS_CACHE deprecated in transformers v5, use HF_HOME only
ENV HF_HOME=/app/model-cache
# Disable Ray usage stats prompt
ENV RAY_USAGE_STATS_ENABLED=0

CMD ["python", "ray_app.py"]
