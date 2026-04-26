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
SERVICES_DIR="$ROOT/mnt/user-data/outputs/yolo-microservices"

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
    echo ">>> Building Docker images (context: $ROOT)..."
    cd "$ROOT"

    echo "  [1/4] pedestrian-service..."
    docker build \
        -f "$SERVICES_DIR/pedestrian-service/Dockerfile" \
        -t "${REGISTRY}/pedestrian-service:${TAG}" \
        .
    echo "  ✓ ${REGISTRY}/pedestrian-service:${TAG}"

    echo "  [2/4] vehicle-service..."
    docker build \
        -f "$SERVICES_DIR/vehicle-service/Dockerfile" \
        -t "${REGISTRY}/vehicle-service:${TAG}" \
        .
    echo "  ✓ ${REGISTRY}/vehicle-service:${TAG}"

    echo "  [3/4] router-service..."
    docker build \
        -f "$ROOT/Dockerfile.router" \
        -t "${REGISTRY}/router-service:${TAG}" \
        .
    echo "  ✓ ${REGISTRY}/router-service:${TAG}"

    echo "  [4/4] gateway..."
    docker build \
        -f "$SERVICES_DIR/gateway/Dockerfile" \
        -t "${REGISTRY}/gateway:${TAG}" \
        .
    echo "  ✓ ${REGISTRY}/gateway:${TAG}"

    echo ""
    echo "Done. Push with:"
    echo "  docker push ${REGISTRY}/pedestrian-service:${TAG}"
    echo "  docker push ${REGISTRY}/vehicle-service:${TAG}"
    echo "  docker push ${REGISTRY}/router-service:${TAG}"
    echo "  docker push ${REGISTRY}/gateway:${TAG}"
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
cmd_local() {
    echo ">>> Starting all services locally..."

    # 先确保 proto stubs 已生成
    if [ ! -f "$ROOT/proto_gen/pipeline_pb2.py" ]; then
        echo "  proto stubs 未找到，先生成..."
        cmd_proto
    fi

    echo "Starting pedestrian-service on :50052..."
    cd "$SERVICES_DIR/pedestrian-service" && python server.py &
    PED_PID=$!

    echo "Starting vehicle-service on :50053..."
    cd "$SERVICES_DIR/vehicle-service" && python server.py &
    VEH_PID=$!

    sleep 3

    echo "Starting router on :50051..."
    cd "$ROOT" && python server.py &
    RTR_PID=$!

    sleep 3

    echo "Starting gateway on :5000..."
    cd "$SERVICES_DIR/gateway" && python server.py &
    GW_PID=$!

    echo ""
    echo "All services running:"
    echo "  Gateway:    http://localhost:5000   (PID: $GW_PID)"
    echo "  Router:     :50051                 (PID: $RTR_PID)"
    echo "  Pedestrian: :50052                 (PID: $PED_PID)"
    echo "  Vehicle:    :50053                 (PID: $VEH_PID)"
    echo ""
    echo "Press Ctrl+C to stop all"

    trap "kill $PED_PID $VEH_PID $RTR_PID $GW_PID 2>/dev/null" EXIT
    wait
}

# ---- 入口 ----
case "${1:-}" in
    proto)   cmd_proto ;;
    docker)  cmd_docker ;;
    compose) cmd_compose ;;
    k8s)     cmd_k8s ;;
    local)   cmd_local ;;
    all)     cmd_proto && cmd_docker && cmd_compose ;;
    *)
        echo "用法: $0 {proto|docker|compose|k8s|local|all}"
        echo ""
        echo "  proto   - 生成 gRPC Python 代码"
        echo "  docker  - 构建所有 Docker 镜像"
        echo "  compose - docker-compose 本地启动"
        echo "  k8s     - 部署到 Kubernetes 集群"
        echo "  local   - 不用 Docker，直接本地跑 4 个进程"
        echo "  all     - proto → docker → compose"
        exit 1
        ;;
esac
