"""
压力测试 & Autoscaling 验证脚本

功能:
1. 发送并发请求观察 autoscaling 扩容
2. 停止请求后观察缩容
3. 记录每个请求的延迟和处理副本
"""

import requests
import time
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# 配置
# ============================================================
BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
PREDICT_URL = f"{BASE_URL}/predict"
HEALTH_URL = f"{BASE_URL}/health"

TEST_TEXTS = [
    "This movie was absolutely fantastic and I loved every minute of it!",
    "The food was terrible and the service was even worse.",
    "I feel neutral about this product, it's neither good nor bad.",
    "What an incredible experience, I would recommend it to everyone!",
    "Disappointing quality, would not buy again.",
    "The weather today is quite nice and sunny.",
    "I'm extremely frustrated with this software, it keeps crashing.",
    "Best purchase I've ever made, worth every penny!",
]


def send_request(text, request_id):
    """发送单个推理请求"""
    try:
        start = time.time()
        resp = requests.post(
            PREDICT_URL,
            json={"text": text},
            timeout=30,
        )
        elapsed = time.time() - start
        data = resp.json()
        return {
            "id": request_id,
            "status": resp.status_code,
            "label": data.get("label"),
            "score": data.get("score"),
            "server_latency_ms": data.get("latency_ms"),
            "total_latency_ms": round(elapsed * 1000, 1),
            "replica/worker": data.get("replica") or data.get("worker_pid"),
        }
    except Exception as e:
        return {"id": request_id, "error": str(e)}


def check_health():
    """检查服务健康状态"""
    try:
        resp = requests.get(HEALTH_URL, timeout=5)
        print(f"  Health: {resp.json()}")
    except Exception as e:
        print(f"  Health check failed: {e}")


def run_load_test(concurrency, num_requests, label=""):
    """运行一轮压力测试"""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  并发数: {concurrency}, 总请求: {num_requests}")
    print(f"{'='*60}")

    check_health()

    results = []
    start_all = time.time()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = []
        for i in range(num_requests):
            text = TEST_TEXTS[i % len(TEST_TEXTS)]
            futures.append(executor.submit(send_request, text, i))

        for f in as_completed(futures):
            result = f.result()
            results.append(result)
            if "error" not in result:
                print(f"  [#{result['id']:03d}] {result['label']:>8} "
                      f"({result['score']}) "
                      f"延迟: {result['total_latency_ms']:>6.0f}ms "
                      f"副本: {result['replica/worker']}")
            else:
                print(f"  [#{result['id']:03d}] ERROR: {result['error']}")

    total_time = time.time() - start_all
    success = [r for r in results if "error" not in r]
    latencies = [r["total_latency_ms"] for r in success]

    print(f"\n  --- 统计 ---")
    print(f"  成功: {len(success)}/{num_requests}")
    print(f"  总耗时: {total_time:.1f}s")
    if latencies:
        print(f"  平均延迟: {sum(latencies)/len(latencies):.0f}ms")
        print(f"  最大延迟: {max(latencies):.0f}ms")
        print(f"  最小延迟: {min(latencies):.0f}ms")
        # 看哪些副本在处理
        replicas = set(r["replica/worker"] for r in success)
        print(f"  活跃副本: {len(replicas)} 个 -> {replicas}")
    print(f"  QPS: {len(success)/total_time:.1f}")

    return results


# ============================================================
# 主测试流程
# ============================================================
if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════╗
║  AI Serving 压力测试 & Autoscaling 验证      ║
║  目标: {BASE_URL:<38} ║
╚══════════════════════════════════════════════╝
    """)

    # 阶段 1: 预热（低负载）
    run_load_test(
        concurrency=1,
        num_requests=3,
        label="阶段 1: 预热（1 并发，3 请求）"
    )

    time.sleep(5)

    # 阶段 2: 中等负载
    run_load_test(
        concurrency=3,
        num_requests=10,
        label="阶段 2: 中等负载（3 并发，10 请求）"
    )

    time.sleep(5)

    # 阶段 3: 高负载 -> 触发 autoscaling
    run_load_test(
        concurrency=8,
        num_requests=20,
        label="阶段 3: 高负载 -> 观察扩容（8 并发，20 请求）"
    )

    # 阶段 4: 等待缩容
    print(f"\n{'='*60}")
    print("  阶段 4: 等待 30 秒观察缩容...")
    print(f"{'='*60}")
    for i in range(6):
        time.sleep(5)
        print(f"  [{i*5+5}s] ", end="")
        check_health()

    # 阶段 5: 低负载验证缩容
    run_load_test(
        concurrency=1,
        num_requests=3,
        label="阶段 5: 低负载验证（应该已缩容）"
    )

    print("\n✅ 测试完成!")
