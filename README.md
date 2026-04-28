[English](README.md) | [中文](README.zh.md)
# YOLO Microservices Pipeline

A real-time object detection microservices system based on YOLOv8, using gRPC communication, with support for pedestrian pose estimation and vehicle counting/tracking.

## Architecture Overview

```
[Client]
   │  HTTP POST /predict  (image)
   ▼
[Gateway :5000]          Flask HTTP entry point
   │  gRPC
   ▼
[Router :50051]          YOLOv8s detection → dispatch by class
   ├──────────────┬
   │  gRPC        │  gRPC
   ▼              ▼
[Pedestrian      [Vehicle
 :50052]          :50053]
 YOLOv8s-pose    IoU tracking
 Pose estimation  Vehicle counting
```

| Service | Port | Function |
|---------|------|----------|
| Gateway | 5000 (HTTP) | External REST entry point, forwards images to Router |
| Router | 50051 (gRPC) | YOLOv8s detection, dispatches person/vehicle results |
| Pedestrian | 50052 (gRPC) | YOLOv8s-pose keypoint estimation |
| Vehicle | 50053 (gRPC) | IoU tracking + vehicle-class counting |

## Directory Structure

```
AIserving/
├── server.py                       # Router service entry point
├── Dockerfile.router               # Router Docker image
├── pipeline.proto                  # gRPC protocol definition
├── proto_gen/                      # Generated gRPC stubs (router)
├── requirements.txt                # Global Python dependencies
├── setup.sh                        # One-shot install: deps + proto stubs
├── build.sh                        # Build / deploy script
├── generate.sh                     # Standalone proto generation helper
├── docker-compose.yml              # Docker Compose configuration
├── all-in-one.yaml                 # Kubernetes manifests
├── validate_video.py               # Video dataset validation script
├── yolov8s.pt                      # YOLOv8s weights (router)
├── datasets/                       # Sample video datasets
│   └── person_vehicle.mp4
└── services/
    ├── gateway/
    │   ├── server.py
    │   ├── requirements.txt
    │   ├── Dockerfile
    │   └── proto_gen/
    ├── pedestrian-service/
    │   ├── server.py
    │   ├── requirements.txt
    │   ├── Dockerfile
    │   ├── yolov8s-pose.pt
    │   └── proto_gen/
    └── vehicle-service/
        ├── server.py
        ├── requirements.txt
        ├── Dockerfile
        └── proto_gen/
```

---

## Bare-Metal Full Installation

The following steps apply to a fresh environment (Ubuntu 20.04 / 22.04) with **no pre-installed Python, curl, or Docker**.

### Step 1: System Packages

```bash
sudo apt update && sudo apt install -y \
    python3 python3-pip python3-venv python3-dev \
    build-essential gcc g++ \
    libglib2.0-0 libgl1 libgomp1 \
    git wget
```

> - `python3-venv` — required for creating virtual environments
> - `python3-dev` + `build-essential` — compile C-extension packages such as grpcio
> - `libglib2.0-0` + `libgl1` — OpenCV headless runtime
> - `libgomp1` — PyTorch OpenMP multi-threaded inference

Verify the installation:

```bash
python3 --version    # Expected: 3.9+
pip3 --version
```

### Step 2: Create Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
# python / pip now point to the venv versions
python --version
```

> To deactivate: `deactivate`. To re-enter: `source .venv/bin/activate`

### Step 3: Install Python Dependencies & Generate gRPC Stubs

With the venv activated:

```bash
chmod +x setup.sh
./setup.sh deps     # Install all Python packages
./setup.sh proto    # Generate gRPC stubs and distribute to each service

# Or in one step:
./setup.sh          # Equivalent to deps + proto
```

> `torch` + `ultralytics` is approximately 2 GB. First-time installation may take a while.

### Step 4: (Optional) NVIDIA GPU Driver + CUDA

Skip this step if no GPU is available — the system will automatically fall back to CPU inference.

```bash
# Check for existing driver
nvidia-smi

# If absent, install CUDA Toolkit (example: CUDA 12.1)
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update && sudo apt install -y cuda-toolkit-12-1
echo 'export PATH=/usr/local/cuda/bin:$PATH' >> ~/.bashrc
echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc
```

After installing CUDA, replace torch with the GPU build:

```bash
source .venv/bin/activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```

### Step 5: (Optional) Docker + Docker Compose

Only required for container/Kubernetes deployment.

```bash
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
newgrp docker
docker --version
docker compose version
```

---

## Quick Start

### Local (No Docker)

```bash
./build.sh local
```

Starts 4 processes. Access the API at `http://localhost:5000`.

### Docker Compose

```bash
./build.sh compose
```

Equivalent to `docker compose up --build -d`. Access at `http://localhost:5000`.

### Kubernetes

```bash
REGISTRY=your-registry ./build.sh docker   # Build images
docker push your-registry/pedestrian-service:latest
docker push your-registry/vehicle-service:latest
docker push your-registry/router-service:latest
docker push your-registry/gateway:latest

REGISTRY=your-registry ./build.sh k8s      # Deploy
```

Access via NodePort: `http://<node-ip>:30500`

---

## Dataset & Validation

### Download Traffic Video Dataset

A sample traffic video containing pedestrians, cars, and bicycles is included for pipeline testing.

```bash
mkdir -p datasets
wget "https://github.com/intel-iot-devkit/sample-videos/raw/master/person-bicycle-car-detection.mp4" \
     -O datasets/person_vehicle.mp4
```

| Property | Value |
|----------|-------|
| Source | Intel IoT DevKit open sample videos |
| Resolution | 768 × 432 |
| Frame rate | 12 fps |
| Duration | ~54 s (647 frames) |
| Content | Pedestrians, bicycles, cars in outdoor scenes |

### Validation Script

`validate_video.py` supports two modes:

**Local mode** — runs YOLOv8 directly, no gRPC services required:

```bash
.venv/bin/python validate_video.py \
    --video datasets/person_vehicle.mp4 \
    --local \
    --out datasets/person_vehicle_annotated.mp4
```

**gRPC mode** — sends every frame through the full Router → Pedestrian / Vehicle pipeline:

```bash
.venv/bin/python validate_video.py \
    --video datasets/person_vehicle.mp4 \
    --grpc \
    --addr localhost:50051 \
    --out datasets/person_vehicle_annotated.mp4
```

| Flag | Default | Description |
|------|---------|-------------|
| `--video` | `datasets/person_vehicle.mp4` | Input video path |
| `--local` | — | Run YOLOv8 locally (no services needed) |
| `--grpc` | — | Send frames to Router gRPC service |
| `--addr` | `localhost:50051` | Router gRPC address |
| `--max-frames` | 0 (all) | Limit number of frames to process |
| `--out` | — | Save annotated output video |

### Validation Results (YOLOv8s, CPU, 647 frames)

| Metric | Value |
|--------|-------|
| Persons detected (cumulative) | 248 |
| Vehicles detected (cumulative) | 85 |
| Other detections (bicycle, etc.) | 174 |
| Average inference latency | 76 ms / frame |
| Min / Max latency | 72.7 / 177.7 ms |

---

## API Reference

### `GET /health`

```json
{"status": "ok", "service": "gateway", "router": "localhost:50051"}
```

### `POST /predict`

**Request**: `multipart/form-data`, field name `image`, supports JPEG/PNG.

**Example response**:

```json
{
  "total_latency_ms": 120.5,
  "total_detections": 5,
  "annotated_img": "data:image/jpeg;base64,...",
  "PersonPoseHandler": {
    "task": "pose_estimation",
    "person_count": 2,
    "latency_ms": 85.3,
    "persons": [
      {
        "confidence": 0.9123,
        "bbox": [120.0, 45.0, 320.0, 480.0],
        "keypoints": {
          "nose": {"x": 220.0, "y": 80.0, "confidence": 0.98},
          "left_shoulder": {"x": 160.0, "y": 180.0, "confidence": 0.95}
        }
      }
    ]
  },
  "VehicleCountHandler": {
    "task": "vehicle_counting",
    "current_total": 3,
    "active_tracks": 3,
    "latency_ms": 12.1,
    "vehicles": [
      {"class": "car", "confidence": 0.88, "bbox": [...], "track_id": 1}
    ],
    "current_counts": {"car": 2, "truck": 1},
    "cumulative": {"car": 5, "truck": 2}
  },
  "unhandled": []
}
```

**curl example**:

```bash
curl -X POST http://localhost:5000/predict \
     -F "image=@/path/to/image.jpg" | python3 -m json.tool
```

---

## Dependencies

### System Packages

| Package | Purpose |
|---------|---------|
| `python3-dev` / `build-essential` | Compile C-extension packages such as grpcio |
| `libglib2.0-0` | OpenCV runtime |
| `libgl1-mesa-glx` | OpenCV GUI dependency (also needed in headless mode) |
| `libgomp1` | PyTorch OpenMP multi-threaded inference |

### Python Packages

| Package | Purpose |
|---------|---------|
| `grpcio` / `grpcio-tools` | gRPC inter-service communication |
| `protobuf` | Protocol buffer serialization |
| `flask` | Gateway HTTP server |
| `ultralytics` | YOLOv8 object detection / pose estimation |
| `torch` / `torchvision` | Deep learning inference backend |
| `opencv-python-headless` | Image annotation rendering |
| `pillow` / `numpy` | Image processing utilities |

---

## Regenerating gRPC Stubs

After modifying `pipeline.proto`, run:

```bash
./setup.sh proto
```

Stubs are automatically generated and copied to the `proto_gen/` directory of each service.

---

## Common Commands

```bash
# Tail logs for all services
docker compose logs -f

# Tail logs for a specific service
docker compose logs -f gateway

# Stop all services
docker compose down

# List pods in the namespace
kubectl -n yolo-pipeline get pods -o wide

# Stream service logs
kubectl -n yolo-pipeline logs -f deployment/router
```




