# AI Serving Lab

A multi-platform AI inference service running on Kubernetes. Supports Flask and Ray Serve backends, **KEDA** event-driven autoscaling, and uses **Cilium** as the CNI with **Hubble** for real-time L7 HTTP observability.

Supports two deployment targets:
- **k3s** — single-node, local or AWS EC2
- **GKE** — Google Kubernetes Engine (multi-node, production-grade)

> **Language / 语言**: [English](README.md) | [中文](README_CN.md)

---

## Architecture

```
                        ┌─────────────────────────────────────────┐
  port-forward only     │  Kubernetes Cluster (k3s or GKE)        │
  (no public exposure)  │                                         │
                        │  CNI: Cilium  ←→  Hubble (L7 observe)  │
  localhost:18000 ──────┼──► ai-serving   (Flask + Gunicorn)      │
  localhost:18001 ──────┼──► ai-serving-ray (Ray Serve)           │
  localhost:8265  ──────┼──► Ray Dashboard                        │
  localhost:8080  ──────┼──► Hubble UI                            │
                        │                                         │
                        │  KEDA ScaledObjects (CPU / Memory /     │
                        │  Cron triggers) → HPA managed by KEDA   │
                        │                                         │
                        │  Storage:                               │
                        │    k3s  → hostPath PV (3Gi)             │
                        │    GKE  → GCE Persistent Disk PVC (10Gi)│
                        └─────────────────────────────────────────┘
```

---

## Project Structure

```
AIserving/
├── deploy.sh                          # One-command deploy (k3s or GKE)
├── loadtest.py                        # Load test & KEDA scaling observation
├── deployment/
│   ├── namespace.yaml                 # ai-serving namespace
│   ├── deployment.yaml                # Flask Deployment (k3s, imagePullPolicy: Never)
│   ├── service.yaml                   # Flask ClusterIP :8000
│   ├── hpa.yaml                       # Legacy HPA (replaced by KEDA)
│   ├── pv-model-cache.yaml            # k3s hostPath PV (3Gi)
│   ├── keda-scaledobjects.yaml        # KEDA ScaledObjects (Flask + Ray)
│   ├── cilium-visibility.yaml         # Cilium L7 HTTP visibility policy
│   ├── gke/                           # GKE-specific overlays
│   │   ├── deployment.yaml            # Flask (imagePullPolicy: IfNotPresent,
│   │   │                              #   strategy: Recreate, imagePullSecrets)
│   │   └── pvc-model-cache.yaml       # GCE PD dynamic PVC (10Gi, standard-rwo)
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

## Quick Start

### k3s (single-node / AWS EC2)

```bash
git clone https://github.com/Dark-Fantasy-K/AIserving
cd AIserving
chmod +x deploy.sh
./deploy.sh --platform k3s
```

### GKE (Google Kubernetes Engine)

```bash
./deploy.sh \
  --platform    gke \
  --gke-project <GCP_PROJECT_ID> \
  --gke-cluster <CLUSTER_NAME> \
  --gke-region  <ZONE_OR_REGION>   # e.g. europe-west3-a
```

### deploy.sh Parameters

| Flag | Description | Default |
|------|-------------|---------|
| `--platform k3s\|gke` | Target platform | `k3s` |
| `--gke-project` | GCP Project ID *(GKE required)* | — |
| `--gke-cluster` | GKE cluster name *(GKE required)* | — |
| `--gke-region` | GKE zone or region | `us-central1` |
| `--registry` | Container registry URL | `gcr.io/<project>` |
| `--cilium-version` | Cilium version | `v1.16.5` |
| `--skip-build` | Skip image build & push | false |
| `--skip-cilium` | Skip Cilium installation | false |

```bash
# Already have images pushed — only redeploy k8s resources
./deploy.sh --platform gke \
  --gke-project my-project --gke-cluster my-cluster \
  --gke-region europe-west3-a \
  --skip-build
```

The script runs 8 steps automatically:

| Step | k3s | GKE |
|------|-----|-----|
| 1 | Install k3s (flannel disabled) | `gcloud container clusters get-credentials` |
| 2 | Install Docker | Install Docker |
| 3 | Install Cilium CLI + Hubble CLI, deploy Cilium | Same (no `k8sServiceHost`) |
| 4 | Build images | Build + `docker push` to GCR |
| 5 | Import into k3s containerd | Already pushed in Step 4 |
| 6 | Apply manifests (hostPath PV) | Apply manifests (GCE PD PVC, Recreate strategy) |
| 7 | Hubble L7 policy + NodePort expose | Hubble L7 policy + LoadBalancer → then ClusterIP |
| 8 | Health check + verify | Health check via port-forward |

---

## Storage

### k3s — hostPath PersistentVolume

```yaml
# deployment/pv-model-cache.yaml
storageClassName: manual
hostPath:
  path: /var/lib/ai-serving/model-cache
capacity:
  storage: 3Gi
```

### GKE — Dynamic GCE Persistent Disk

```yaml
# deployment/gke/pvc-model-cache.yaml
storageClassName: standard-rwo   # GCE PD HDD (default)
accessModes: [ReadWriteOnce]
resources:
  requests:
    storage: 10Gi
```

GKE provisions the underlying Persistent Disk automatically — no manual PV needed.

Available StorageClasses on GKE:

| StorageClass | Type | Use case |
|---|---|---|
| `standard-rwo` *(default)* | GCE PD HDD | Model cache (large, low cost) |
| `premium-rwo` | GCE PD SSD | High IOPS inference workloads |

> **Note**: `ReadWriteOnce` PVCs can only attach to one node at a time.
> The GKE Deployment uses `strategy: Recreate` to avoid `Multi-Attach` errors during rolling updates.

---

## Security — All Services ClusterIP

All services are exposed as **ClusterIP only** (no public LoadBalancer or NodePort).
Access everything through `kubectl port-forward`:

```bash
# AI inference APIs
kubectl port-forward svc/ai-serving     18000:8000 -n ai-serving &
kubectl port-forward svc/ai-serving-ray 18001:8000 -n ai-serving &

# Ray Dashboard
kubectl port-forward svc/ai-serving-ray 8265:8265 -n ai-serving &

# Hubble UI
kubectl port-forward svc/hubble-ui 8080:80 -n kube-system &
```

GKE-specific: GCR image pull requires an `imagePullSecret` (created by deploy.sh):

```bash
# Manually refresh if token expires
kubectl create secret docker-registry gcr-pull-secret \
  --docker-server=gcr.io \
  --docker-username=oauth2accesstoken \
  --docker-password="$(gcloud auth print-access-token)" \
  --docker-email="$(gcloud config get-value account)" \
  -n ai-serving --dry-run=client -o yaml | kubectl apply -f -
```

---

## API Endpoints

All examples below use port-forward addresses.

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

## Autoscaling — KEDA

KEDA replaces the legacy HPA with event-driven scaling. It manages its own HPA objects internally.

### Flask (`ai-serving`)

| Trigger | Threshold | Replicas |
|---------|-----------|----------|
| CPU utilization | > 60% | 1 → 5 |
| Memory utilization | > 75% | 1 → 5 |
| Scale-up window | 30s, max +2 pods/30s | |
| Scale-down window | 180s, max -1 pod/120s | |

### Ray Serve (`ai-serving-ray`)

| Trigger | Threshold | Replicas |
|---------|-----------|----------|
| CPU utilization | > 60% | 1 → 3 |
| Memory utilization | > 70% | 1 → 3 |
| Cron (CST 22:00–08:00) | Night off-peak | lock at 1 |
| Scale-up window | 60s, max +1 pod/60s | |
| Scale-down window | 300s, max -1 pod/180s | |

```bash
# Check ScaledObject status
kubectl get scaledobject -n ai-serving

# Watch KEDA-managed HPA
kubectl get hpa -n ai-serving -w

# Describe triggers
kubectl describe scaledobject ai-serving-scaledobject -n ai-serving
```

---

## Load Testing

`loadtest.py` runs concurrent requests against `/predict` and shows real-time scaling behavior.

```bash
# Default: Flask, 10 concurrency, 180s
python3 loadtest.py

# Ray Serve
python3 loadtest.py --target ray

# Both services simultaneously
python3 loadtest.py --target both --concurrency 10 --duration 120

# High load (attempt to reach maxReplicas)
python3 loadtest.py --concurrency 15 --duration 300
```

| Flag | Default | Notes |
|------|---------|-------|
| `--target flask\|ray\|both` | `flask` | Service to stress |
| `--concurrency` | `10` | Threads; ≤15 recommended for CPU-only inference |
| `--duration` | `180` | Test duration in seconds |
| `--ramp` | `20` | Ramp-up time in seconds |
| `--timeout` | `30` | Per-request timeout in seconds |

Sample output:
```
 时间    RPS    P50ms   P95ms   P99ms      成功    错误  副本数
──────────────────────────────────────────────────────────────
  20s    6.0      450    1200    1800       120       0  flask=1/1→1
  50s    8.2      480    1350    1900       520       0  flask=↑2/2→2
  90s    9.1      510    1400    2000      1020       0  flask=↑3/3→3
```

> The script auto-starts `kubectl port-forward` and tears it down after the test.

---

## Hubble Observability

```bash
# Start relay port-forward
kubectl port-forward -n kube-system svc/hubble-relay 4245:80 &

# Live HTTP flows with latency
hubble observe --server localhost:4245 \
  --namespace ai-serving --protocol http -f

# Hubble UI
kubectl port-forward svc/hubble-ui 8080:80 -n kube-system &
# Open http://localhost:8080
```

---

## Serving Backends

### Flask AI (`deployment/flask_ai/`)

- Flask + Gunicorn (2 workers)
- Prometheus metrics at `/metrics`
- OpenTelemetry traces → Jaeger
- Low memory footprint

### Ray Serve (`deployment/ray_serve/`)

- Ray Serve with CPU request `200m` (reduced from 500m for small nodes)
- Prometheus metrics on port 9999
- OpenTelemetry traces → Jaeger
- Ray Dashboard accessible via port-forward `:8265`
- Requires `/dev/shm` ≥ 2.5Gi — configured via `emptyDir: medium: Memory`

---

## Model

**`distilbert-base-uncased-finetuned-sst-2-english`** — Sentiment analysis (POSITIVE / NEGATIVE)

- Downloaded automatically on first start (~250 MB)
- Cached in PersistentVolume (`/app/model-cache`) — survives pod restarts
- CPU-only inference; no GPU required

---

## Cilium Notes

### Why k3s needs `--flannel-backend=none`

k3s ships with flannel as the default CNI. Cilium must fully own the CNI layer to enable eBPF dataplane and Hubble L7 visibility. They cannot coexist.

### GKE Cilium installation

On GKE, `k8sServiceHost` is not required. The cluster name is set explicitly to avoid Cilium's 32-character name limit (GKE context names like `gke_<project>_<zone>_<cluster>` are too long):

```bash
cilium install --version v1.16.5 \
  --set cluster.name=<cluster-name>   # max 32 chars
  --set hubble.relay.enabled=true \
  ...
```

### L7 visibility requires two configurations

| Configuration | Effect | Location |
|---|---|---|
| `CiliumNetworkPolicy` with L7 rules | Declares which HTTP paths to proxy | `cilium-visibility.yaml` |
| Pod annotation `io.cilium.proxy-visibility` | Forces traffic through Envoy | `kubectl patch` |

Missing either one means Hubble only sees L4 (TCP) flows.
