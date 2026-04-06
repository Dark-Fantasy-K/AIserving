#!/bin/bash
# =============================================================
#  AIserving 一键部署脚本
#  仓库: https://github.com/Dark-Fantasy-K/AIserving
#  CNI:  Cilium (默认) + Hubble 流量可观测性
# =============================================================

set -euo pipefail

# ── 颜色 & 日志函数 ────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()     { echo -e "${CYAN}$(date '+%H:%M:%S')${NC} $*"; }
success() { echo -e "${GREEN}✅ $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠️  $*${NC}"; }
error()   { echo -e "${RED}❌ $*${NC}" >&2; exit 1; }
step()    { echo -e "\n${BOLD}${BLUE}$*${NC}"; echo -e "${BLUE}$(printf '─%.0s' {1..52})${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR/deployment"
FLASK_DIR="$DEPLOY_DIR/flask_ai"
RAY_DIR="$DEPLOY_DIR/ray_serve"
FLASK_IMAGE="ai-serving:latest"
RAY_IMAGE="ai-serving-ray:latest"

# Cilium 版本（可通过环境变量覆盖）
CILIUM_VERSION="${CILIUM_VERSION:-v1.16.5}"

echo -e "${BOLD}"
cat << 'EOF'
  ╔════════════════════════════════════════════════╗
  ║   🤖  AIserving 一键部署脚本                  ║
  ║   Flask + Ray Serve · k3s · Cilium · Hubble   ║
  ╚════════════════════════════════════════════════╝
EOF
echo -e "${NC}"

log "📁 脚本目录: $SCRIPT_DIR"
log "🕐 开始时间: $(date)"

# ══════════════════════════════════════════════════════════════
step "🔧 Step 1/8  安装 k3s（Cilium CNI 模式）"
# ══════════════════════════════════════════════════════════════
# 关键参数：
#   --flannel-backend=none     禁用默认 flannel，由 Cilium 接管 CNI
#   --disable-network-policy   禁用 k3s 内置 NetworkPolicy，由 Cilium 管理
#   --disable=traefik          禁用 Traefik，减少资源占用

if command -v k3s &>/dev/null && systemctl is-active --quiet k3s; then
    # 检查是否以 Cilium 模式启动（无 flannel）
    if sudo cat /etc/systemd/system/k3s.service 2>/dev/null | grep -q "flannel-backend=none"; then
        warn "k3s (Cilium 模式) 已在运行，跳过安装"
    else
        warn "k3s 以 flannel 模式运行，需重装以支持 Cilium"
        log "🗑️  卸载旧版 k3s..."
        sudo /usr/local/bin/k3s-uninstall.sh 2>/dev/null || true
        log "📥 重新安装 k3s (Cilium CNI 模式)..."
        curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \
          --flannel-backend=none \
          --disable-network-policy \
          --disable=traefik \
          --write-kubeconfig-mode=644" sh -
    fi
else
    log "📥 安装 k3s (Cilium CNI 模式)..."
    curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \
      --flannel-backend=none \
      --disable-network-policy \
      --disable=traefik \
      --write-kubeconfig-mode=644" sh -
fi

# 等待 API server 就绪
log "⏳ 等待 k3s API server 就绪..."
for i in {1..30}; do
    kubectl get nodes &>/dev/null && break
    sleep 3
done
success "k3s 安装完成，节点: $(kubectl get nodes --no-headers | awk '{print $1}')"

# ══════════════════════════════════════════════════════════════
step "🐳 Step 2/8  安装 Docker"
# ══════════════════════════════════════════════════════════════

if command -v docker &>/dev/null && sudo docker info &>/dev/null; then
    warn "Docker 已安装，跳过"
else
    log "📥 安装 Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER" || true
    sudo systemctl start docker
    sudo systemctl enable docker
fi
success "Docker $(sudo docker --version | awk '{print $3}' | tr -d ',')"

# ══════════════════════════════════════════════════════════════
step "🌐 Step 3/8  安装 Cilium CNI + Hubble 可观测性"
# ══════════════════════════════════════════════════════════════
# Cilium 替代 flannel 作为 CNI，并内置 Hubble 网络流量可视化
# Hubble 提供 L7 HTTP 延迟、请求率、错误率的实时观测

# 3a. 安装 cilium CLI
if ! command -v cilium &>/dev/null; then
    log "📥 安装 cilium CLI..."
    CILIUM_CLI_VERSION=$(curl -s https://raw.githubusercontent.com/cilium/cilium-cli/main/stable.txt)
    curl -sL "https://github.com/cilium/cilium-cli/releases/download/${CILIUM_CLI_VERSION}/cilium-linux-amd64.tar.gz" \
        | sudo tar xz -C /usr/local/bin
fi
success "cilium CLI $(cilium version --client 2>/dev/null | head -1)"

# 3b. 安装 hubble CLI
if ! command -v hubble &>/dev/null; then
    log "📥 安装 hubble CLI..."
    HUBBLE_VERSION=$(curl -s https://raw.githubusercontent.com/cilium/hubble/master/stable.txt)
    curl -sL "https://github.com/cilium/hubble/releases/download/${HUBBLE_VERSION}/hubble-linux-amd64.tar.gz" \
        | sudo tar xz -C /usr/local/bin
fi
success "hubble CLI $(hubble version 2>/dev/null | head -1)"

# 3c. 安装 Cilium（含 Hubble relay + UI）
if kubectl get daemonset cilium -n kube-system &>/dev/null 2>&1; then
    warn "Cilium 已部署，跳过安装"
else
    NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
    log "🚀 部署 Cilium ${CILIUM_VERSION}（节点 IP: ${NODE_IP}）..."
    # hubble.metrics 启用 HTTP L7 延迟指标，供 Prometheus 抓取
    cilium install \
        --version "${CILIUM_VERSION}" \
        --set k8sServiceHost="${NODE_IP}" \
        --set k8sServicePort=6443 \
        --set hubble.relay.enabled=true \
        --set hubble.ui.enabled=true \
        --set hubble.metrics.enableOpenMetrics=true \
        --set "hubble.metrics.enabled={dns,drop,tcp,flow,port-distribution,icmp,httpV2:exemplars=true;labelsContext=source_ip\,source_namespace\,source_workload\,destination_ip\,destination_namespace\,destination_workload\,traffic_direction}"
fi

log "⏳ 等待 Cilium 全部就绪（约 60-90s）..."
# 节点先进入 Ready 再继续
for i in {1..60}; do
    STATUS=$(kubectl get nodes --no-headers 2>/dev/null | awk '{print $2}')
    [[ "$STATUS" == "Ready" ]] && break
    sleep 3
done
# 等待 cilium-agent
kubectl rollout status daemonset/cilium -n kube-system --timeout=180s
kubectl rollout status deployment/hubble-relay -n kube-system --timeout=120s
kubectl rollout status deployment/hubble-ui -n kube-system --timeout=60s

success "Cilium + Hubble 就绪"
kubectl get pods -n kube-system -l 'k8s-app in (cilium,hubble-relay,hubble-ui)' --no-headers \
    | awk '{printf "   %-45s %s\n", $1, $3}'

# ══════════════════════════════════════════════════════════════
step "🏗️  Step 4/8  构建 Docker 镜像（Flask + Ray Serve）"
# ══════════════════════════════════════════════════════════════

# Flask
log "📦 构建 Flask 镜像 ${FLASK_IMAGE}..."
sudo docker build -t "${FLASK_IMAGE}" "${FLASK_DIR}"
success "Flask 镜像构建完成"

# Ray Serve
log "📦 构建 Ray Serve 镜像 ${RAY_IMAGE}（含 PyTorch，约 3-5 分钟）..."
sudo docker build -t "${RAY_IMAGE}" "${RAY_DIR}"
success "Ray Serve 镜像构建完成"

# ══════════════════════════════════════════════════════════════
step "📤 Step 5/8  导入镜像到 k3s containerd"
# ══════════════════════════════════════════════════════════════
# k3s 使用独立 containerd，与 Docker daemon 隔离，需手动 import

log "📤 导入 Flask 镜像..."
sudo docker save "${FLASK_IMAGE}" | sudo k3s ctr images import -

log "📤 导入 Ray Serve 镜像..."
sudo docker save "${RAY_IMAGE}" | sudo k3s ctr images import -

# 验证
for img in "${FLASK_IMAGE}" "${RAY_IMAGE}"; do
    if sudo k3s ctr images list 2>/dev/null | grep -q "${img%%:*}"; then
        success "已确认镜像: ${img}"
    else
        error "镜像导入验证失败: ${img}"
    fi
done

# ══════════════════════════════════════════════════════════════
step "☸️  Step 6/8  部署 Kubernetes 资源"
# ══════════════════════════════════════════════════════════════

log "📄 部署公共资源（namespace / PV / Flask）..."
kubectl apply -f "${DEPLOY_DIR}/namespace.yaml"
kubectl apply -f "${DEPLOY_DIR}/pv-model-cache.yaml"
kubectl apply -f "${DEPLOY_DIR}/deployment.yaml"
kubectl apply -f "${DEPLOY_DIR}/service.yaml"

log "📄 部署 Ray Serve..."
kubectl apply -f "${RAY_DIR}/deployment.yaml"
kubectl apply -f "${RAY_DIR}/service.yaml"

success "所有 Kubernetes 资源已创建"
kubectl get pods -n ai-serving

# ══════════════════════════════════════════════════════════════
step "🔍 Step 7/8  配置 Hubble L7 流量可见性"
# ══════════════════════════════════════════════════════════════
# 两项配置缺一不可：
#   1. CiliumNetworkPolicy (L7 规则) — 告诉 Cilium 代理哪些 HTTP 路径
#   2. Pod 注解 io.cilium.proxy-visibility — 强制流量经过 Envoy 代理
#      格式: "<Ingress/PORT/TCP/HTTP>"

log "📋 应用 Cilium L7 可见性策略..."
kubectl apply -f "${DEPLOY_DIR}/cilium-visibility.yaml"

log "🏷️  给 Pod 打 Hubble L7 代理注解..."
for deploy in ai-serving ai-serving-ray; do
    CURRENT=$(kubectl get deployment "${deploy}" -n ai-serving \
        -o jsonpath='{.spec.template.metadata.annotations.io\.cilium\.proxy-visibility}' 2>/dev/null || true)
    if [ "${CURRENT}" != "<Ingress/8000/TCP/HTTP>" ]; then
        kubectl patch deployment "${deploy}" -n ai-serving --type=json -p='[
          {"op":"add","path":"/spec/template/metadata/annotations/io.cilium.proxy-visibility",
           "value":"<Ingress/8000/TCP/HTTP>"}
        ]'
        log "   ${deploy} → 已添加代理注解"
    else
        warn "   ${deploy} 注解已存在，跳过"
    fi
done

# 暴露 Hubble UI（NodePort 30880）
HUBBLE_TYPE=$(kubectl get svc hubble-ui -n kube-system \
    -o jsonpath='{.spec.type}' 2>/dev/null || echo "")
if [ "${HUBBLE_TYPE}" != "NodePort" ]; then
    log "🌐 将 Hubble UI 暴露为 NodePort 30880..."
    kubectl patch svc hubble-ui -n kube-system \
        -p '{"spec":{"type":"NodePort","ports":[{"port":80,"targetPort":8081,"nodePort":30880}]}}'
fi

# 启动 Hubble relay port-forward（后台，供 hubble CLI 使用）
if ! pgrep -f "port-forward.*hubble-relay" &>/dev/null; then
    log "🔗 启动 Hubble relay port-forward (4245:80)..."
    kubectl port-forward -n kube-system svc/hubble-relay 4245:80 \
        &>/var/log/hubble-pf.log &
    sleep 3
fi

success "Hubble L7 可见性配置完成"

# ══════════════════════════════════════════════════════════════
step "⏳ Step 8/8  等待服务就绪 & 验证"
# ══════════════════════════════════════════════════════════════

log "🔄 等待 Flask deployment 就绪（最长 5 分钟）..."
kubectl rollout status deployment/ai-serving -n ai-serving --timeout=300s
log "🔄 等待 Ray Serve deployment 就绪（最长 6 分钟）..."
kubectl rollout status deployment/ai-serving-ray -n ai-serving --timeout=360s

log "📋 最终 Pod 状态:"
kubectl get pods -n ai-serving -o wide

# 等待 HTTP 健康检查
log "⏳ 等待 Flask 健康检查通过..."
for i in {1..30}; do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:30800/health 2>/dev/null || true)
    [ "$HTTP_CODE" = "200" ] && break
    printf "   尝试 %d/30 (HTTP %s)...\r" "$i" "$HTTP_CODE"
    sleep 5
done
echo ""

# ── Flask 验证 ────────────────────────────────────────────────
log "🏥 Flask /health ..."
HEALTH=$(curl -s http://localhost:30800/health)
STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "?")
[ "$STATUS" = "healthy" ] && success "Flask /health → ${STATUS}" || error "Flask /health 异常: ${HEALTH}"

log "🔮 Flask /predict ..."
PREDICT=$(curl -s -X POST http://localhost:30800/predict \
    -H "Content-Type: application/json" -d '{"text":"This is absolutely amazing!"}')
LABEL=$(echo "$PREDICT" | python3 -c "import sys,json; print(json.load(sys.stdin)['label'])" 2>/dev/null || echo "?")
LATENCY=$(echo "$PREDICT" | python3 -c "import sys,json; print(json.load(sys.stdin)['latency_ms'])" 2>/dev/null || echo "?")
[ "$LABEL" != "?" ] && success "Flask /predict → ${LABEL}  ${LATENCY}ms" || warn "Flask /predict 解析失败: ${PREDICT}"

# ── Ray Serve 验证 ────────────────────────────────────────────
log "🔮 Ray /predict ..."
PREDICT_RAY=$(curl -s -X POST http://localhost:30801/predict \
    -H "Content-Type: application/json" -d '{"text":"Ray Serve is working!"}')
LABEL_RAY=$(echo "$PREDICT_RAY" | python3 -c "import sys,json; print(json.load(sys.stdin)['label'])" 2>/dev/null || echo "?")
LATENCY_RAY=$(echo "$PREDICT_RAY" | python3 -c "import sys,json; print(json.load(sys.stdin)['latency_ms'])" 2>/dev/null || echo "?")
[ "$LABEL_RAY" != "?" ] && success "Ray  /predict → ${LABEL_RAY}  ${LATENCY_RAY}ms" || warn "Ray /predict 解析失败: ${PREDICT_RAY}"

# ── Hubble 验证 ───────────────────────────────────────────────
log "👁️  生成流量并检查 Hubble L7 追踪..."
for _ in {1..3}; do
    curl -s -X POST http://localhost:30800/predict -H "Content-Type: application/json" \
        -d '{"text":"hubble test"}' > /dev/null
done
sleep 2
HUBBLE_FLOWS=$(hubble observe --server localhost:4245 --namespace ai-serving \
    --protocol http --last 20 2>/dev/null | grep -c "http-response" || echo "0")
if [ "${HUBBLE_FLOWS}" -gt 0 ]; then
    success "Hubble 已捕获 ${HUBBLE_FLOWS} 条 HTTP 响应 flow"
else
    warn "Hubble 暂未看到 HTTP flow（可稍后再试：hubble observe --server localhost:4245 --namespace ai-serving --protocol http）"
fi

# ══════════════════════════════════════════════════════════════
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || echo "localhost")

echo ""
echo -e "${BOLD}${GREEN}"
cat << 'EOF'
  ╔════════════════════════════════════════════════╗
  ║   🎉  部署成功！                               ║
  ╚════════════════════════════════════════════════╝
EOF
echo -e "${NC}"

echo -e "${BOLD}📡 AI 推理服务:${NC}"
echo -e "   Flask   NodePort: ${CYAN}http://localhost:30800${NC}   (外网: http://${NODE_IP}:30800)"
echo -e "   Ray     NodePort: ${CYAN}http://localhost:30801${NC}   (外网: http://${NODE_IP}:30801)"
echo ""
echo -e "${BOLD}👁️  Hubble 可观测性:${NC}"
echo -e "   Hubble UI:    ${CYAN}http://localhost:30880${NC}  （需开放 EC2 安全组 30880 端口）"
echo -e "   Hubble CLI:   ${YELLOW}hubble observe --server localhost:4245 --namespace ai-serving --protocol http${NC}"
echo -e "   延迟统计:     ${YELLOW}hubble observe --server localhost:4245 --namespace ai-serving --protocol http --last 100 -o json | python3 -c \"...\"${NC}"
echo ""
echo -e "${BOLD}📖 常用命令:${NC}"
echo -e "   健康检查:   ${YELLOW}curl http://localhost:30800/health${NC}"
echo -e "   情感分析:   ${YELLOW}curl -X POST http://localhost:30800/predict -H 'Content-Type: application/json' -d '{\"text\":\"Hello\"}'${NC}"
echo -e "   查看日志:   ${YELLOW}kubectl logs -f -n ai-serving -l app=ai-serving${NC}"
echo -e "   实时流量:   ${YELLOW}hubble observe --server localhost:4245 --namespace ai-serving --protocol http -f${NC}"
echo -e "   重启 pf:    ${YELLOW}kubectl port-forward -n kube-system svc/hubble-relay 4245:80 &${NC}"
echo ""
log "🕐 完成时间: $(date)"
