# AI Serving Lab

在 AWS EC2 上运行 k3s（轻量 Kubernetes）的 AI 推理服务，通过 Tailscale 内网访问。支持两种 Serving 后端、HPA 自动扩缩容，以及完整的可观测性栈。

> **Language / 语言**: [English](README.md) | [中文](README_CN.md)

---

## 架构

```
Client（Tailscale 内网）
        │
        │ Tailscale VPN
        ▼
┌─────────────────────────────────────────────┐
│  AWS EC2 (t2.micro / t3.micro)              │
│                                             │
│  k3s (Kubernetes)                           │
│  ┌─────────────────────────────────────┐    │
│  │  Namespace: ai-serving              │    │
│  │                                     │    │
│  │  Deployment（Flask 或 Ray Serve）   │    │
│  │    replicas: 1 → 3（HPA）          │    │
│  │    HPA: CPU 70% / Memory 80%        │    │
│  │                                     │    │
│  │  Ingress (Traefik) → :8000          │    │
│  │  PVC: model-cache                   │    │
│  └─────────────────────────────────────┘    │
│                                             │
│  ┌─────────────────────────────────────┐    │
│  │  Namespace: observability            │    │
│  │    Prometheus  Grafana  Jaeger       │    │
│  └─────────────────────────────────────┘    │
│                                             │
│  Tailscale (100.x.x.x)                      │
└─────────────────────────────────────────────┘
```

---

## 项目结构

```
AIserving/
├── deployment/
│   ├── namespace.yaml          # ai-serving 命名空间
│   ├── deployment.yaml         # Deployment + HPA + Ingress
│   ├── service.yaml            # ClusterIP Service
│   ├── hpa.yaml                # HorizontalPodAutoscaler
│   ├── ingress.yaml            # Traefik Ingress
│   ├── pv-model-cache.yaml     # 模型缓存用 PersistentVolume
│   ├── flask_ai/               # 后端方案 A：Flask + Gunicorn
│   │   ├── flask_app.py
│   │   ├── Dockerfile
│   │   └── requirements-k8s.txt
│   ├── ray_serve/              # 后端方案 B：Ray Serve
│   │   ├── ray_app.py
│   │   ├── Dockerfile
│   │   ├── deployment.yaml
│   │   ├── service.yaml
│   │   └── requirements-k8s.txt
│   └── observability/          # Prometheus + Grafana + Jaeger
│       ├── kustomization.yaml
│       ├── namespace.yaml
│       ├── prometheus/
│       ├── grafana/
│       └── jaeger/
├── load_test.py                # 压力测试与自动扩缩容验证
└── requirements.txt            # 本地开发依赖
```

---

## 两种 Serving 后端

### 方案 A：Flask AI（`deployment/flask_ai/`）

- Flask + Gunicorn 多 Worker
- 通过 `prometheus-flask-exporter` 暴露 Prometheus 指标（`/metrics`）
- OpenTelemetry 链路追踪 → Jaeger
- 内存占用低，适合 `t2.micro`

### 方案 B：Ray Serve（`deployment/ray_serve/`）

- Ray Serve 原生自动扩缩容（基于请求队列长度）
- 内置 Ray 指标 + Prometheus counters/histograms
- OpenTelemetry 链路追踪 → Jaeger
- Ray Dashboard：`:8265`
- 扩缩容配置：min=1, max=3，目标每副本 5 个并发请求

---

## 模型

**`distilbert-base-uncased-finetuned-sst-2-english`** — 情感分析（POSITIVE / NEGATIVE）

模型缓存在 PersistentVolume（`/app/model-cache`）中，Pod 重启后无需重新下载。

---

## API 接口

### `POST /predict`

```bash
curl -X POST http://<tailscale-ip>/predict \
  -H "Content-Type: application/json" \
  -d '{"text": "This movie was absolutely fantastic!"}'
```

响应：
```json
{
  "text": "This movie was absolutely fantastic!",
  "label": "POSITIVE",
  "score": 0.9998,
  "latency_ms": 42.3,
  "worker_pid": 12345
}
```

### `GET /health`

```json
{
  "status": "healthy",
  "timestamp": 1712345678.0,
  "memory_used_pct": 63.2,
  "cpu_pct": 12.5
}
```

### `GET /metrics`

Prometheus 格式指标，供 Prometheus 抓取。

---

## 自动扩缩容

HPA 配置位于 `deployment/deployment.yaml`：

| 参数 | 值 |
|------|-----|
| minReplicas | 1 |
| maxReplicas | 3 |
| CPU 触发阈值 | 平均利用率 70% |
| Memory 触发阈值 | 平均利用率 80% |
| 扩容稳定窗口 | 30s |
| 缩容稳定窗口 | 120s |

---

## 可观测性

所有组件通过 Kustomize 部署在 `observability` 命名空间中。

```bash
kubectl apply -k deployment/observability/
```

| 组件 | 用途 |
|------|------|
| **Prometheus** | 指标采集（自动发现带 `prometheus.io/scrape: "true"` 注解的 Pod） |
| **Grafana** | 延迟、吞吐量、副本数等 Dashboard |
| **Jaeger** | 分布式链路追踪（OTLP over HTTP，端口 4318） |

Pod 注解配置：
```yaml
prometheus.io/scrape: "true"
prometheus.io/port: "8000"
prometheus.io/path: "/metrics"
```

---

## 压力测试

```bash
# 对本地集群测试
python load_test.py http://<tailscale-ip>

# 或通过 NodePort 测试
python load_test.py http://localhost:30800
```

测试阶段：
1. 预热：1 并发，3 请求
2. 中等负载：3 并发，10 请求
3. 高负载：8 并发，20 请求（触发扩容）
4. 冷却：等待 30s 观察缩容
5. 验证缩容：1 并发，3 请求

---

## 快速开始

### 1. EC2 + k3s 环境准备

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git

# 安装 k3s
curl -sfL https://get.k3s.io | sh -

# 安装 Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# 获取 Tailscale IP
tailscale ip -4
```

### 2. 构建镜像

```bash
# Flask 后端
cd deployment/flask_ai
sudo nerdctl build -t ai-serving:latest .

# 或 Ray Serve 后端
cd deployment/ray_serve
sudo nerdctl build -t ai-serving:latest .
```

### 3. 部署

```bash
# 应用所有 manifest
kubectl apply -f deployment/namespace.yaml
kubectl apply -f deployment/pv-model-cache.yaml
kubectl apply -f deployment/deployment.yaml

# 或部署 Ray Serve
kubectl apply -f deployment/ray_serve/deployment.yaml
kubectl apply -f deployment/ray_serve/service.yaml

# 部署可观测性栈
kubectl apply -k deployment/observability/
```

### 4. 验证

```bash
kubectl get pods -n ai-serving
kubectl get hpa -n ai-serving

# 测试接口
curl http://$(tailscale ip -4)/health
```
