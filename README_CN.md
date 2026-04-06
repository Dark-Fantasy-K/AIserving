# AI Serving Lab

在 AWS EC2 上运行 k3s（轻量 Kubernetes）的 AI 推理服务。支持 Flask 和 Ray Serve 两种后端、HPA 自动扩缩容，并以 **Cilium** 作为默认 CNI，通过 **Hubble** 实现 L7 HTTP 推理延迟的实时网络可观测性。

> **Language / 语言**: [English](README.md) | [中文](README_CN.md)

---

## 架构

```
Client
   │
   ▼  NodePort :30800 (Flask) / :30801 (Ray)
┌──────────────────────────────────────────────────┐
│  AWS EC2                                         │
│                                                  │
│  k3s (Kubernetes)                                │
│  ┌────────────────────────────────────────────┐  │
│  │  CNI: Cilium  ←→  Hubble（L7 流量观测）   │  │
│  │                                            │  │
│  │  Namespace: ai-serving                     │  │
│  │  ┌─────────────────┐  ┌─────────────────┐  │  │
│  │  │  Flask + Gunicorn│  │  Ray Serve      │  │  │
│  │  │  :30800          │  │  :30801         │  │  │
│  │  │  (ai-serving)    │  │  (ai-serving-ray│  │  │
│  │  └─────────────────┘  └─────────────────┘  │  │
│  │  PVC: model-cache (3Gi)                    │  │
│  └────────────────────────────────────────────┘  │
│                                                  │
│  Namespace: kube-system                          │
│  ┌────────────────────────────────────────────┐  │
│  │  cilium-agent  hubble-relay  hubble-ui      │  │
│  │  Hubble UI NodePort :30880                  │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

---

## 项目结构

```
AIserving/
├── deploy.sh                        # 一键部署脚本（含 Cilium + Hubble）
├── deployment/
│   ├── namespace.yaml               # ai-serving 命名空间
│   ├── deployment.yaml              # Flask Deployment（replicas: 1）
│   ├── service.yaml                 # Flask NodePort :30800
│   ├── hpa.yaml                     # HPA（CPU 70% / Memory 80%）
│   ├── pv-model-cache.yaml          # 模型缓存 PersistentVolume (3Gi)
│   ├── cilium-visibility.yaml       # Cilium L7 HTTP 可见性策略
│   ├── flask_ai/                    # 后端 A：Flask + Gunicorn
│   │   ├── flask_app.py
│   │   ├── Dockerfile
│   │   └── requirements-k8s.txt
│   └── ray_serve/                   # 后端 B：Ray Serve
│       ├── ray_app.py
│       ├── Dockerfile
│       ├── deployment.yaml          # Ray Deployment（含 /dev/shm 卷）
│       ├── service.yaml             # Ray NodePort :30801
│       └── requirements-k8s.txt
├── load_test.py                     # 压力测试 & 自动扩缩容验证
└── requirements.txt                 # 本地开发依赖
```

---

## 快速开始

### 一键部署

```bash
git clone https://github.com/Dark-Fantasy-K/AIserving
cd AIserving
chmod +x deploy.sh
./deploy.sh
```

脚本会自动完成以下 8 个步骤：
1. 安装 k3s（`--flannel-backend=none --disable-network-policy`）
2. 安装 Docker
3. 安装 Cilium CLI + Hubble CLI，部署 Cilium CNI + Hubble
4. 构建 Flask / Ray Serve Docker 镜像
5. 将镜像导入 k3s containerd
6. 部署 ai-serving k8s 资源
7. 应用 Cilium L7 可见性策略 + 暴露 Hubble UI
8. 验证服务 + 检查 Hubble 流量捕获

### 手动部署

#### 1. 安装 k3s（必须禁用 flannel）

```bash
# ⚠️ 必须加这三个参数，否则 Cilium 无法接管 CNI
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \
  --flannel-backend=none \
  --disable-network-policy \
  --disable=traefik \
  --write-kubeconfig-mode=644" sh -
```

#### 2. 安装 Cilium + Hubble

```bash
# 安装 cilium CLI
CILIUM_CLI_VERSION=$(curl -s https://raw.githubusercontent.com/cilium/cilium-cli/main/stable.txt)
curl -sL "https://github.com/cilium/cilium-cli/releases/download/${CILIUM_CLI_VERSION}/cilium-linux-amd64.tar.gz" \
  | sudo tar xz -C /usr/local/bin

# 部署 Cilium（含 Hubble relay + UI + HTTP 延迟指标）
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
cilium install \
  --version v1.16.5 \
  --set k8sServiceHost=${NODE_IP} \
  --set k8sServicePort=6443 \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true \
  --set hubble.metrics.enableOpenMetrics=true \
  --set "hubble.metrics.enabled={dns,drop,tcp,flow,httpV2:exemplars=true;labelsContext=source_namespace\,destination_namespace\,destination_workload\,traffic_direction}"

# 等待就绪
cilium status --wait
```

#### 3. 构建镜像 & 导入 k3s

```bash
# 构建（k3s 不用 Docker daemon，必须手动 import）
sudo docker build -t ai-serving:latest deployment/flask_ai/
sudo docker build -t ai-serving-ray:latest deployment/ray_serve/

# 导入到 k3s containerd
sudo docker save ai-serving:latest     | sudo k3s ctr images import -
sudo docker save ai-serving-ray:latest | sudo k3s ctr images import -
```

#### 4. 部署 k8s 资源

```bash
kubectl apply -f deployment/namespace.yaml
kubectl apply -f deployment/pv-model-cache.yaml
kubectl apply -f deployment/deployment.yaml
kubectl apply -f deployment/service.yaml
kubectl apply -f deployment/ray_serve/deployment.yaml
kubectl apply -f deployment/ray_serve/service.yaml
```

#### 5. 配置 Hubble L7 可见性

```bash
# 应用 L7 NetworkPolicy（告诉 Cilium 代理哪些 HTTP 路径）
kubectl apply -f deployment/cilium-visibility.yaml

# 给 Pod 打代理注解（强制流量通过 Envoy sidecar）
kubectl patch deployment ai-serving -n ai-serving --type=json -p='[
  {"op":"add","path":"/spec/template/metadata/annotations/io.cilium.proxy-visibility",
   "value":"<Ingress/8000/TCP/HTTP>"}
]'
kubectl patch deployment ai-serving-ray -n ai-serving --type=json -p='[
  {"op":"add","path":"/spec/template/metadata/annotations/io.cilium.proxy-visibility",
   "value":"<Ingress/8000/TCP/HTTP>"}
]'

# 暴露 Hubble UI 为 NodePort
kubectl patch svc hubble-ui -n kube-system \
  -p '{"spec":{"type":"NodePort","ports":[{"port":80,"targetPort":8081,"nodePort":30880}]}}'

# 启动 Hubble relay port-forward（hubble CLI 需要）
kubectl port-forward -n kube-system svc/hubble-relay 4245:80 &
```

---

## 服务端口

| 服务 | NodePort | 用途 |
|------|----------|------|
| Flask /predict | **30800** | Flask + Gunicorn 推理 |
| Ray /predict   | **30801** | Ray Serve 推理 |
| Ray Dashboard  | **30265** | Ray 内部监控 |
| Hubble UI      | **30880** | 网络流量可视化 |

---

## API 接口

### `POST /predict`

```bash
# Flask
curl -X POST http://localhost:30800/predict \
  -H "Content-Type: application/json" \
  -d '{"text": "This movie was absolutely fantastic!"}'

# Ray Serve
curl -X POST http://localhost:30801/predict \
  -H "Content-Type: application/json" \
  -d '{"text": "This movie was absolutely fantastic!"}'
```

响应：
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
curl http://localhost:30800/health
```

```json
{ "status": "healthy", "memory_used_pct": 42.1, "cpu_pct": 12.3 }
```

---

## Hubble 推理延迟观测

Hubble 通过 Cilium 的 Envoy 代理在**内核网络层**捕获 HTTP 流量，无需修改应用代码。

### 实时流量观测

```bash
# 安装 hubble CLI（如未安装）
HUBBLE_VERSION=$(curl -s https://raw.githubusercontent.com/cilium/hubble/master/stable.txt)
curl -sL "https://github.com/cilium/hubble/releases/download/${HUBBLE_VERSION}/hubble-linux-amd64.tar.gz" \
  | sudo tar xz -C /usr/local/bin

# 启动 relay port-forward
kubectl port-forward -n kube-system svc/hubble-relay 4245:80 &

# 观测 ai-serving 的所有 HTTP 流量（含延迟）
hubble observe --server localhost:4245 \
  --namespace ai-serving \
  --protocol http \
  -f
```

输出示例：
```
22:11:59  host → ai-serving/ai-serving-994c97549:8000  http-request  POST /predict
22:11:59  host ← ai-serving/ai-serving-994c97549:8000  http-response 200  46ms
22:11:59  host → ai-serving/ai-serving-ray-5568596c97:8000  http-request  POST /predict
22:11:59  host ← ai-serving/ai-serving-ray-5568596c97:8000  http-response 200  53ms
```

### 延迟统计脚本

```bash
# 发送测试流量
for i in {1..20}; do
  curl -s -X POST http://localhost:30800/predict \
    -H "Content-Type: application/json" \
    -d "{\"text\": \"test $i\"}" > /dev/null
done

# 从 Hubble 提取延迟数据
hubble observe --server localhost:4245 \
  --namespace ai-serving --protocol http \
  --last 100 -o json \
| python3 -c "
import sys, json, statistics
flask_ms, ray_ms = [], []
for line in sys.stdin:
    try:
        flow = json.loads(line)['flow']
        l7 = flow.get('l7', {})
        if l7.get('type') != 'RESPONSE': continue
        url = l7.get('http', {}).get('url', '')
        if 'predict' not in url: continue
        ms = int(l7.get('latency_ns', 0)) / 1e6
        if ms == 0: continue
        pod = flow.get('destination', {}).get('pod_name', '')
        (ray_ms if 'ray' in pod else flask_ms).append(ms)
    except: pass
for name, data in [('Flask', flask_ms), ('Ray  ', ray_ms)]:
    if data:
        s = sorted(data)
        print(f'{name}  n={len(data)}  min={min(data):.0f}ms  p50={statistics.median(data):.0f}ms  p95={s[int(len(s)*0.95)]:.0f}ms  max={max(data):.0f}ms')
"
```

### Hubble UI

访问 `http://<EC2-IP>:30880`（需开放安全组 30880 端口）：
- Service Map — 可视化服务间流量拓扑
- Flows — 实时 HTTP 请求 / 响应流
- 延迟、状态码、吞吐量展示

---

## 两种 Serving 后端

### 方案 A：Flask AI（`deployment/flask_ai/`）

- Flask + Gunicorn（2 worker）
- `prometheus-flask-exporter` 暴露 `/metrics`
- OpenTelemetry → Jaeger 链路追踪
- 内存占用低，适合 t2.micro / t3.micro

### 方案 B：Ray Serve（`deployment/ray_serve/`）

- Ray Serve 原生自动扩缩容（基于请求队列，min=1 max=3）
- 内置 Ray 指标 + Prometheus metrics 端口 9999
- OpenTelemetry → Jaeger 链路追踪
- Ray Dashboard：NodePort 30265
- **注意**：需要 `/dev/shm` ≥ 2.5Gi（deployment.yaml 已配置 `emptyDir: medium: Memory`）

---

## 模型

**`distilbert-base-uncased-finetuned-sst-2-english`**（情感分析 POSITIVE / NEGATIVE）

- 首次启动时自动下载，约 250MB
- 缓存在 PersistentVolume（`/app/model-cache`），重启后无需重新下载
- 仅使用 CPU，适合无 GPU 实例

---

## 自动扩缩容（HPA）

| 参数 | 值 |
|------|----|
| minReplicas | 1 |
| maxReplicas | 3 |
| CPU 触发阈值 | 70% |
| Memory 触发阈值 | 80% |
| 扩容稳定窗口 | 30s |
| 缩容稳定窗口 | 120s |

```bash
kubectl apply -f deployment/hpa.yaml
kubectl get hpa -n ai-serving -w
```

---

## 压力测试

```bash
python load_test.py http://localhost:30800
```

测试阶段：
1. 预热（1 并发，3 请求）
2. 中等负载（3 并发，10 请求）
3. 高负载（8 并发，20 请求）→ 触发 HPA 扩容
4. 冷却 30s → 观察缩容
5. 验证缩容

---

## Cilium 关键配置说明

### 为什么 k3s 必须加 `--flannel-backend=none`

k3s 默认使用 flannel 作为 CNI。Cilium 需要完全接管 CNI 层才能启用 eBPF 数据面和 Hubble L7 可见性。两者不能共存。

### L7 可见性需要两项配置

| 配置 | 作用 | 文件 |
|------|------|------|
| `CiliumNetworkPolicy`（L7 规则） | 声明哪些 HTTP 路径需代理 | `cilium-visibility.yaml` |
| Pod 注解 `io.cilium.proxy-visibility` | 强制端口流量通过 Envoy 代理 | `kubectl patch` |

缺少任意一项都会导致 Hubble 只能看到 L4（TCP）流量，看不到 HTTP 方法/路径/延迟。

### Ray Serve `/dev/shm` 要求

Ray 对象存储需要 ≥ 30% 可用 RAM 的共享内存。EC2 默认 `/dev/shm` 仅 64MB，必须挂载 `emptyDir: medium: Memory`（已在 `ray_serve/deployment.yaml` 中配置）。
