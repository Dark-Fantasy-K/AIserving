#!/usr/bin/env bash
# ============================================================
#  setup.sh — 一键安装依赖 + 生成 gRPC 代码
#
#  用法:
#    ./setup.sh           # 完整安装 (依赖 + proto)
#    ./setup.sh deps      # 仅安装 Python 依赖
#    ./setup.sh proto     # 仅生成 gRPC stubs
#    ./setup.sh venv      # 创建 venv 后再安装依赖
# ============================================================

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVICES_DIR="$ROOT/mnt/user-data/outputs/yolo-microservices"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

# ---- 前置检查 ----
check_python() {
    if command -v python3 &>/dev/null; then
        PY=python3
    elif command -v python &>/dev/null; then
        PY=python
    else
        die "未找到 Python，请先安装 Python 3.9+"
    fi

    PY_VER=$($PY -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$($PY -c "import sys; print(sys.version_info.major)")
    PY_MINOR=$($PY -c "import sys; print(sys.version_info.minor)")

    if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
        die "需要 Python 3.9+，当前版本: $PY_VER"
    fi
    ok "Python $PY_VER ($PY)"
}

# ---- 创建虚拟环境 ----
cmd_venv() {
    check_python
    VENV_DIR="$ROOT/.venv"
    if [ -d "$VENV_DIR" ]; then
        warn "venv 已存在: $VENV_DIR"
    else
        echo ">>> 创建虚拟环境..."
        $PY -m venv "$VENV_DIR"
        ok "venv 创建于 $VENV_DIR"
    fi

    # 激活
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    ok "已激活 venv"
    cmd_deps
}

# ---- 安装 Python 依赖 ----
cmd_deps() {
    check_python
    echo ""
    echo ">>> 安装 Python 依赖..."

    $PY -m pip install --upgrade pip -q

    # 全局 requirements
    echo "  安装全局依赖..."
    $PY -m pip install -r "$ROOT/requirements.txt"
    ok "全局依赖安装完成"

    # 各服务的 requirements（去重安装）
    for svc_dir in "$SERVICES_DIR"/*/; do
        svc=$(basename "$svc_dir")
        req="$svc_dir/requirements.txt"
        if [ -f "$req" ]; then
            echo "  安装 $svc 依赖..."
            $PY -m pip install -r "$req" -q
            ok "$svc 依赖安装完成"
        fi
    done

    echo ""
    ok "所有依赖安装完成"
}

# ---- 生成 gRPC Python stubs ----
cmd_proto() {
    check_python
    echo ""
    echo ">>> 生成 gRPC Python stubs..."

    # 安装 grpcio-tools（如果未安装）
    $PY -m pip install grpcio-tools -q

    PROTO_FILE="$ROOT/pipeline.proto"
    OUT_DIR="$ROOT/generated"

    [ -f "$PROTO_FILE" ] || die "未找到 $PROTO_FILE"

    rm -rf "$OUT_DIR"
    mkdir -p "$OUT_DIR"

    $PY -m grpc_tools.protoc \
        -I "$ROOT" \
        --python_out="$OUT_DIR" \
        --pyi_out="$OUT_DIR" \
        --grpc_python_out="$OUT_DIR" \
        "$PROTO_FILE"

    # 修复 import 路径（protoc 生成的是绝对 import，需改为相对）
    sed -i 's/^import pipeline_pb2/from . import pipeline_pb2/' \
        "$OUT_DIR/pipeline_pb2_grpc.py" 2>/dev/null || \
    sed -i '' 's/^import pipeline_pb2/from . import pipeline_pb2/' \
        "$OUT_DIR/pipeline_pb2_grpc.py"

    touch "$OUT_DIR/__init__.py"
    ok "stubs 生成于 $OUT_DIR"

    # 目标目录：root (router), 各服务
    TARGETS=(
        "$ROOT/proto_gen"
        "$SERVICES_DIR/gateway/proto_gen"
        "$SERVICES_DIR/pedestrian-service/proto_gen"
        "$SERVICES_DIR/vehicle-service/proto_gen"
    )

    for target in "${TARGETS[@]}"; do
        rm -rf "$target"
        cp -r "$OUT_DIR" "$target"
        ok "已复制到 ${target#$ROOT/}"
    done

    echo ""
    ok "gRPC stubs 生成并分发完成"
}

# ---- 完整安装 ----
cmd_all() {
    echo "========================================"
    echo "  YOLO Microservices — 环境初始化"
    echo "========================================"
    echo ""
    cmd_deps
    cmd_proto
    echo ""
    echo "========================================"
    ok "初始化完成！"
    echo ""
    echo "下一步："
    echo "  本地运行:   ./build.sh local"
    echo "  Docker:     ./build.sh compose"
    echo "  K8s:        REGISTRY=<your-reg> ./build.sh k8s"
    echo "========================================"
}

# ---- 入口 ----
case "${1:-all}" in
    deps)  cmd_deps ;;
    proto) cmd_proto ;;
    venv)  cmd_venv ;;
    all)   cmd_all ;;
    *)
        echo "用法: $0 [deps|proto|venv|all]"
        echo ""
        echo "  (无参数) / all  — 完整安装: 依赖 + proto stubs"
        echo "  deps            — 仅安装 Python 依赖"
        echo "  proto           — 仅生成 gRPC stubs"
        echo "  venv            — 创建 .venv 后安装依赖"
        exit 1
        ;;
esac
