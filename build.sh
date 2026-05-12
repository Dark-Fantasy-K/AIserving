#!/usr/bin/env bash
# ============================================================
#  build.sh — 构建 + 部署 YOLO 微服务
#
#  用法:
#    ./build.sh proto          # 生成 gRPC 代码
#    ./build.sh docker         # 构建所有 Docker 镜像
#    ./build.sh compose        # docker-compose 本地启动
#    ./build.sh k8s            # 部署到 K8s
#    ./build.sh local          # 不用 Docker，直接本地跑 4 个进程
#    ./build.sh all            # proto → docker → compose
# ============================================================

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVICES_DIR="$ROOT/services"

REGISTRY="${REGISTRY:-your-registry}"
TAG="${TAG:-latest}"

# ---- 1) 生成 proto ----
cmd_proto() {
    echo ">>> Generating gRPC stubs..."
    bash "$ROOT/setup.sh" proto
}

# ---- 2) 构建 Docker 镜像 ----
# 每个镜像都以项目根为 build context，Dockerfile 内部自行生成 proto stubs
cmd_docker() {
    # 用法: ./build.sh docker [服务名...]
    # 不传参数则构建全部；传名称则只构建指定服务
    # 示例: ./build.sh docker router gateway
    local targets=("${@:-pedestrian vehicle router gateway}")

    echo ">>> Building Docker images (context: $ROOT)..."
    cd "$ROOT"

    local idx=0
    for svc in "${targets[@]}"; do
        idx=$((idx + 1))
        case "$svc" in
            pedestrian)
                echo "  [$idx] pedestrian-service..."
                docker build -f "$SERVICES_DIR/pedestrian-service/Dockerfile" \
                    -t "${REGISTRY}/pedestrian-service:${TAG}" .
                echo "  ✓ ${REGISTRY}/pedestrian-service:${TAG}"
                ;;
            vehicle)
                echo "  [$idx] vehicle-service..."
                docker build -f "$SERVICES_DIR/vehicle-service/Dockerfile" \
                    -t "${REGISTRY}/vehicle-service:${TAG}" .
                echo "  ✓ ${REGISTRY}/vehicle-service:${TAG}"
                ;;
            router)
                echo "  [$idx] router-service..."
                docker build -f "$SERVICES_DIR/router-service/Dockerfile" \
                    -t "${REGISTRY}/router-service:${TAG}" .
                echo "  ✓ ${REGISTRY}/router-service:${TAG}"
                ;;
            gateway)
                echo "  [$idx] gateway..."
                docker build -f "$SERVICES_DIR/gateway/Dockerfile" \
                    -t "${REGISTRY}/gateway:${TAG}" .
                echo "  ✓ ${REGISTRY}/gateway:${TAG}"
                ;;
            *)
                echo "未知服务: $svc（可选: pedestrian vehicle router gateway）" >&2
                ;;
        esac
    done

    echo ""
    echo "Done."
}

# ---- 3) docker-compose 启动 ----
cmd_compose() {
    echo ">>> Starting with docker-compose..."
    cd "$ROOT"
    docker compose up --build -d
    echo ""
    echo "Services:"
    echo "  Gateway:    http://localhost:5000"
    echo "  Router:     localhost:50051 (gRPC)"
    echo "  Pedestrian: localhost:50052 (gRPC)"
    echo "  Vehicle:    localhost:50053 (gRPC)"
    echo ""
    echo "Logs: docker compose logs -f"
}

# ---- 4) K8s 部署 ----
cmd_k8s() {
    echo ">>> Deploying to Kubernetes..."

    sed -i "s|your-registry|${REGISTRY}|g" "$ROOT/all-in-one.yaml" 2>/dev/null || \
    sed -i '' "s|your-registry|${REGISTRY}|g" "$ROOT/all-in-one.yaml"

    kubectl apply -f "$ROOT/all-in-one.yaml"

    echo ""
    echo "Waiting for pods..."
    kubectl -n yolo-pipeline rollout status deployment/pedestrian --timeout=120s
    kubectl -n yolo-pipeline rollout status deployment/vehicle --timeout=60s
    kubectl -n yolo-pipeline rollout status deployment/router --timeout=120s
    kubectl -n yolo-pipeline rollout status deployment/gateway --timeout=60s

    echo ""
    echo "Pods:"
    kubectl -n yolo-pipeline get pods -o wide
    echo ""
    echo "Services:"
    kubectl -n yolo-pipeline get svc
    echo ""
    echo "Access: http://<node-ip>:30500"
}

# ---- 5) 本地开发（不用 Docker）----

# 判断是否需要重新生成 gRPC stubs
# 返回 0 = 需要，1 = 无需
_needs_proto() {
    local proto="$ROOT/pipeline.proto"
    local targets=(
        "$ROOT/proto_gen/pipeline_pb2.py"
        "$SERVICES_DIR/gateway/proto_gen/pipeline_pb2.py"
        "$SERVICES_DIR/pedestrian-service/proto_gen/pipeline_pb2.py"
        "$SERVICES_DIR/vehicle-service/proto_gen/pipeline_pb2.py"
    )
    for t in "${targets[@]}"; do
        [ ! -f "$t" ] && return 0
        [ "$proto" -nt "$t" ] && return 0
    done
    return 1
}

cmd_local() {
    echo ">>> Starting all services locally..."

    PYTHON="${ROOT}/.venv/bin/python"
    if [ ! -x "$PYTHON" ]; then
        PYTHON="$(command -v python3 || command -v python)"
    fi
    echo "Using Python: $PYTHON"

    if _needs_proto; then
        echo ">>> pipeline.proto 有变更，重新生成 gRPC stubs..."
        cmd_proto
    else
        echo ">>> gRPC stubs 已是最新，跳过生成"
    fi

    # ---- 检查 ONNX 模型文件 ----
    local det_model="$ROOT/triton-models/yolov8s/1/model.onnx"
    local pose_model="$ROOT/triton-models/yolov8s-pose/1/model.onnx"
    if [ ! -f "$det_model" ] || [ ! -f "$pose_model" ]; then
        echo ">>> ONNX 模型文件缺失，正在导出..."
        cd "$ROOT" && "$PYTHON" scripts/export_models.py
    else
        echo ">>> ONNX 模型已存在，跳过导出"
    fi

    # ---- 启动 Triton（通过 Docker）----
    local triton_status
    triton_status=$(docker inspect --format '{{.State.Status}}' triton 2>/dev/null || echo "missing")

    if [ "$triton_status" = "running" ]; then
        echo ">>> Triton 已在运行，跳过启动"
    else
        if [ "$triton_status" = "missing" ]; then
            echo ">>> 启动 Triton 容器..."
            docker run -d \
                --name triton \
                -p 8000:8000 -p 8001:8001 -p 8002:8002 \
                -v "$ROOT/triton-models:/models" \
                nvcr.io/nvidia/tritonserver:24.05-py3 \
                tritonserver --model-repository=/models --log-verbose=0
        else
            echo ">>> 重启已停止的 Triton 容器..."
            docker start triton
        fi
        echo ">>> 等待 Triton 模型加载..."
        for i in $(seq 1 30); do
            if curl -sf http://localhost:8000/v2/health/ready >/dev/null 2>&1; then
                echo ">>> Triton ready"
                break
            fi
            sleep 2
        done
    fi

    export TRITON_URL="localhost:8001"

    echo "Starting pedestrian-service on :50052..."
    cd "$SERVICES_DIR/pedestrian-service" && PYTHONPATH="$ROOT" "$PYTHON" server.py &
    PED_PID=$!

    echo "Starting vehicle-service on :50053..."
    cd "$SERVICES_DIR/vehicle-service" && PYTHONPATH="$ROOT" "$PYTHON" server.py &
    VEH_PID=$!

    sleep 3

    echo "Starting router on :50051..."
    cd "$SERVICES_DIR/router-service" && PYTHONPATH="$ROOT" "$PYTHON" server.py &
    RTR_PID=$!

    sleep 3

    echo "Starting gateway on :5000..."
    cd "$SERVICES_DIR/gateway" && PYTHONPATH="$ROOT" "$PYTHON" server.py &
    GW_PID=$!

    echo ""
    echo "All services running:"
    echo "  Gateway:    http://localhost:5000   (PID: $GW_PID)"
    echo "  Router:     :50051                 (PID: $RTR_PID)"
    echo "  Pedestrian: :50052                 (PID: $PED_PID)"
    echo "  Vehicle:    :50053                 (PID: $VEH_PID)"
    echo ""
    echo "Press Ctrl+C to stop all"

    trap "kill $PED_PID $VEH_PID $RTR_PID $GW_PID 2>/dev/null; docker stop triton 2>/dev/null" EXIT
    wait
}

# ---- 入口 ----
case "${1:-}" in
    proto)   cmd_proto ;;
    docker)  shift; cmd_docker "$@" ;;
    compose) cmd_compose ;;
    k8s)     cmd_k8s ;;
    local)   cmd_local ;;
    all)     cmd_proto && cmd_docker && cmd_compose ;;
    *)
        echo "用法: $0 {proto|docker|compose|k8s|local|all}"
        echo ""
        echo "  proto              - 生成 gRPC Python 代码"
        echo "  docker [服务...]   - 构建指定镜像（不传则全部）"
        echo "  compose - docker-compose 本地启动"
        echo "  k8s     - 部署到 Kubernetes 集群"
        echo "  local   - 不用 Docker，直接本地跑 4 个进程"
        echo "  all     - proto → docker → compose"
        exit 1
        ;;
esac
