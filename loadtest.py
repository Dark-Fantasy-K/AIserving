#!/usr/bin/env python3
"""
AIserving 压测脚本 — 触发 KEDA 自动扩容
用法:
  python3 loadtest.py                        # 默认压测 Flask
  python3 loadtest.py --target ray           # 压测 Ray Serve
  python3 loadtest.py --concurrency 20 --duration 120
  python3 loadtest.py --target both          # 同时压测两个服务
"""

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ── 颜色 ──────────────────────────────────────────────────────
R = "\033[0;31m"; G = "\033[0;32m"; Y = "\033[1;33m"
C = "\033[0;36m"; B = "\033[1;34m"; W = "\033[1m"; N = "\033[0m"

# ── 测试数据 ──────────────────────────────────────────────────
PAYLOADS = [
    {"text": "This product is absolutely amazing and I love it!"},
    {"text": "Terrible experience, would not recommend to anyone."},
    {"text": "The service was okay, nothing special about it."},
    {"text": "Outstanding quality, exceeded all my expectations!"},
    {"text": "Very disappointed with the results, waste of money."},
    {"text": "Decent product for the price, works as advertised."},
    {"text": "Best purchase I have ever made, highly recommend!"},
    {"text": "Not what I expected, returning it immediately."},
]

# ── 统计 ──────────────────────────────────────────────────────
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0
        self.success = 0
        self.errors = 0
        self.latencies = deque(maxlen=1000)
        self.start_time = time.time()
        self.window_reqs = deque(maxlen=100)   # (timestamp, success) for RPS window

    def record(self, latency_ms: float, ok: bool):
        with self.lock:
            self.total += 1
            ts = time.time()
            self.window_reqs.append((ts, ok))
            if ok:
                self.success += 1
                self.latencies.append(latency_ms)
            else:
                self.errors += 1

    def rps(self) -> float:
        now = time.time()
        with self.lock:
            recent = [t for t, _ in self.window_reqs if now - t <= 5]
        return len(recent) / 5.0

    def p50(self) -> float:
        with self.lock:
            lats = sorted(self.latencies)
        if not lats: return 0
        return lats[int(len(lats) * 0.50)]

    def p95(self) -> float:
        with self.lock:
            lats = sorted(self.latencies)
        if not lats: return 0
        return lats[int(len(lats) * 0.95)]

    def p99(self) -> float:
        with self.lock:
            lats = sorted(self.latencies)
        if not lats: return 0
        return lats[min(int(len(lats) * 0.99), len(lats)-1)]

    def elapsed(self) -> float:
        return time.time() - self.start_time

# ── 单次请求 ──────────────────────────────────────────────────
_REQUEST_TIMEOUT = 30  # 由 main() 设置

def do_request(url: str, payload: dict) -> tuple[float, bool, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            body = resp.read()
            latency = (time.time() - t0) * 1000
            result = json.loads(body)
            return latency, True, result.get("label", "?")
    except urllib.error.HTTPError as e:
        return (time.time() - t0) * 1000, False, f"HTTP {e.code}"
    except Exception as e:
        return (time.time() - t0) * 1000, False, str(e)[:40]

# ── kubectl 工具 ──────────────────────────────────────────────
def get_pod_count(deployment: str, namespace: str = "ai-serving") -> tuple[int, int]:
    """返回 (ready, total) 副本数"""
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "deployment", deployment, "-n", namespace,
             "-o", "jsonpath={.status.readyReplicas},{.status.replicas}"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        parts = out.split(",")
        ready = int(parts[0]) if parts[0] else 0
        total = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        return ready, total
    except Exception:
        return -1, -1

def get_scaledobject_status(name: str, namespace: str = "ai-serving") -> str:
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "scaledobject", name,
             "-n", namespace, "--no-headers",
             "-o", "custom-columns=ACTIVE:.status.conditions[?(@.type=='Active')].status,"
                              "REPLICAS:.status.observedGeneration"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        return out
    except Exception:
        return "?"

def get_hpa_replicas(hpa_name: str, namespace: str = "ai-serving") -> str:
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "hpa", hpa_name, "-n", namespace,
             "--no-headers", "-o",
             "custom-columns=CURRENT:.status.currentReplicas,"
                             "DESIRED:.status.desiredReplicas"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        return out
    except Exception:
        return "? ?"

def start_port_forward(svc: str, local_port: int, remote_port: int = 8000,
                       namespace: str = "ai-serving") -> subprocess.Popen:
    proc = subprocess.Popen(
        ["kubectl", "port-forward", f"svc/{svc}",
         f"{local_port}:{remote_port}", "-n", namespace],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)   # 等待 port-forward 就绪
    return proc

# ── 监控线程 ──────────────────────────────────────────────────
def monitor_loop(stats: Stats, deployments: list[tuple[str, str]], stop_evt: threading.Event):
    """后台打印 RPS / 延迟 / Pod 副本数"""
    print(f"\n{W}{'时间':>6}  {'RPS':>6}  {'P50ms':>7}  {'P95ms':>7}  {'P99ms':>7}  "
          f"{'成功':>7}  {'错误':>5}  副本数{N}")
    print("─" * 72)
    prev_counts = {}

    while not stop_evt.is_set():
        rps   = stats.rps()
        p50   = stats.p50()
        p95   = stats.p95()
        p99   = stats.p99()
        ok    = stats.success
        err   = stats.errors
        elapsed = int(stats.elapsed())

        replica_parts = []
        for dep, hpa in deployments:
            ready, total = get_pod_count(dep)
            hpa_str = get_hpa_replicas(hpa)
            desired = hpa_str.split()[1] if len(hpa_str.split()) > 1 else "?"

            # 检测 ready 副本数变化
            prev = prev_counts.get(dep, ready)
            if ready > prev:
                change = f"{G}↑{ready}/{total}{N}"
            elif ready < prev and ready >= 0:
                change = f"{Y}↓{ready}/{total}{N}"
            elif total > ready:
                # Pod 启动中（total > ready），用橙色提示
                change = f"{Y}{ready}/{total}(起动中){N}"
            else:
                change = f"{ready}/{total}"
            prev_counts[dep] = ready

            short = dep.replace("ai-serving-ray", "ray").replace("ai-serving", "flask")
            replica_parts.append(f"{short}={change}→{desired}")

        replicas = "  ".join(replica_parts)

        rps_col   = f"{G}{rps:>5.1f}{N}" if rps > 5 else f"{rps:>5.1f}"
        p95_col   = f"{Y}{p95:>6.0f}{N}" if p95 > 500 else f"{p95:>6.0f}"
        err_col   = f"{R}{err:>4}{N}" if err > 0 else f"{err:>4}"

        print(f"\r{elapsed:>5}s  {rps_col}  {p50:>7.0f}  {p95_col}  {p99:>7.0f}  "
              f"{ok:>7}  {err_col}  {replicas}", end="", flush=True)
        time.sleep(3)
    print()  # 换行

# ── 压测主循环 ────────────────────────────────────────────────
def run_load(url: str, stats: Stats, stop_evt: threading.Event,
             concurrency: int, ramp_time: int):
    """逐步提升并发，模拟真实流量增长"""
    import random

    def worker():
        idx = 0
        while not stop_evt.is_set():
            payload = PAYLOADS[idx % len(PAYLOADS)]
            idx += 1
            lat, ok, _ = do_request(url, payload)
            stats.record(lat, ok)

    # 分阶段启动线程（ramp up）
    threads = []
    step = max(1, concurrency // 5)
    current = 0
    ramp_interval = ramp_time / max(1, concurrency // step)

    while current < concurrency and not stop_evt.is_set():
        batch = min(step, concurrency - current)
        for _ in range(batch):
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            threads.append(t)
        current += batch
        print(f"\n{C}  → 并发数: {current}/{concurrency}{N}", end="", flush=True)
        if current < concurrency:
            time.sleep(ramp_interval)

    # 等待 stop
    stop_evt.wait()

# ── 主程序 ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AIserving 压测 & KEDA 扩容观测")
    parser.add_argument("--target",      choices=["flask", "ray", "both"], default="flask")
    parser.add_argument("--concurrency", type=int, default=10,  help="并发线程数 (默认 10, CPU推理建议≤15)")
    parser.add_argument("--duration",    type=int, default=180, help="压测时长秒 (默认 180)")
    parser.add_argument("--ramp",        type=int, default=20,  help="爬坡时间秒 (默认 20)")
    parser.add_argument("--timeout",     type=int, default=30,  help="单次请求超时秒 (默认 30)")
    args = parser.parse_args()

    print(f"""
{W}╔══════════════════════════════════════════════════════╗
║   🔥  AIserving 压测 — KEDA 扩容触发器               ║
╚══════════════════════════════════════════════════════╝{N}
  目标服务:  {C}{args.target}{N}
  并发线程:  {args.concurrency}
  压测时长:  {args.duration}s
  爬坡时间:  {args.ramp}s
""")

    # ── 启动 port-forward ──────────────────────────────────────
    pf_procs = []
    urls = []
    deployments = []

    if args.target in ("flask", "both"):
        print(f"{C}🔗 启动 port-forward: ai-serving → localhost:18000{N}")
        pf_procs.append(start_port_forward("ai-serving", 18000))
        urls.append(("Flask", "http://localhost:18000/predict"))
        deployments.append(("ai-serving", "keda-hpa-ai-serving-scaledobject"))

    if args.target in ("ray", "both"):
        print(f"{C}🔗 启动 port-forward: ai-serving-ray → localhost:18001{N}")
        pf_procs.append(start_port_forward("ai-serving-ray", 18001))
        urls.append(("Ray", "http://localhost:18001/predict"))
        deployments.append(("ai-serving-ray", "keda-hpa-ai-serving-ray-scaledobject"))

    # ── 健康检查 ──────────────────────────────────────────────
    print(f"\n{C}🏥 健康检查...{N}")
    for name, url in urls:
        health_url = url.replace("/predict", "/health")
        try:
            with urllib.request.urlopen(health_url, timeout=5) as r:
                body = json.loads(r.read())
                status = body.get("status", "?")
                color = G if status == "healthy" else Y
                print(f"   {name}: {color}{status}{N}")
        except Exception as e:
            print(f"   {name}: {R}FAIL — {e}{N}")
            print(f"   {Y}提示: Pod 可能还未就绪，等待 10s 后继续...{N}")
            time.sleep(10)

    # ── 压测 ──────────────────────────────────────────────────
    stop_evt = threading.Event()
    all_stats = []

    print(f"\n{W}🚀 开始压测 (时长: {args.duration}s, 并发: {args.concurrency}){N}")

    # 监控线程
    mon = threading.Thread(
        target=monitor_loop,
        args=(Stats(), deployments, stop_evt),
        daemon=True
    )

    # 每个目标独立 Stats
    load_threads = []
    for name, url in urls:
        s = Stats()
        all_stats.append((name, s))
        lt = threading.Thread(
            target=run_load,
            args=(url, s, stop_evt, args.concurrency, args.ramp),
            daemon=True
        )
        load_threads.append(lt)

    # 用第一个 stats 做监控显示
    mon_stats = all_stats[0][1]
    mon = threading.Thread(
        target=monitor_loop,
        args=(mon_stats, deployments, stop_evt),
        daemon=True
    )
    mon.start()

    global _REQUEST_TIMEOUT
    _REQUEST_TIMEOUT = args.timeout

    for lt in load_threads:
        lt.start()

    time.sleep(args.duration)
    stop_evt.set()

    for lt in load_threads:
        lt.join(timeout=5)
    mon.join(timeout=5)

    # ── 结果汇总 ──────────────────────────────────────────────
    print(f"\n\n{W}{'═'*60}{N}")
    print(f"{W}📊 压测结果{N}")
    print(f"{W}{'═'*60}{N}")

    for name, s in all_stats:
        elapsed = s.elapsed()
        total_rps = s.success / elapsed if elapsed > 0 else 0
        err_rate  = s.errors / s.total * 100 if s.total > 0 else 0
        print(f"""
  {W}{name}{N}
    总请求数:  {s.total}
    成功:      {G}{s.success}{N}  失败: {R}{s.errors}{N}  错误率: {Y}{err_rate:.1f}%{N}
    平均 RPS:  {total_rps:.1f}
    P50 延迟:  {s.p50():.0f}ms
    P95 延迟:  {s.p95():.0f}ms
    P99 延迟:  {s.p99():.0f}ms""")

    print(f"\n{W}📈 最终副本数:{N}")
    for dep, hpa in deployments:
        ready, total = get_pod_count(dep)
        hpa_str = get_hpa_replicas(hpa)
        print(f"   {dep}: {G}{ready}/{total} ready{N}  HPA current/desired={hpa_str}")

    print(f"\n{C}kubectl get hpa -n ai-serving{N}")
    subprocess.run(["kubectl", "get", "hpa", "-n", "ai-serving"])

    # ── 清理 port-forward ─────────────────────────────────────
    for p in pf_procs:
        p.terminate()

    print(f"\n{G}✅ 压测完成{N}\n")

if __name__ == "__main__":
    main()
