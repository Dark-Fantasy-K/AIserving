#!/usr/bin/env bash
# 生成 gRPC Python 代码，复制到各服务目录
set -e

PROTO_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$PROTO_DIR/generated"
mkdir -p "$OUT_DIR"

python -m grpc_tools.protoc \
    -I "$PROTO_DIR" \
    --python_out="$OUT_DIR" \
    --pyi_out="$OUT_DIR" \
    --grpc_python_out="$OUT_DIR" \
    "$PROTO_DIR/pipeline.proto"

# 修复 import 路径
sed -i 's/^import pipeline_pb2/from . import pipeline_pb2/' "$OUT_DIR/pipeline_pb2_grpc.py" 2>/dev/null || \
sed -i '' 's/^import pipeline_pb2/from . import pipeline_pb2/' "$OUT_DIR/pipeline_pb2_grpc.py"

touch "$OUT_DIR/__init__.py"

# 复制到各服务
for svc in router pedestrian-service vehicle-service gateway; do
    target="$PROTO_DIR/../$svc/proto_gen"
    rm -rf "$target"
    cp -r "$OUT_DIR" "$target"
    echo "✓ Copied to $svc/proto_gen/"
done

echo "Done!"
