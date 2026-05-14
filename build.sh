#!/usr/bin/env bash
# ============================================================
#  build.sh — Build + deploy YOLO microservices
#
#  Usage:
#    ./build.sh proto          # Generate gRPC stubs
#    ./build.sh docker         # Build all Docker images
#    ./build.sh compose        # Start locally with docker-compose
#    ./build.sh k8s            # Deploy to Kubernetes
#    ./build.sh local          # Run 4 processes locally without Docker
#    ./build.sh all            # proto → docker → compose
# ============================================================

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVICES_DIR="$ROOT/services"

REGISTRY="${REGISTRY:-your-registry}"
TAG="${TAG:-latest}"

# ---- 1) Generate proto stubs ----
cmd_proto() {
    echo ">>> Generating gRPC stubs..."
    bash "$ROOT/setup.sh" proto
}

# ---- 2) Build Docker images ----
# Each image uses the project root as build context; Dockerfiles generate proto stubs internally
cmd_docker() {
    # Usage: ./build.sh docker [service...]
    # No arguments builds all; named arguments build only those services
    # Example: ./build.sh docker router gateway
    local targets=("$@")
    if [[ $# -eq 0 ]]; then targets=(pedestrian vehicle router gateway); fi

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
                echo "Warning: unknown service: $svc (choices: pedestrian vehicle router gateway)" >&2
                ;;
        esac
    done

    echo ""
    echo "Done."
}

# ---- 3) Start with docker-compose ----
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

# ---- 4) Kubernetes deployment ----
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

# ---- 5) Local development (without Docker) ----
cmd_local() {
    echo ">>> Starting all services locally..."

    # Ensure proto stubs are generated first
    if [ ! -f "$ROOT/proto_gen/pipeline_pb2.py" ]; then
        echo "  Proto stubs not found, generating..."
        cmd_proto
    fi

    echo "Starting pedestrian-service on :50052..."
    cd "$SERVICES_DIR/pedestrian-service" && PYTHONPATH="$ROOT" python server.py &
    PED_PID=$!

    echo "Starting vehicle-service on :50053..."
    cd "$SERVICES_DIR/vehicle-service" && PYTHONPATH="$ROOT" python server.py &
    VEH_PID=$!

    sleep 3

    echo "Starting router on :50051..."
    cd "$SERVICES_DIR/router-service" && PYTHONPATH="$ROOT" python server.py &
    RTR_PID=$!

    sleep 3

    echo "Starting gateway on :5000..."
    cd "$SERVICES_DIR/gateway" && PYTHONPATH="$ROOT" python server.py &
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

# ---- Entrypoint ----
case "${1:-}" in
    proto)   cmd_proto ;;
    docker)  shift; cmd_docker "$@" ;;
    compose) cmd_compose ;;
    k8s)     cmd_k8s ;;
    local)   cmd_local ;;
    all)     cmd_proto && cmd_docker && cmd_compose ;;
    *)
        echo "Usage: $0 {proto|docker|compose|k8s|local|all}"
        echo ""
        echo "  proto              - Generate gRPC Python stubs"
        echo "  docker [service..] - Build specified images (all if none given)"
        echo "  compose            - Start locally with docker-compose"
        echo "  k8s                - Deploy to Kubernetes cluster"
        echo "  local              - Run 4 processes locally without Docker"
        echo "  all                - proto → docker → compose"
        exit 1
        ;;
esac
