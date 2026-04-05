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

app = Flask(__name__)

# ============================================================
# 全局模型（懒加载）
# ============================================================
_model = None
_model_lock = threading.Lock()


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
        return jsonify({"error": "请提供 'text' 字段"}), 400

    model = get_model()
    start = time.time()
    result = model(text, truncation=True, max_length=512)
    latency = time.time() - start

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


@app.route("/metrics", methods=["GET"])
def metrics():
    """简单的监控指标（用于判断是否需要扩缩容）"""
    mem = psutil.virtual_memory()
    return jsonify({
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "memory_percent": mem.percent,
        "memory_available_mb": mem.available // (1024 * 1024),
        "pid": os.getpid(),
    })


# ============================================================
# 直接运行（开发用）
# ============================================================
if __name__ == "__main__":
    print("""
    ┌──────────────────────────────────────┐
    │  Flask AI Serving (dev mode)         │
    │  POST /predict  - 推理接口           │
    │  GET  /health   - 健康检查           │
    │  GET  /metrics  - 系统指标           │
    └──────────────────────────────────────┘
    """)
    app.run(host="0.0.0.0", port=8000, debug=False)
