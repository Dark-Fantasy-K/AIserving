"""
Gunicorn 配置 - 简易 Autoscaling

通过动态调整 worker 数量实现类似 autoscaling 的效果
适合 t2.micro (1 vCPU, 1GB RAM)
"""

import multiprocessing
import os

# ============================================================
# 基础配置
# ============================================================
bind = "0.0.0.0:8000"
workers = 2                    # 初始 worker 数（t2.micro 建议 1-2）
worker_class = "gworker"       # 如果装了 gevent 用 "gevent"，否则用 "sync"
worker_class = "sync"
timeout = 120                  # 首次加载模型可能慢，给 120s
keepalive = 5
max_requests = 500             # 每个 worker 处理 500 请求后重启（防内存泄漏）
max_requests_jitter = 50

# ============================================================
# 日志
# ============================================================
accesslog = "-"                # stdout
errorlog = "-"
loglevel = "info"

# ============================================================
# 内存限制（t2.micro 只有 1GB）
# ============================================================
# preload_app = True           # 共享模型内存，但需要模型支持 fork
preload_app = False            # 安全起见不预加载

# ============================================================
# 启动命令
# ============================================================
# gunicorn -c gunicorn_config.py flask_app:app
