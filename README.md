# YOLO Microservices Pipeline

基于 YOLOv8 的实时目标检测微服务系统，采用 gRPC 通信，支持行人姿态估计和车辆计数跟踪。

## 架构概览

```
[客户端]
   │  HTTP POST /predict  (图像)
   ▼
[Gateway :5000]          Flask HTTP 入口
   │  gRPC
   ▼
[Router :50051]          YOLOv8s 检测 → 按类别分流
   ├──────────────┬
   │  gRPC        │  gRPC
   ▼              ▼
[Pedestrian      [Vehicle
 :50052]          :50053]
 YOLOv8s-pose    IoU 跟踪
 姿态估计         车辆计数
```

| 服务 | 端口 | 功能 |
|------|------|------|
| Gateway | 5000 (HTTP) | 对外 REST 入口，转发图像到 Router |
| Router | 50051 (gRPC) | YOLOv8s 检测，分流 person/vehicle |
| Pedestrian | 50052 (gRPC) | YOLOv8s-pose 姿态估计 |
| Vehicle | 50053 (gRPC) | IoU 跟踪 + 车型计数 |

## 目录结构

```
AIChain/
├── setup.sh                        # 一键安装脚本
├── build.sh                        # 构建/部署脚本
├── pipeline.proto                  # gRPC 协议定义
├── requirements.txt                # 全局 Python 依赖
├── server.py                       # Router 服务主文件
├── generate.sh                     # proto 生成（由 setup.sh 调用）
├── docker-compose.yml              # Docker Compose 配置
├── all-in-one.yaml                 # Kubernetes 部署清单
└── mnt/user-data/outputs/yolo-microservices/
    ├── gateway/
    │   ├── server.py
    │   └── requirements.txt
    ├── pedestrian-service/
    │   ├── server.py
    │   └── requirements.txt
    └── vehicle-service/
        ├── server.py
        └── requirements.txt
```

---

## Bare Metal 全量安装

以下步骤适用于从零开始的裸机环境（Ubuntu 20.04 / 22.04），假设机器上**没有 Python、curl、Docker**。

### 第一步：系统包

```bash
sudo apt update && sudo apt install -y \
    python3 python3-pip python3-venv python3-dev \
    build-essential gcc g++ \
    libglib2.0-0 libgl1 libgomp1 \
    git wget
```

> - `python3-venv` — 创建虚拟环境必需
> - `python3-dev` + `build-essential` — 编译 grpcio 等带 C 扩展的包
> - `libglib2.0-0` + `libgl1-mesa-glx` — OpenCV headless 运行时
> - `libgomp1` — PyTorch OpenMP 多线程推理

验证安装：

```bash
python3 --version    # 应为 3.9+
pip3 --version
```

### 第二步：创建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
# 之后 python / pip 均指向 venv 内的版本
python --version
```

> 退出 venv：`deactivate`；下次进入：`source .venv/bin/activate`

### 第三步：安装 Python 依赖 + 生成 gRPC stubs

激活 venv 后执行：

```bash
chmod +x setup.sh
./setup.sh deps     # 安装所有 Python 包
./setup.sh proto    # 生成 gRPC stubs 并分发到各服务
```

或一步到位：

```bash
./setup.sh          # 等价于 deps + proto
```

> `torch` + `ultralytics` 体积约 2 GB，首次安装耗时较长，请耐心等待。

### 第四步：（可选）NVIDIA GPU 驱动 + CUDA

没有 GPU 可跳过，系统自动降级为 CPU 推理。

```bash
# 检查是否已有驱动
nvidia-smi

# 若无，安装 CUDA Toolkit（以 CUDA 12.1 为例）
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update && sudo apt install -y cuda-toolkit-12-1
echo 'export PATH=/usr/local/cuda/bin:$PATH' >> ~/.bashrc
echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc
```

安装 CUDA 后，替换 torch 为 GPU 版本：

```bash
source .venv/bin/activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```

### 第五步：（可选）Docker + Docker Compose

本地运行不需要 Docker，仅容器/K8s 部署时才需要。

```bash
# 安装 Docker Engine（不依赖 curl）
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
newgrp docker
docker --version
docker compose version
```

---

## 快速开始

### 本地运行（无 Docker）

```bash
./build.sh local
```

启动 4 个进程，访问 `http://localhost:5000`。

### Docker Compose

```bash
./build.sh compose
```

等价于 `docker compose up --build -d`，访问 `http://localhost:5000`。

### Kubernetes

```bash
REGISTRY=your-registry ./build.sh docker   # 构建镜像
docker push your-registry/pedestrian-service:latest
docker push your-registry/vehicle-service:latest
docker push your-registry/router-service:latest
docker push your-registry/gateway:latest

REGISTRY=your-registry ./build.sh k8s      # 部署
```

访问：`http://<node-ip>:30500`

---

## API 接口

### `GET /health`

```json
{"status": "ok", "service": "gateway", "router": "localhost:50051"}
```

### `POST /predict`

**请求**：`multipart/form-data`，字段名 `image`，支持 JPEG/PNG。

**响应示例**：

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

**curl 示例**：

```bash
curl -X POST http://localhost:5000/predict \
     -F "image=@/path/to/image.jpg" | python3 -m json.tool
```

---

## 依赖说明

### 系统包

| 包 | 用途 |
|----|------|
| `python3-dev` / `build-essential` | 编译 grpcio 等带 C 扩展的包 |
| `libglib2.0-0` | OpenCV 运行时 |
| `libgl1-mesa-glx` | OpenCV GUI 依赖（headless 版也需要） |
| `libgomp1` | PyTorch OpenMP 多线程推理 |

### Python 包

| 包 | 用途 |
|----|------|
| `grpcio` / `grpcio-tools` | gRPC 服务间通信 |
| `protobuf` | 协议序列化 |
| `flask` | Gateway HTTP 服务 |
| `ultralytics` | YOLOv8 目标检测 / 姿态估计 |
| `torch` / `torchvision` | 深度学习推理后端 |
| `opencv-python-headless` | 图像标注渲染 |
| `pillow` / `numpy` | 图像处理 |

---

## 重新生成 gRPC stubs

修改 `pipeline.proto` 后，运行：

```bash
./setup.sh proto
```

stubs 会自动生成并复制到所有服务的 `proto_gen/` 目录。

---

## 常用命令

```bash
# 查看服务日志
docker compose logs -f

# 查看单个服务日志
docker compose logs -f gateway

# 停止所有服务
docker compose down

# K8s 查看 Pod 状态
kubectl -n yolo-pipeline get pods -o wide

# K8s 查看服务日志
kubectl -n yolo-pipeline logs -f deployment/router
```
