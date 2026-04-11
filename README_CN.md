# AI Serving Lab

多平台 AI 推理服务，运行在 Kubernetes 上。支持 Flask 和 Ray Serve 两种后端、**KEDA** 事件驱动自动扩缩容，以 **Cilium** 作为 CNI，通过 **Hubble** 实现 L7 HTTP 实时网络可观测性。

支持两种部署目标：
- **k3s** — 单节点，本地或 AWS EC2
- **GKE** — Google Kubernetes Engine（多节点，生产级）

> **Language / 语言**: [English](README.md) | [中文](README_CN.md)

---

## 架构

```
                          ┌──────────────────────────────────────────┐
  仅 port-forward 访问     │  Kubernetes 集群（k3s 或 GKE）            │
  （无公网暴露）           │                                          │
                          │  CNI: Cilium  ←→  Hubble（L7 流量观测）  │
  localhost:18000 ────────┼──► ai-serving（Flask + Gunicorn）         │
  localhost:18001 ────────┼──► ai-serving-ray（Ray Serve）            │
  localhost:8265  ────────┼──► Ray Dashboard                         │
  localhost:8080  ────────┼──► Hubble UI                             │
                          │                                          │
                          │  KEDA ScaledObjects                      │
                          │  （CPU / Memory / Cron 触发器）           │
                          │  → KEDA 托管 HPA                         │
                          │                                          │
                          │  存储：                                   │
                          │    k3s → hostPath PV (3Gi)               │
                          │    GKE → GCE Persistent Disk PVC (10Gi)  │
                          └──────────────────────────────────────────┘
```

---

## 项目结构

```
AIserving/
├── deploy.sh                          # 一键部署脚本（k3s 或 GKE）
├── loadtest.py                        # 压测脚本 & KEDA 扩容观测
├── deployment/
│   ├── namespace.yaml                 # ai-serving 命名空间
│   ├── deployment.yaml                # Flask Deployment（k3s，imagePullPolicy: Never）
│   ├── service.yaml                   # Flask ClusterIP :8000
│   ├── hpa.yaml                       # 旧版 HPA（已被 KEDA 替代）
│   ├── pv-model-cache.yaml            # k3s hostPath PV (3Gi)
│   ├── keda-scaledobjects.yaml        # KEDA ScaledObjects（Flask + Ray）
│   ├── cilium-visibility.yaml         # Cilium L7 HTTP 可见性策略
│   ├── gke/                           # GKE 专用覆盖配置
│   │   ├── deployment.yaml            # Flask（imagePullPolicy: IfNotPresent，
│   │   │                              #   strategy: Recreate，imagePullSecrets）
│   │   └── pvc-model-cache.yaml       # GCE PD 动态 PVC (10Gi, standard-rwo)
│   ├── flask_ai/
│   │   ├── flask_app.py
│   │   ├── Dockerfile
│   │   └── requirements-k8s.txt
│   └── ray_serve/
│       ├── ray_app.py
│       ├── Dockerfile
│       ├── deployment.yaml
│       ├── service.yaml
│       └── requirements-k8s.txt
└── requirements.txt
```

---

## 快速开始

### k3s（单节点 / AWS EC2）

```bash
git clone https://github.com/Dark-Fantasy-K/AIserving
cd AIserving
chmod +x deploy.sh
./deploy.sh --platform k3s
```

### GKE（Google Kubernetes Engine）

```bash
./deploy.sh \
  --platform    gke \
  --gke-project <GCP_PROJECT_ID> \
  --gke-cluster <CLUSTER_NAME> \
  --gke-region  <ZONE_OR_REGION>   # 例如 europe-west3-a
```

### deploy.sh 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--platform k3s\|gke` | 目标平台 | `k3s` |
| `--gke-project` | GCP 项目 ID（GKE 必填） | — |
| `--gke-cluster` | GKE 集群名称（GKE 必填） | — |
| `--gke-region` | GKE zone 或 region | `us-central1` |
| `--registry` | 容器镜像仓库地址 | `gcr.io/<project>` |
| `--cilium-version` | Cilium 版本 | `v1.16.5` |
| `--skip-build` | 跳过镜像构建与推送 | false |
| `--skip-cilium` | 跳过 Cilium 安装 | false |

```bash
# 镜像已推送，仅重新部署 k8s 资源
./deploy.sh --platform gke \
  --gke-project my-project --gke-cluster my-cluster \
  --gke-region europe-west3-a \
  --skip-build
```

脚本自动执行 8 个步骤：

| 步骤 | k3s | GKE |
|------|-----|-----|
| 1 | 安装 k3s（禁用 flannel） | `gcloud container clusters get-credentials` |
| 2 | 安装 Docker | 安装 Docker |
| 3 | 安装 Cilium CLI + Hubble CLI，部署 Cilium | 同左（无需设置 `k8sServiceHost`） |
| 4 | 构建镜像 | 构建 + `docker push` 到 GCR |
| 5 | 导入到 k3s containerd | 步骤 4 已完成推送 |
| 6 | 应用 manifest（hostPath PV） | 应用 manifest���GCE PD PVC，Recreate 策略） |
| 7 | Hubble L7 策略 + NodePort 暴露 | Hubble L7 策略 + ClusterIP |
| 8 | 健康检查 + 验证 | 通过 port-forward 健康检查 |

---

## 存储

### k3s — hostPath PersistentVolume

```yaml
# deployment/pv-model-cache.yaml
storageClassName: manual
hostPath:
  path: /var/lib/ai-serving/model-cache
capacity:
  storage: 3Gi
```

### GKE — 动态 GCE Persistent Disk

```yaml
# deployment/gke/pvc-model-cache.yaml
storageClassName: standard-rwo   # GCE PD HDD（默认）
accessModes: [ReadWriteOnce]
resources:
  requests:
    storage: 10Gi
```

GKE 会自动创建底层 Persistent Disk，无需手动创建 PV。

GKE 可用 StorageClass：

| StorageClass | 类型 | 适用场景 |
|---|---|---|
| `standard-rwo`（默认） | GCE PD HDD | 模型缓存（容量大、成本低） |
| `premium-rwo` | GCE PD SSD | 高 IOPS 推理场景 |

> **注意**：`ReadWriteOnce` PVC 同时只能挂载到一个节点。
> GKE Deployment 使用 `strategy: Recreate`，确保旧 Pod 先删除再创建新 Pod，避免 `Multi-Attach` 错误。

---

## 安全 — 全部 ClusterIP，无公网暴露

所有服务均为 **ClusterIP**（无 LoadBalancer 或 NodePort 公网入口）。
所有访问通过 `kubectl port-forward` 进行：

```bash
# AI 推理接口
kubectl port-forward svc/ai-serving     18000:8000 -n ai-serving &
kubectl port-forward svc/ai-serving-ray 18001:8000 -n ai-serving &

# Ray Dashboard
kubectl port-forward svc/ai-serving-ray 8265:8265 -n ai-serving &

# Hubble UI
kubectl port-forward svc/hubble-ui 8080:80 -n kube-system &
```

GKE 拉取 GCR 镜像需要 `imagePullSecret`（由 deploy.sh 自动创建）：

```bash
# Token 过期时手动刷新
kubectl create secret docker-registry gcr-pull-secret \
  --docker-server=gcr.io \
  --docker-username=oauth2accesstoken \
  --docker-password="$(gcloud auth print-access-token)" \
  --docker-email="$(gcloud config get-value account)" \
  -n ai-serving --dry-run=client -o yaml | kubectl apply -f -
```

---

## API 接口

以下示例均使用 port-forward 地址。

### `POST /predict`

```bash
curl -X POST http://localhost:18000/predict \
  -H "Content-Type: application/json" \
  -d '{"text": "This movie was absolutely fantastic!"}'
```

```json
{
  "text": "This movie was absolutely fantastic!",
  "label": "POSITIVE",
  "score": 0.9998,
  "latency_ms": 46.3
}
```

### `GET /health`

```bash
curl http://localhost:18000/health
```

```json
{ "status": "healthy", "memory_used_pct": 42.1, "cpu_pct": 12.3 }
```

---

## 自动扩缩容 — KEDA

KEDA 替代原有 HPA，实现事件驱动自动扩缩容，内部托管 HPA 对象。

### Flask（`ai-serving`）

| 触发器 | 阈值 | 副本范围 |
|--------|------|----------|
| CPU 利用率 | > 60% | 1 → 5 |
| Memory 利用率 | > 75% | 1 → 5 |
| 扩容稳定窗口 | 30s，每 30s 最多 +2 Pod | |
| 缩容稳定窗口 | 180s，每 120s 最多 -1 Pod | |

### Ray Serve（`ai-serving-ray`）

| 触发器 | 阈值 | 副本范围 |
|--------|------|----------|
| CPU 利用率 | > 60% | 1 → 3 |
| Memory 利用率 | > 70% | 1 → 3 |
| Cron（北京时间 22:00–08:00） | 夜间低峰期 | 锁定为 1 |
| 扩容稳定窗口 | 60s，每 60s 最多 +1 Pod | |
| 缩容稳定窗口 | 300s，每 180s 最多 -1 Pod | |

```bash
# 查看 ScaledObject 状态
kubectl get scaledobject -n ai-serving

# 观察 KEDA 托管 HPA
kubectl get hpa -n ai-serving -w

# 查看触发器详情
kubectl describe scaledobject ai-serving-scaledobject -n ai-serving
```

---

## 压力测试

`loadtest.py` 对 `/predict` 发起并发请求，实时展示 KEDA 扩容过程。

```bash
# 默认：压测 Flask，10 并发，持续 180s
python3 loadtest.py

# 压测 Ray Serve
python3 loadtest.py --target ray

# 同时压测两个服务
python3 loadtest.py --target both --concurrency 10 --duration 120

# 高压测试（尝试触发最大副本数）
python3 loadtest.py --concurrency 15 --duration 300
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--target flask\|ray\|both` | `flask` | 目标服务 |
| `--concurrency` | `10` | 并发线程数；CPU 推理建议 ≤15 |
| `--duration` | `180` | 压测时长（秒） |
| `--ramp` | `20` | 爬坡时间（秒） |
| `--timeout` | `30` | 单次请求超时（秒） |

输出示例：
```
 时间    RPS    P50ms   P95ms   P99ms      成功    错误  副本数
──────────────────────────────────────────────────────────────
  20s    6.0      450    1200    1800       120       0  flask=1/1→1
  50s    8.2      480    1350    1900       520       0  flask=↑2/2→2
  90s    9.1      510    1400    2000      1020       0  flask=↑3/3→3
```

> 脚本会自动启动 `kubectl port-forward`，测试结束后自���关闭。

---

## Hubble 可观测性

```bash
# 启动 relay port-forward
kubectl port-forward -n kube-system svc/hubble-relay 4245:80 &

# 实时观测含延迟的 HTTP 流量
hubble observe --server localhost:4245 \
  --namespace ai-serving --protocol http -f

# Hubble UI
kubectl port-forward svc/hubble-ui 8080:80 -n kube-system &
# 浏览器访问 http://localhost:8080
```

---

## 两种 Serving 后端

### 方案 A：Flask AI（`deployment/flask_ai/`）

- Flask + Gunicorn（2 worker）
- Prometheus metrics 暴露在 `/metrics`
- OpenTelemetry → Jaeger 链路追踪
- 内存占用低

### 方案 B：Ray Serve（`deployment/ray_serve/`）

- CPU request 调整为 `200m`（原 500m，适配小节点）
- Prometheus metrics 端口 9999
- OpenTelemetry → Jaeger 链路追踪
- Ray Dashboard 通过 port-forward `:8265` 访问
- 需要 `/dev/shm` ≥ 2.5Gi（已通过 `emptyDir: medium: Memory` 配置）

---

## 模型

**`distilbert-base-uncased-finetuned-sst-2-english`**（情感分析：POSITIVE / NEGATIVE）

- 首次启动时自动下载，约 250MB
- 缓存在 PersistentVolume（`/app/model-cache`），重启后无需重新下载
- 仅使用 CPU，无需 GPU

---

## Cilium 关键配置说明

### 为什么 k3s 必须加 `--flannel-backend=none`

k3s 默认使用 flannel 作为 CNI。Cilium 需要完全接管 CNI 层才能启用 eBPF 数据面和 Hubble L7 可见性，两者不能共存。

### GKE 上安装 Cilium

GKE 模式无需设置 `k8sServiceHost`，但需显式指定 `cluster.name`（≤32 字符），避免 GKE context 名称过长报错：

```bash
# GKE context 格式为 gke_<project>_<zone>_<cluster>，通常超过 32 字符
cilium install --version v1.16.5 \
  --set cluster.name=<cluster-name> \   # 最长 32 字符
  --set hubble.relay.enabled=true \
  ...
```

### L7 可见性需要两项配置

| 配置 | 作用 | 文件位置 |
|------|------|----------|
| `CiliumNetworkPolicy`（L7 规则） | 声明哪些 HTTP 路径需代理 | `cilium-visibility.yaml` |
| Pod 注解 `io.cilium.proxy-visibility` | 强制端口流量通过 Envoy 代理 | `kubectl patch` |

缺少任意一项，Hubble 只能看到 L4（TCP）流量，无法显示 HTTP 方法/路径/延迟。
