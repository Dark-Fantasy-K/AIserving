# AI Serving Lab

A lightweight AI inference service running on k3s (Kubernetes) on AWS EC2. Supports Flask and Ray Serve backends, HPA autoscaling, and uses **Cilium** as the default CNI with **Hubble** for real-time L7 HTTP inference latency observability.

> **Language / 语言**: [English](README.md) | [中文](README_CN.md)

---

## Architecture

```
Client
   │
   ▼  NodePort :30800 (Flask) / :30801 (Ray)
┌──────────────────────────────────────────────────┐
│  AWS EC2                                         │
│                                                  │
│  k3s (Kubernetes)                                │
│  ┌────────────────────────────────────────────┐  │
│  │  CNI: Cilium  ←→  Hubble (L7 observability)│  │
│  │                                            │  │
│  │  Namespace: ai-serving                     │  │
│  │  ┌─────────────────┐  ┌─────────────────┐  │  │
│  │  │  Flask + Gunicorn│  │  Ray Serve      │  │  │
│  │  │  :30800          │  │  :30801         │  │  │
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

## Project Structure

```
AIserving/
├── deploy.sh                        # One-command deploy (Cilium + Hubble included)
├── deployment/
│   ├── namespace.yaml               # ai-serving namespace
│   ├── deployment.yaml              # Flask Deployment (replicas: 1)
│   ├── service.yaml                 # Flask NodePort :30800
│   ├── hpa.yaml                     # HPA (CPU 70% / Memory 80%)
│   ├── pv-model-cache.yaml          # Model cache PersistentVolume (3Gi)
│   ├── cilium-visibility.yaml       # Cilium L7 HTTP visibility policy
│   ├── flask_ai/                    # Backend A: Flask + Gunicorn
│   │   ├── flask_app.py
│   │   ├── Dockerfile
│   │   └── requirements-k8s.txt
│   └── ray_serve/                   # Backend B: Ray Serve
│       ├── ray_app.py
│       ├── Dockerfile
│       ├── deployment.yaml          # Ray Deployment (with /dev/shm volume)
│       ├── service.yaml             # Ray NodePort :30801
│       └── requirements-k8s.txt
├── load_test.py                     # Load test & autoscaling validation
└── requirements.txt                 # Local dev dependencies
```

---

## Quick Start

### One-command deploy

```bash
git clone https://github.com/Dark-Fantasy-K/AIserving
cd AIserving
chmod +x deploy.sh
./deploy.sh
```

The script runs 8 steps automatically:
1. Install k3s with `--flannel-backend=none --disable-network-policy`
2. Install Docker
3. Install Cilium CLI + Hubble CLI, deploy Cilium CNI + Hubble
4. Build Flask and Ray Serve Docker images
5. Import images into k3s containerd
6. Apply all k8s manifests
7. Apply Cilium L7 visibility policy + expose Hubble UI
8. Verify services + confirm Hubble flow capture

### Manual deploy

#### 1. Install k3s (flannel must be disabled)

```bash
# ⚠️ These three flags are required for Cilium CNI to work
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \
  --flannel-backend=none \
  --disable-network-policy \
  --disable=traefik \
  --write-kubeconfig-mode=644" sh -
```

#### 2. Install Cilium + Hubble

```bash
# Install cilium CLI
CILIUM_CLI_VERSION=$(curl -s https://raw.githubusercontent.com/cilium/cilium-cli/main/stable.txt)
curl -sL "https://github.com/cilium/cilium-cli/releases/download/${CILIUM_CLI_VERSION}/cilium-linux-amd64.tar.gz" \
  | sudo tar xz -C /usr/local/bin

# Deploy Cilium with Hubble relay + UI + HTTP latency metrics
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
cilium install \
  --version v1.16.5 \
  --set k8sServiceHost=${NODE_IP} \
  --set k8sServicePort=6443 \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true \
  --set hubble.metrics.enableOpenMetrics=true \
  --set "hubble.metrics.enabled={dns,drop,tcp,flow,httpV2:exemplars=true;labelsContext=source_namespace\,destination_namespace\,destination_workload\,traffic_direction}"

cilium status --wait
```

#### 3. Build images & import into k3s

```bash
# k3s uses its own containerd — Docker images must be imported manually
sudo docker build -t ai-serving:latest deployment/flask_ai/
sudo docker build -t ai-serving-ray:latest deployment/ray_serve/

sudo docker save ai-serving:latest     | sudo k3s ctr images import -
sudo docker save ai-serving-ray:latest | sudo k3s ctr images import -
```

#### 4. Deploy k8s resources

```bash
kubectl apply -f deployment/namespace.yaml
kubectl apply -f deployment/pv-model-cache.yaml
kubectl apply -f deployment/deployment.yaml
kubectl apply -f deployment/service.yaml
kubectl apply -f deployment/ray_serve/deployment.yaml
kubectl apply -f deployment/ray_serve/service.yaml
```

#### 5. Enable Hubble L7 visibility

```bash
# Step A: apply L7 NetworkPolicy (declares which HTTP paths to proxy)
kubectl apply -f deployment/cilium-visibility.yaml

# Step B: annotate pods (forces traffic through Envoy proxy)
# Both steps are required — missing either means Hubble sees only L4 (TCP)
kubectl patch deployment ai-serving -n ai-serving --type=json -p='[
  {"op":"add","path":"/spec/template/metadata/annotations/io.cilium.proxy-visibility",
   "value":"<Ingress/8000/TCP/HTTP>"}
]'
kubectl patch deployment ai-serving-ray -n ai-serving --type=json -p='[
  {"op":"add","path":"/spec/template/metadata/annotations/io.cilium.proxy-visibility",
   "value":"<Ingress/8000/TCP/HTTP>"}
]'

# Expose Hubble UI as NodePort
kubectl patch svc hubble-ui -n kube-system \
  -p '{"spec":{"type":"NodePort","ports":[{"port":80,"targetPort":8081,"nodePort":30880}]}}'

# Start Hubble relay port-forward (required for hubble CLI)
kubectl port-forward -n kube-system svc/hubble-relay 4245:80 &
```

---

## Service Ports

| Service | NodePort | Purpose |
|---------|----------|---------|
| Flask /predict | **30800** | Flask + Gunicorn inference |
| Ray /predict   | **30801** | Ray Serve inference |
| Ray Dashboard  | **30265** | Ray cluster monitoring |
| Hubble UI      | **30880** | Network flow visualization |

---

## API Endpoints

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

Response:
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

## Hubble Inference Latency Observation

Hubble captures HTTP traffic at the **kernel network layer** via Cilium's Envoy proxy — no application changes required.

### Live traffic

```bash
# Install hubble CLI (if not already installed)
HUBBLE_VERSION=$(curl -s https://raw.githubusercontent.com/cilium/hubble/master/stable.txt)
curl -sL "https://github.com/cilium/hubble/releases/download/${HUBBLE_VERSION}/hubble-linux-amd64.tar.gz" \
  | sudo tar xz -C /usr/local/bin

# Start relay port-forward
kubectl port-forward -n kube-system svc/hubble-relay 4245:80 &

# Watch all HTTP flows in ai-serving namespace
hubble observe --server localhost:4245 \
  --namespace ai-serving \
  --protocol http \
  -f
```

Sample output:
```
22:11:59  host → ai-serving/ai-serving-994c97549:8000  http-request  POST /predict
22:11:59  host ← ai-serving/ai-serving-994c97549:8000  http-response 200  46ms
22:11:59  host → ai-serving/ai-serving-ray-5568596c97:8000  http-request  POST /predict
22:11:59  host ← ai-serving/ai-serving-ray-5568596c97:8000  http-response 200  53ms
```

### Latency statistics

```bash
# Generate test traffic
for i in {1..20}; do
  curl -s -X POST http://localhost:30800/predict \
    -H "Content-Type: application/json" \
    -d "{\"text\": \"test $i\"}" > /dev/null
done

# Compute latency stats from Hubble flows
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

Open `http://<EC2-IP>:30880` (open port 30880 in EC2 security group):
- **Service Map** — visualize inter-service traffic topology
- **Flows** — real-time HTTP request/response stream
- Latency, status codes, and throughput per endpoint

---

## Serving Backends

### Option A: Flask AI (`deployment/flask_ai/`)

- Flask + Gunicorn (2 workers)
- Prometheus metrics via `prometheus-flask-exporter` at `/metrics`
- OpenTelemetry traces → Jaeger
- Low memory footprint; suitable for t2.micro / t3.micro

### Option B: Ray Serve (`deployment/ray_serve/`)

- Native Ray Serve autoscaling (queue-based, min=1 max=3)
- Prometheus metrics on port 9999
- OpenTelemetry traces → Jaeger
- Ray Dashboard on NodePort 30265
- **Requires** `/dev/shm` ≥ 2.5Gi — already configured with `emptyDir: medium: Memory` in `ray_serve/deployment.yaml`

---

## Model

**`distilbert-base-uncased-finetuned-sst-2-english`** — Sentiment analysis (POSITIVE / NEGATIVE)

- Downloaded automatically on first start (~250 MB)
- Cached in PersistentVolume (`/app/model-cache`) — survives pod restarts
- CPU-only inference; no GPU required

---

## Autoscaling (HPA)

| Parameter | Value |
|-----------|-------|
| minReplicas | 1 |
| maxReplicas | 3 |
| CPU trigger | 70% avg utilization |
| Memory trigger | 80% avg utilization |
| Scale-up stabilization | 30s |
| Scale-down stabilization | 120s |

```bash
kubectl apply -f deployment/hpa.yaml
kubectl get hpa -n ai-serving -w
```

---

## Load Testing

```bash
python load_test.py http://localhost:30800
```

Stages: warm-up → moderate load → high load (triggers HPA) → cool-down → verify scale-down.

---

## Cilium Configuration Notes

### Why k3s needs `--flannel-backend=none`

k3s ships with flannel as the default CNI. Cilium must fully own the CNI layer to enable eBPF dataplane and Hubble L7 visibility. They cannot coexist.

### L7 visibility requires two things

| Configuration | Effect | Location |
|---------------|--------|----------|
| `CiliumNetworkPolicy` with L7 rules | Tells Cilium which HTTP paths to proxy | `cilium-visibility.yaml` |
| Pod annotation `io.cilium.proxy-visibility` | Forces port traffic through Envoy proxy | `kubectl patch` |

Missing either one means Hubble only sees L4 (TCP) flows — no HTTP method, path, or latency.

### Ray Serve `/dev/shm` requirement

Ray's object store needs ≥ 30% of available RAM as shared memory. EC2 default `/dev/shm` is only 64 MB. The `ray_serve/deployment.yaml` mounts `emptyDir: medium: Memory` at `/dev/shm` to fix this.
