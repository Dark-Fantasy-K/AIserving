"""
Ray Serve AI Serving
- 所有含锁的对象（OTEL tracer、Prometheus counters）均在 __init__ 内初始化
  避免 Ray 序列化类时 pickle 失败
- ASGI 协议转发 HTTP 请求给 FastAPI
"""

import os
import time
import psutil

import ray
from ray import serve
from ray.util.metrics import Counter as RayCounter, Histogram as RayHistogram


# ============================================================
# Ray Serve Deployment
# ============================================================
@serve.deployment(
    name="sentiment-analysis",
    ray_actor_options={"num_cpus": 0.5, "memory": 800 * 1024 * 1024},
    autoscaling_config={
        "min_replicas": 1,
        "max_replicas": 3,
        "target_num_ongoing_requests_per_replica": 5,
        "upscale_delay_s": 10,
        "downscale_delay_s": 60,
    },
    health_check_period_s=10,
    health_check_timeout_s=30,
)
class SentimentServe:
    def __init__(self):
        # ---- 1. OTEL（在 actor 进程内初始化，不被 pickle）----
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        otel_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        service_name = os.environ.get("OTEL_SERVICE_NAME", "ai-serving-ray")

        if otel_endpoint:
            resource = Resource.create({"service.name": service_name})
            provider = TracerProvider(resource=resource)
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=f"{otel_endpoint}/v1/traces")
                )
            )
            trace.set_tracer_provider(provider)
            print(f"[OTEL] Tracing enabled → {otel_endpoint}")

        self._tracer = trace.get_tracer(__name__)
        RequestsInstrumentor().instrument()

        # ---- 2. Prometheus counters（在 actor 进程内初始化）----
        from prometheus_client import (
            Counter as PromCounter,
            Histogram as PromHistogram,
            generate_latest,
            CONTENT_TYPE_LATEST,
        )
        self._generate_latest = generate_latest
        self._CONTENT_TYPE_LATEST = CONTENT_TYPE_LATEST

        self._prom_requests = PromCounter(
            "ai_serving_ray_predict_requests_total",
            "Total predict requests", ["status"],
        )
        self._prom_latency = PromHistogram(
            "ai_serving_ray_predict_latency_seconds",
            "Predict inference latency",
            buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
        )

        # ---- 3. Ray built-in metrics ----
        self._ray_requests = RayCounter(
            "serve_predict_requests",
            description="Predict requests per replica",
            tag_keys=("status",),
        )
        self._ray_latency = RayHistogram(
            "serve_predict_latency_ms",
            description="Predict latency in ms",
            boundaries=[50, 100, 250, 500, 1000, 2000, 5000],
        )

        # ---- 4. Model ----
        from transformers import pipeline
        print("[Ray Serve] 加载模型中...")
        self._model = pipeline(
            "sentiment-analysis",
            model="distilbert-base-uncased-finetuned-sst-2-english",
            device=-1,
        )

        # ---- 5. Replica info ----
        self._replica_id = str(serve.get_replica_context().replica_id)

        print(f"[Ray Serve] 启动完成，replica={self._replica_id}")

    async def __call__(self, request):
        """Ray Serve 入口：接收 starlette.requests.Request，手动路由"""
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response as StarletteResponse

        path = request.url.path
        method = request.method

        if method == "POST" and path == "/predict":
            body = await request.json()
            text = body.get("text", "")
            if not text:
                self._prom_requests.labels(status="error").inc()
                return JSONResponse({"error": "请提供 'text' 字段"}, status_code=400)

            with self._tracer.start_as_current_span("model-inference") as span:
                span.set_attribute("input.length", len(text))
                start = time.time()
                result = self._model(text, truncation=True, max_length=512)
                latency = time.time() - start
                span.set_attribute("inference.latency_ms", round(latency * 1000, 1))
                span.set_attribute("inference.label", result[0]["label"])
                span.set_attribute("inference.score", result[0]["score"])

            latency_ms = round(latency * 1000, 1)
            self._ray_requests.inc(tags={"status": "success"})
            self._ray_latency.observe(latency_ms)
            self._prom_requests.labels(status="success").inc()
            self._prom_latency.observe(latency)

            return JSONResponse({
                "text": text[:100],
                "label": result[0]["label"],
                "score": round(result[0]["score"], 4),
                "latency_ms": latency_ms,
                "replica_id": self._replica_id,
            })

        elif method == "GET" and path == "/health":
            mem = psutil.virtual_memory()
            return JSONResponse({
                "status": "healthy",
                "timestamp": time.time(),
                "memory_used_pct": mem.percent,
                "cpu_pct": psutil.cpu_percent(interval=0.1),
                "replica_id": self._replica_id,
            })

        elif method == "GET" and path == "/metrics":
            return StarletteResponse(
                content=self._generate_latest(),
                media_type=self._CONTENT_TYPE_LATEST,
            )

        else:
            return JSONResponse({"error": "Not found"}, status_code=404)

    def check_health(self):
        return True


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    ray.init(
        address="local",
        dashboard_host="0.0.0.0",
        dashboard_port=8265,
        ignore_reinit_error=True,
    )

    serve.start(
        http_options={"host": "0.0.0.0", "port": 8000},
        metrics_export_port=9999,
    )

    serve.run(SentimentServe.bind(), name="ai-serving-ray", route_prefix="/")

    print("[Ray Serve] 服务就绪")
    print("  POST /predict  - 推理接口")
    print("  GET  /health   - 健康检查")
    print("  GET  /metrics  - Prometheus metrics")
    print("  Ray dashboard  :8265")

    while True:
        time.sleep(30)
