"""
Flask 轻量 AI Serving（备选方案）
- 适合 t2.micro 内存紧张的情况
- 手动实现简易 autoscaling 逻辑
- 用 gunicorn 多 worker 模拟扩缩容
"""

from flask import Flask, request, jsonify
import time
import os
import threading
import psutil

# ============================================================
# OpenTelemetry 初始化（需要在 Flask app 创建之前）
# ============================================================
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor

_otel_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
_service_name = os.environ.get("OTEL_SERVICE_NAME", "ai-serving")

if _otel_endpoint:
    resource = Resource.create({"service.name": _service_name})
    provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter(
        endpoint=f"{_otel_endpoint}/v1/traces",
    )
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(provider)
    print(f"[OTEL] Tracing enabled → {_otel_endpoint}")
else:
    print("[OTEL] OTEL_EXPORTER_OTLP_ENDPOINT not set, tracing disabled")

tracer = trace.get_tracer(__name__)

# ============================================================
# Flask app + Prometheus metrics
# ============================================================
app = Flask(__name__)

# Auto-instrument Flask (creates spans for every request)
FlaskInstrumentor().instrument_app(app)
# Auto-instrument outbound requests
RequestsInstrumentor().instrument()

from prometheus_flask_exporter import PrometheusMetrics
metrics_exporter = PrometheusMetrics(app)
# Static info metric
metrics_exporter.info("ai_serving_info", "AI Serving info", version="1.0.0")

# ============================================================
# 全局模型（懒加载）
# ============================================================
_model = None
_model_lock = threading.Lock()

# Custom Prometheus counters / histograms
from prometheus_client import Counter, Histogram

PREDICT_REQUESTS = Counter(
    "ai_serving_predict_requests_total",
    "Total predict requests",
    ["status"],
)
PREDICT_LATENCY = Histogram(
    "ai_serving_predict_latency_seconds",
    "Predict inference latency",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)


def get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from transformers import pipeline
                print("[Flask] 加载模型中...")
                _model = pipeline(
                    "sentiment-analysis",
                    model="distilbert-base-uncased-finetuned-sst-2-english",
                    device=-1,
                )
                print("[Flask] 模型加载完毕")
    return _model


# ============================================================
# API 路由
# ============================================================
@app.route("/predict", methods=["POST"])
def predict():
    """推理接口"""
    data = request.get_json(force=True)
    text = data.get("text", "")

    if not text:
        PREDICT_REQUESTS.labels(status="error").inc()
        return jsonify({"error": "请提供 'text' 字段"}), 400

    with tracer.start_as_current_span("model-inference") as span:
        span.set_attribute("input.length", len(text))

        model = get_model()
        start = time.time()
        result = model(text, truncation=True, max_length=512)
        latency = time.time() - start

        span.set_attribute("inference.latency_ms", round(latency * 1000, 1))
        span.set_attribute("inference.label", result[0]["label"])
        span.set_attribute("inference.score", result[0]["score"])

    PREDICT_LATENCY.observe(latency)
    PREDICT_REQUESTS.labels(status="success").inc()

    return jsonify({
        "text": text[:100],
        "label": result[0]["label"],
        "score": round(result[0]["score"], 4),
        "latency_ms": round(latency * 1000, 1),
        "worker_pid": os.getpid(),
    })


@app.route("/health", methods=["GET"])
def health():
    """健康检查"""
    mem = psutil.virtual_memory()
    return jsonify({
        "status": "healthy",
        "timestamp": time.time(),
        "memory_used_pct": mem.percent,
        "cpu_pct": psutil.cpu_percent(interval=0.1),
    })


# /metrics is handled automatically by PrometheusMetrics (Prometheus format)

# ============================================================
# 直接运行（开发用）
# ============================================================
if __name__ == "__main__":
    print("""
    ┌───────────────────────────────────────┐
    │  Flask AI Serving (dev mode)          │
    │  POST /predict  - inference interface │
    │  GET  /health   - health check        │
    │  GET  /metrics  - prometheus metrics  │
    └───────────────────────────────────────┘
    """)
    app.run(host="0.0.0.0", port=8000, debug=False)
