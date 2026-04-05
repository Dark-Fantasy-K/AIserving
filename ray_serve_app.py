"""
Ray Serve AI Inference 服务
- 模型: distilbert-base-uncased (轻量，适合 t2.micro)
- 功能: 文本情感分析 / 文本分类
- 自带 autoscaling: min_replicas=1, max_replicas=3
"""

import ray
from ray import serve
from ray.serve.config import AutoscalingConfig
import time
import os


# ============================================================
# 模型部署 - 情感分析
# ============================================================
@serve.deployment(
    name="sentiment_model",
    # ---------- Autoscaling 配置 ----------
    autoscaling_config=AutoscalingConfig(
        min_replicas=1,              # 最少 1 个副本
        max_replicas=3,              # 最多 3 个（t2.micro 建议别超过 2）
        target_ongoing_requests=2,   # 每个副本目标并发数
        upscale_delay_s=10,          # 扩容等待秒数
        downscale_delay_s=30,        # 缩容等待秒数
    ),
    ray_actor_options={
        "num_cpus": 0.5,             # 每个副本用 0.5 CPU
        "memory": 512 * 1024 * 1024, # 每个副本限制 512MB 内存
    },
    max_ongoing_requests=5,          # 单副本最大并发
)
class SentimentModel:
    def __init__(self):
        """加载模型 - 首次请求时才真正加载（lazy loading）"""
        self._pipeline = None
        print("[SentimentModel] 部署就绪，模型将在首次请求时加载")

    def _load_model(self):
        """懒加载模型，节省启动时间"""
        if self._pipeline is None:
            from transformers import pipeline
            print("[SentimentModel] 正在加载模型...")
            start = time.time()
            self._pipeline = pipeline(
                "sentiment-analysis",
                model="distilbert-base-uncased-finetuned-sst-2-english",
                device=-1,  # 强制 CPU
            )
            print(f"[SentimentModel] 模型加载完成，耗时 {time.time()-start:.1f}s")

    async def __call__(self, request):
        """处理推理请求"""
        data = await request.json()
        text = data.get("text", "")

        if not text:
            return {"error": "请提供 'text' 字段"}

        self._load_model()

        start = time.time()
        result = self._pipeline(text, truncation=True, max_length=512)
        latency = time.time() - start

        return {
            "text": text[:100],  # 截断显示
            "label": result[0]["label"],
            "score": round(result[0]["score"], 4),
            "latency_ms": round(latency * 1000, 1),
            "replica": os.getpid(),  # 显示哪个副本处理的，方便观察 autoscaling
        }


# ============================================================
# 健康检查端点
# ============================================================
@serve.deployment(name="health", num_replicas=1)
class HealthCheck:
    async def __call__(self, request):
        return {
            "status": "healthy",
            "timestamp": time.time(),
            "service": "ai-serving-lab",
        }


# ============================================================
# 绑定路由并启动
# ============================================================
sentiment_app = SentimentModel.bind()
health_app = HealthCheck.bind()

app = serve.run(
    sentiment_app,
    name="sentiment",
    route_prefix="/predict",
    host="0.0.0.0",  # 绑定所有接口，Tailscale 也能访问
    port=8000,
)

health = serve.run(
    health_app,
    name="health",
    route_prefix="/health",
)

print("""
╔══════════════════════════════════════════════════╗
║  AI Serving 已启动!                              ║
║                                                  ║
║  推理接口:  http://<TAILSCALE_IP>:8000/predict   ║
║  健康检查:  http://<TAILSCALE_IP>:8000/health    ║
║  Ray 面板:  http://<TAILSCALE_IP>:8265           ║
║                                                  ║
║  测试命令:                                        ║
║  curl -X POST http://localhost:8000/predict \\    ║
║    -H 'Content-Type: application/json' \\         ║
║    -d '{"text": "This product is amazing!"}'     ║
╚══════════════════════════════════════════════════╝
""")

# 保持进程运行
import signal
signal.pause()
