FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies (non-torch first)
COPY requirements-k8s.txt .
RUN pip install --no-cache-dir -r requirements-k8s.txt

# Install PyTorch CPU-only explicitly (much smaller than default)
RUN pip install --no-cache-dir \
    torch==2.2.2 \
    --index-url https://download.pytorch.org/whl/cpu

COPY flask_app.py .

EXPOSE 8000

# Model is downloaded at runtime to /app/model-cache (mounted as PVC or hostPath)
ENV TRANSFORMERS_CACHE=/app/model-cache
ENV HF_HOME=/app/model-cache

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "180", "--access-logfile", "-", "flask_app:app"]
