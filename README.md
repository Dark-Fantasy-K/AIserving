# AI Serving Lab

A lightweight AI inference service running on k3s (Kubernetes) on AWS EC2, accessible via Tailscale private network. Supports two serving backends, HPA autoscaling, and a full observability stack.

在 AWS EC2 上运行 k3s（轻量 Kubernetes）的 AI 推理服务，通过 Tailscale 内网访问。支持两种 Serving 后端、HPA 自动扩缩容，以及完整的可观测性栈。

---

## Architecture / 架构

```
Client (Tailscale 内网)
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
│  │  Deployment (Flask or Ray Serve)    │    │
│  │    replicas: 1 → 3 (HPA)           │    │
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

## Project Structure / 项目结构

```
AIserving/
├── deployment/
│   ├── namespace.yaml          # ai-serving namespace
│   ├── deployment.yaml         # Deployment + HPA + Ingress
│   ├── service.yaml            # ClusterIP Service
│   ├── hpa.yaml                # HorizontalPodAutoscaler
│   ├── ingress.yaml            # Traefik Ingress
│   ├── pv-model-cache.yaml     # PersistentVolume for model cache
│   ├── flask_ai/               # Backend option A: Flask + Gunicorn
│   │   ├── flask_app.py
│   │   ├── Dockerfile
│   │   └── requirements-k8s.txt
│   ├── ray_serve/              # Backend option B: Ray Serve
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
├── load_test.py                # Load test & autoscaling validation
└── requirements.txt            # Local dev dependencies
```

---

## Serving Backends / 两种 Serving 后端

### Option A: Flask AI (`deployment/flask_ai/`)

- Flask + Gunicorn multi-worker
- Prometheus metrics via `prometheus-flask-exporter` (`/metrics`)
- OpenTelemetry traces → Jaeger
- Memory-efficient; suitable for `t2.micro`

---

- Flask + Gunicorn 多 Worker
- 通过 `prometheus-flask-exporter` 暴露 Prometheus 指标（`/metrics`）
- OpenTelemetry 链路追踪 → Jaeger
- 内存占用低，适合 `t2.micro`

### Option B: Ray Serve (`deployment/ray_serve/`)

- Ray Serve with native autoscaling (request-based, per replica)
- Built-in Ray metrics + Prometheus counters/histograms
- OpenTelemetry traces → Jaeger
- Ray dashboard on `:8265`
- Autoscaling config: min=1, max=3, target 5 ongoing requests/replica

---

- Ray Serve 原生自动扩缩容（基于请求队列长度）
- 内置 Ray 指标 + Prometheus counters/histograms
- OpenTelemetry 链路追踪 → Jaeger
- Ray Dashboard：`:8265`
- 扩缩容配置：min=1, max=3，目标每副本 5 个并发请求

---

## Model / 模型

**`distilbert-base-uncased-finetuned-sst-2-english`** — Sentiment analysis (POSITIVE / NEGATIVE)

Model is cached in a PersistentVolume (`/app/model-cache`) to avoid re-downloading on pod restart.

模型缓存在 PersistentVolume（`/app/model-cache`）中，Pod 重启后无需重新下载。

---

## API Endpoints / API 接口

### `POST /predict`

```bash
curl -X POST http://<tailscale-ip>/predict \
  -H "Content-Type: application/json" \
  -d '{"text": "This movie was absolutely fantastic!"}'
```

Response / 响应：
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

Prometheus-format metrics for scraping.

Prometheus 格式指标，供 Prometheus 抓取。

---

## Autoscaling / 自动扩缩容

HPA is configured in `deployment/deployment.yaml`:

HPA 配置位于 `deployment/deployment.yaml`：

| Parameter | Value |
|-----------|-------|
| minReplicas | 1 |
| maxReplicas | 3 |
| CPU trigger | 70% average utilization |
| Memory trigger | 80% average utilization |
| Scale-up stabilization | 30s |
| Scale-down stabilization | 120s |

---

## Observability / 可观测性

All components are deployed in the `observability` namespace via Kustomize.

所有组件通过 Kustomize 部署在 `observability` 命名空间中。

```bash
kubectl apply -k deployment/observability/
```

| Component | Purpose |
|-----------|---------|
| **Prometheus** | Metrics scraping (auto-discovers pods with `prometheus.io/scrape: "true"`) |
| **Grafana** | Dashboards for latency, throughput, replica count |
| **Jaeger** | Distributed tracing (OTLP over HTTP, port 4318) |

Pods annotated with:
```yaml
prometheus.io/scrape: "true"
prometheus.io/port: "8000"
prometheus.io/path: "/metrics"
```

---

## Load Testing / 压力测试

```bash
# Run against local cluster
python load_test.py http://<tailscale-ip>

# Or against NodePort
python load_test.py http://localhost:30800
```

Test stages / 测试阶段：
1. Warm-up: 1 concurrent, 3 requests / 预热：1 并发，3 请求
2. Medium load: 3 concurrent, 10 requests / 中等负载：3 并发，10 请求
3. High load: 8 concurrent, 20 requests (triggers scale-up) / 高负载：8 并发，20 请求（触发扩容）
4. Cool-down: 30s wait to observe scale-down / 冷却：等待 30s 观察缩容
5. Verify scale-down: 1 concurrent, 3 requests / 验证缩容：1 并发，3 请求

---

## Quick Start / 快速开始

### 1. EC2 + k3s Setup / 环境准备

```bash
# Update system
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git

# Install k3s
curl -sfL https://get.k3s.io | sh -

# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Get Tailscale IP
tailscale ip -4
```

### 2. Build & Load Image / 构建镜像

```bash
# Flask backend
cd deployment/flask_ai
sudo nerdctl build -t ai-serving:latest .

# Or Ray Serve backend
cd deployment/ray_serve
sudo nerdctl build -t ai-serving:latest .
```

### 3. Deploy / 部署

```bash
# Apply all manifests
kubectl apply -f deployment/namespace.yaml
kubectl apply -f deployment/pv-model-cache.yaml
kubectl apply -f deployment/deployment.yaml

# Or for Ray Serve
kubectl apply -f deployment/ray_serve/deployment.yaml
kubectl apply -f deployment/ray_serve/service.yaml

# Deploy observability stack
kubectl apply -k deployment/observability/
```

### 4. Verify / 验证

```bash
kubectl get pods -n ai-serving
kubectl get hpa -n ai-serving

# Test the endpoint
curl http://$(tailscale ip -4)/health
```
