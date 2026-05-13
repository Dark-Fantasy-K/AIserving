#!/usr/bin/env bash
# ============================================================
#  setup.sh — install dependencies + generate gRPC stubs
#
#  Usage:
#    ./setup.sh           # full setup (deps + proto + otel)
#    ./setup.sh deps      # install Python dependencies only
#    ./setup.sh proto     # generate gRPC stubs only
#    ./setup.sh venv      # create venv then install deps
#    ./setup.sh otel      # install and verify OpenTelemetry
# ============================================================

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVICES_DIR="$ROOT/services"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

# ---- prerequisites ----
check_python() {
    if command -v python3 &>/dev/null; then
        PY=python3
    elif command -v python &>/dev/null; then
        PY=python
    else
        die "Python not found — please install Python 3.9+"
    fi

    PY_VER=$($PY -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$($PY -c "import sys; print(sys.version_info.major)")
    PY_MINOR=$($PY -c "import sys; print(sys.version_info.minor)")

    if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
        die "Python 3.9+ required, found: $PY_VER"
    fi
    ok "Python $PY_VER ($PY)"
}

# ---- create virtual environment ----
cmd_venv() {
    check_python
    VENV_DIR="$ROOT/.venv"
    if [ -d "$VENV_DIR" ]; then
        warn "venv already exists: $VENV_DIR"
    else
        echo ">>> Creating virtual environment..."
        $PY -m venv "$VENV_DIR"
        ok "venv created at $VENV_DIR"
    fi

    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    ok "venv activated"
    cmd_deps
}

# ---- install Python dependencies ----
cmd_deps() {
    check_python
    echo ""
    echo ">>> Installing Python dependencies..."

    $PY -m pip install --upgrade pip -q

    echo "  Installing global requirements..."
    $PY -m pip install -r "$ROOT/requirements.txt"
    ok "Global dependencies installed"

    for svc_dir in "$SERVICES_DIR"/*/; do
        svc=$(basename "$svc_dir")
        req="$svc_dir/requirements.txt"
        if [ -f "$req" ]; then
            echo "  Installing $svc dependencies..."
            $PY -m pip install -r "$req" -q
            ok "$svc dependencies installed"
        fi
    done

    echo ""
    ok "All dependencies installed"
}

# ---- generate gRPC Python stubs ----
cmd_proto() {
    check_python
    echo ""
    echo ">>> Generating gRPC Python stubs..."

    $PY -m pip install grpcio-tools -q

    PROTO_FILE="$ROOT/pipeline.proto"
    OUT_DIR="$ROOT/generated"

    [ -f "$PROTO_FILE" ] || die "Proto file not found: $PROTO_FILE"

    rm -rf "$OUT_DIR"
    mkdir -p "$OUT_DIR"

    $PY -m grpc_tools.protoc \
        -I "$ROOT" \
        --python_out="$OUT_DIR" \
        --pyi_out="$OUT_DIR" \
        --grpc_python_out="$OUT_DIR" \
        "$PROTO_FILE"

    # protoc generates absolute imports; rewrite to relative
    sed -i 's/^import pipeline_pb2/from . import pipeline_pb2/' \
        "$OUT_DIR/pipeline_pb2_grpc.py" 2>/dev/null || \
    sed -i '' 's/^import pipeline_pb2/from . import pipeline_pb2/' \
        "$OUT_DIR/pipeline_pb2_grpc.py"

    touch "$OUT_DIR/__init__.py"
    ok "Stubs generated at $OUT_DIR"

    TARGETS=(
        "$ROOT/proto_gen"
        "$SERVICES_DIR/gateway/proto_gen"
        "$SERVICES_DIR/pedestrian-service/proto_gen"
        "$SERVICES_DIR/vehicle-service/proto_gen"
    )

    for target in "${TARGETS[@]}"; do
        rm -rf "$target"
        cp -r "$OUT_DIR" "$target"
        ok "Copied to ${target#$ROOT/}"
    done

    echo ""
    ok "gRPC stubs generated and distributed"
}

# ---- install and verify OpenTelemetry ----
cmd_otel() {
    check_python
    echo ""
    echo ">>> Installing OpenTelemetry packages..."

    OTEL_PKGS=(
        "opentelemetry-api>=1.20"
        "opentelemetry-sdk>=1.20"
        "opentelemetry-exporter-otlp-proto-grpc>=1.20"
    )

    for pkg in "${OTEL_PKGS[@]}"; do
        echo "  Installing $pkg ..."
        $PY -m pip install "$pkg" -q
    done
    ok "OpenTelemetry packages installed"

    echo ""
    echo ">>> Verifying OpenTelemetry imports..."
    $PY - <<'PYEOF'
from opentelemetry import trace, metrics, propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, ConsoleMetricExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
print("  ✓ opentelemetry-api")
print("  ✓ opentelemetry-sdk")
print("  ✓ opentelemetry-exporter-otlp-proto-grpc")
PYEOF
    ok "OpenTelemetry verification passed"

    echo ""
    echo "  Tip: set OTEL_EXPORTER_OTLP_ENDPOINT to export traces to a collector"
    echo "  Example: export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317"
}

# ---- full setup ----
cmd_all() {
    echo "========================================"
    echo "  YOLO Microservices — Environment Setup"
    echo "========================================"
    echo ""
    cmd_deps
    cmd_otel
    cmd_proto
    echo ""
    echo "========================================"
    ok "Setup complete!"
    echo ""
    echo "Next steps:"
    echo "  Local run:  ./build.sh local"
    echo "  Docker:     ./build.sh compose"
    echo "  K8s:        REGISTRY=<your-reg> ./build.sh k8s"
    echo "========================================"
}

# ---- entrypoint ----
case "${1:-all}" in
    deps)  cmd_deps ;;
    proto) cmd_proto ;;
    venv)  cmd_venv ;;
    otel)  cmd_otel ;;
    all)   cmd_all ;;
    *)
        echo "Usage: $0 [deps|proto|venv|otel|all]"
        echo ""
        echo "  (no arg) / all  — full setup: deps + otel + proto stubs"
        echo "  deps            — install Python dependencies only"
        echo "  proto           — generate gRPC stubs only"
        echo "  venv            — create .venv then install deps"
        echo "  otel            — install and verify OpenTelemetry"
        exit 1
        ;;
esac
