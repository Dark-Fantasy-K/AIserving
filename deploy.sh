#!/bin/bash
# =============================================================
#  AIserving 一键部署脚本
#  仓库: https://github.com/Dark-Fantasy-K/AIserving
#  CNI:  Cilium (默认) + Hubble 流量可观测性
#  支持平台: k3s | gke
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

# ── 帮助信息 ──────────────────────────────────────────────────
usage() {
    cat << EOF
用法: $0 [选项]

选项:
  --platform <k3s|gke>       目标平台 (默认: k3s)
  --gke-project  <project>   GCP 项目 ID          (GKE 必填)
  --gke-cluster  <cluster>   GKE 集群名称         (GKE 必填)
  --gke-region   <region>    GKE 区域             (默认: us-central1)
  --registry     <url>       镜像仓库地址          (GKE 默认: gcr.io/<project>)
  --cilium-version <ver>     Cilium 版本           (默认: v1.16.5)
  --skip-build               跳过 Docker 镜像构建
  --skip-cilium              跳过 Cilium 安装
  -h, --help                 显示此帮助

示例:
  # k3s（本地单机）
  $0 --platform k3s

  # GKE（Google Kubernetes Engine）
  $0 --platform gke \\
     --gke-project my-gcp-project \\
     --gke-cluster my-cluster \\
     --gke-region  us-central1

  # GKE + 自定义镜像仓库（Artifact Registry）
  $0 --platform gke \\
     --gke-project my-gcp-project \\
     --gke-cluster my-cluster \\
     --registry    us-docker.pkg.dev/my-gcp-project/ai-serving
EOF
    exit 0
}

# ── 参数解析 ──────────────────────────────────────────────────
PLATFORM="k3s"
GKE_PROJECT=""
GKE_CLUSTER=""
GKE_REGION="us-central1"
REGISTRY=""
CILIUM_VERSION="${CILIUM_VERSION:-v1.16.5}"
SKIP_BUILD=false
SKIP_CILIUM=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --platform)        PLATFORM="$2";      shift 2 ;;
        --gke-project)     GKE_PROJECT="$2";   shift 2 ;;
        --gke-cluster)     GKE_CLUSTER="$2";   shift 2 ;;
        --gke-region)      GKE_REGION="$2";    shift 2 ;;
        --registry)        REGISTRY="$2";      shift 2 ;;
        --cilium-version)  CILIUM_VERSION="$2"; shift 2 ;;
        --skip-build)      SKIP_BUILD=true;    shift ;;
        --skip-cilium)     SKIP_CILIUM=true;   shift ;;
        -h|--help)         usage ;;
        *) error "未知参数: $1  (使用 --help 查看用法)" ;;
    esac
done

# ── 参数校验 ──────────────────────────────────────────────────
case "$PLATFORM" in
    k3s|gke) ;;
    *) error "不支持的平台: ${PLATFORM}，请指定 k3s 或 gke" ;;
esac

if [[ "$PLATFORM" == "gke" ]]; then
    [[ -z "$GKE_PROJECT" ]] && error "GKE 模式需要 --gke-project"
    [[ -z "$GKE_CLUSTER" ]] && error "GKE 模式需要 --gke-cluster"
    : "${REGISTRY:=gcr.io/${GKE_PROJECT}}"   # 默认使用 GCR
fi

# ── 路径常量 ──────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR/deployment"
FLASK_DIR="$DEPLOY_DIR/flask_ai"
RAY_DIR="$DEPLOY_DIR/ray_serve"

FLASK_LOCAL="ai-serving:latest"
RAY_LOCAL="ai-serving-ray:latest"

if [[ "$PLATFORM" == "gke" ]]; then
    FLASK_IMAGE="${REGISTRY}/ai-serving:latest"
    RAY_IMAGE="${REGISTRY}/ai-serving-ray:latest"
else
    FLASK_IMAGE="$FLASK_LOCAL"
    RAY_IMAGE="$RAY_LOCAL"
fi

# ── Banner ────────────────────────────────────────────────────
echo -e "${BOLD}"
cat << EOF
  ╔════════════════════════════════════════════════╗
  ║   🤖  AIserving 一键部署脚本                  ║
  ║   Flask + Ray Serve · ${PLATFORM^^} · Cilium · Hubble$(printf '%*s' $((13 - ${#PLATFORM})) '')║
  ╚════════════════════════════════════════════════╝
EOF
echo -e "${NC}"

log "📁 脚本目录:    $SCRIPT_DIR"
log "🚀 目标平台:    ${BOLD}${PLATFORM^^}${NC}"
[[ "$PLATFORM" == "gke" ]] && log "☁️  GCP 项目:    ${GKE_PROJECT}  集群: ${GKE_CLUSTER}  区域: ${GKE_REGION}"
[[ "$PLATFORM" == "gke" ]] && log "📦 镜像仓库:    ${REGISTRY}"
log "🕐 开始时间:    $(date)"

# ══════════════════════════════════════════════════════════════
# STEP 1  平台初始化
# ══════════════════════════════════════════════════════════════

if [[ "$PLATFORM" == "k3s" ]]; then

    step "🔧 Step 1/8  安装 k3s（Cilium CNI 模式）"
    # 关键参数：
    #   --flannel-backend=none     禁用默认 flannel，由 Cilium 接管 CNI
    #   --disable-network-policy   禁用 k3s 内置 NetworkPolicy，由 Cilium 管理
    #   --disable=traefik          禁用 Traefik，减少资源占用

    if command -v k3s &>/dev/null && systemctl is-active --quiet k3s; then
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

    log "⏳ 等待 k3s API server 就绪..."
    for i in {1..30}; do
        kubectl get nodes &>/dev/null && break
        sleep 3
    done
    success "k3s 安装完成，节点: $(kubectl get nodes --no-headers | awk '{print $1}')"

else  # gke

    step "☁️  Step 1/8  连接 GKE 集群"

    if ! command -v gcloud &>/dev/null; then
        error "未找到 gcloud CLI，请先安装 Google Cloud SDK: https://cloud.google.com/sdk/docs/install"
    fi

    log "🔑 获取 GKE 集群凭据..."
    gcloud container clusters get-credentials "${GKE_CLUSTER}" \
        --region "${GKE_REGION}" \
        --project "${GKE_PROJECT}"

    log "⏳ 验证集群连接..."
    kubectl cluster-info
    success "GKE 集群连接成功: ${GKE_CLUSTER} (${GKE_REGION})"

fi

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

if [[ "$SKIP_CILIUM" == true ]]; then
    warn "已指定 --skip-cilium，跳过 Cilium 安装"
else
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
        if [[ "$PLATFORM" == "k3s" ]]; then
            NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
            log "🚀 部署 Cilium ${CILIUM_VERSION}（节点 IP: ${NODE_IP}）..."
            cilium install \
                --version "${CILIUM_VERSION}" \
                --set k8sServiceHost="${NODE_IP}" \
                --set k8sServicePort=6443 \
                --set hubble.relay.enabled=true \
                --set hubble.ui.enabled=true \
                --set hubble.metrics.enableOpenMetrics=true \
                --set "hubble.metrics.enabled={dns,drop,tcp,flow,port-distribution,icmp,httpV2:exemplars=true;labelsContext=source_ip\,source_namespace\,source_workload\,destination_ip\,destination_namespace\,destination_workload\,traffic_direction}"
        else
            # GKE 模式：显式设置 cluster.name（≤32字符），避免 Cilium 使用过长的 context 名
            # Cilium 默认从 kubeconfig context 读取集群名，GKE context 格式为
            # gke_<project>_<region>_<cluster>，通常超过 32 字符限制
            CILIUM_CLUSTER_NAME="${GKE_CLUSTER:0:32}"
            log "🚀 部署 Cilium ${CILIUM_VERSION}（GKE 模式，cluster.name=${CILIUM_CLUSTER_NAME}）..."
            cilium install \
                --version "${CILIUM_VERSION}" \
                --set cluster.name="${CILIUM_CLUSTER_NAME}" \
                --set hubble.relay.enabled=true \
                --set hubble.ui.enabled=true \
                --set hubble.metrics.enableOpenMetrics=true \
                --set "hubble.metrics.enabled={dns,drop,tcp,flow,port-distribution,icmp,httpV2:exemplars=true;labelsContext=source_ip\,source_namespace\,source_workload\,destination_ip\,destination_namespace\,destination_workload\,traffic_direction}"
        fi
    fi

    log "⏳ 等待 Cilium 全部就绪（约 60-90s）..."
    for i in {1..60}; do
        STATUS=$(kubectl get nodes --no-headers 2>/dev/null | awk '{print $2}' | head -1)
        [[ "$STATUS" == "Ready" ]] && break
        sleep 3
    done
    kubectl rollout status daemonset/cilium -n kube-system --timeout=180s
    kubectl rollout status deployment/hubble-relay -n kube-system --timeout=120s
    kubectl rollout status deployment/hubble-ui -n kube-system --timeout=60s

    success "Cilium + Hubble 就绪"
    kubectl get pods -n kube-system -l 'k8s-app in (cilium,hubble-relay,hubble-ui)' --no-headers \
        | awk '{printf "   %-45s %s\n", $1, $3}'
fi

# ══════════════════════════════════════════════════════════════
if [[ "$PLATFORM" == "k3s" ]]; then
    step "🏗️  Step 4/8  构建 Docker 镜像（Flask + Ray Serve）"
else
    step "🏗️  Step 4/8  构建并推送 Docker 镜像（Flask + Ray Serve → ${REGISTRY}）"
fi
# ══════════════════════════════════════════════════════════════

if [[ "$SKIP_BUILD" == true ]]; then
    warn "已指定 --skip-build，跳过镜像构建与推送"
else
    if [[ "$PLATFORM" == "gke" ]]; then
        log "🔑 使用 gcloud access token 登录 Docker..."
        # gcloud auth configure-docker 只写入当前用户的 ~/.docker/config.json
        # sudo docker 以 root 运行，读不到普通用户凭据，需显式登录
        REGISTRY_HOST="${REGISTRY%%/*}"   # 取仓库域名，如 gcr.io 或 us-docker.pkg.dev
        gcloud auth print-access-token | \
            sudo docker login -u oauth2accesstoken --password-stdin "https://${REGISTRY_HOST}"
    fi

    # ── Flask ──────────────────────────────────────────────────
    log "📦 构建 Flask 镜像..."
    if [[ "$PLATFORM" == "gke" ]]; then
        sudo docker build -t "${FLASK_LOCAL}" "${FLASK_DIR}"
        sudo docker tag "${FLASK_LOCAL}" "${FLASK_IMAGE}"
        log "📤 推送 Flask 镜像 → ${FLASK_IMAGE}..."
        sudo docker push "${FLASK_IMAGE}"
        success "Flask 镜像构建并推送完成"
    else
        sudo docker build -t "${FLASK_IMAGE}" "${FLASK_DIR}"
        success "Flask 镜像构建完成"
    fi

    # ── Ray Serve ──────────────────────────────────────────────
    log "📦 构建 Ray Serve 镜像（含 PyTorch，约 3-5 分钟）..."
    if [[ "$PLATFORM" == "gke" ]]; then
        sudo docker build -t "${RAY_LOCAL}" "${RAY_DIR}"
        sudo docker tag "${RAY_LOCAL}" "${RAY_IMAGE}"
        log "📤 推送 Ray Serve 镜像 → ${RAY_IMAGE}..."
        sudo docker push "${RAY_IMAGE}"
        success "Ray Serve 镜像构建并推送完成"
    else
        sudo docker build -t "${RAY_IMAGE}" "${RAY_DIR}"
        success "Ray Serve 镜像构建完成"
    fi
fi

# ══════════════════════════════════════════════════════════════
if [[ "$PLATFORM" == "k3s" ]]; then
    step "📤 Step 5/8  导入镜像到 k3s containerd"
    # k3s 使用独立 containerd，与 Docker daemon 隔离，需手动 import

    log "📤 导入 Flask 镜像..."
    sudo docker save "${FLASK_IMAGE}" | sudo k3s ctr images import -

    log "📤 导入 Ray Serve 镜像..."
    sudo docker save "${RAY_IMAGE}" | sudo k3s ctr images import -

    for img in "${FLASK_IMAGE}" "${RAY_IMAGE}"; do
        if sudo k3s ctr images list 2>/dev/null | grep -q "${img%%:*}"; then
            success "已确认镜像: ${img}"
        else
            error "镜像导入验证失败: ${img}"
        fi
    done
else
    step "📤 Step 5/8  镜像就绪确认"
    if [[ "$SKIP_BUILD" == true ]]; then
        warn "已指定 --skip-build，假设镜像已提前推送至仓库"
        log "   Flask  : ${FLASK_IMAGE}"
        log "   Ray    : ${RAY_IMAGE}"
    else
        success "镜像已在 Step 4 构建完成并推送，Step 5 跳过"
    fi
fi

# ══════════════════════════════════════════════════════════════
step "☸️  Step 6/8  部署 Kubernetes 资源"
# ══════════════════════════════════════════════════════════════

if [[ "$PLATFORM" == "k3s" ]]; then
    log "📄 部署公共资源（namespace / PV / Flask）..."
    kubectl apply -f "${DEPLOY_DIR}/namespace.yaml"
    kubectl apply -f "${DEPLOY_DIR}/pv-model-cache.yaml"
    kubectl apply -f "${DEPLOY_DIR}/deployment.yaml"
    kubectl apply -f "${DEPLOY_DIR}/service.yaml"

    log "📄 部署 Ray Serve..."
    kubectl apply -f "${RAY_DIR}/deployment.yaml"
    kubectl apply -f "${RAY_DIR}/service.yaml"

else
    # GKE 模式：使用 deployment/gke/ overlay，动态替换镜像地址
    GKE_OVERLAY="${DEPLOY_DIR}/gke"

    log "📄 部署 namespace..."
    kubectl apply -f "${DEPLOY_DIR}/namespace.yaml"

    log "🔑 创建/刷新 GCR imagePullSecret..."
    kubectl create secret docker-registry gcr-pull-secret \
        --docker-server=gcr.io \
        --docker-username=oauth2accesstoken \
        --docker-password="$(gcloud auth print-access-token)" \
        --docker-email="$(gcloud config get-value account)" \
        -n ai-serving \
        --dry-run=client -o yaml | kubectl apply -f -

    log "📄 部署 GKE PVC（动态供应 GCE Persistent Disk）..."
    kubectl apply -f "${GKE_OVERLAY}/pvc-model-cache.yaml"

    log "📄 部署 Flask（替换镜像地址: ${FLASK_IMAGE}）..."
    sed "s|image: ai-serving:latest|image: ${FLASK_IMAGE}|g" \
        "${GKE_OVERLAY}/deployment.yaml" | kubectl apply -f -
    sed 's/type: NodePort/type: LoadBalancer/g' \
        "${DEPLOY_DIR}/service.yaml" | kubectl apply -f -

    log "📄 部署 Ray Serve（替换镜像地址: ${RAY_IMAGE}）..."
    sed "s|image: ai-serving-ray:latest|image: ${RAY_IMAGE}|g" \
        "${RAY_DIR}/deployment.yaml" | \
        sed 's/imagePullPolicy: Never/imagePullPolicy: IfNotPresent/g' | \
        kubectl apply -f -
    sed 's/type: NodePort/type: LoadBalancer/g' \
        "${RAY_DIR}/service.yaml" | kubectl apply -f -
fi

success "所有 Kubernetes 资源已创建"
kubectl get pods -n ai-serving

# ══════════════════════════════════════════════════════════════
step "🔍 Step 7/8  配置 Hubble L7 流量可见性"
# ══════════════════════════════════════════════════════════════

if [[ "$SKIP_CILIUM" == true ]]; then
    warn "已指定 --skip-cilium，跳过 Hubble 配置"
else
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

    if [[ "$PLATFORM" == "k3s" ]]; then
        # 暴露 Hubble UI（NodePort 30880）
        HUBBLE_TYPE=$(kubectl get svc hubble-ui -n kube-system \
            -o jsonpath='{.spec.type}' 2>/dev/null || echo "")
        if [ "${HUBBLE_TYPE}" != "NodePort" ]; then
            log "🌐 将 Hubble UI 暴露为 NodePort 30880..."
            kubectl patch svc hubble-ui -n kube-system \
                -p '{"spec":{"type":"NodePort","ports":[{"port":80,"targetPort":8081,"nodePort":30880}]}}'
        fi
    else
        # GKE 使用 LoadBalancer 暴露 Hubble UI
        HUBBLE_TYPE=$(kubectl get svc hubble-ui -n kube-system \
            -o jsonpath='{.spec.type}' 2>/dev/null || echo "")
        if [ "${HUBBLE_TYPE}" != "LoadBalancer" ]; then
            log "🌐 将 Hubble UI 暴露为 LoadBalancer（GKE）..."
            kubectl patch svc hubble-ui -n kube-system \
                -p '{"spec":{"type":"LoadBalancer","ports":[{"port":80,"targetPort":8081}]}}'
        fi
    fi

    # 启动 Hubble relay port-forward（后台，供 hubble CLI 使用）
    if ! pgrep -f "port-forward.*hubble-relay" &>/dev/null; then
        log "🔗 启动 Hubble relay port-forward (4245:80)..."
        kubectl port-forward -n kube-system svc/hubble-relay 4245:80 \
            &>/var/log/hubble-pf.log &
        sleep 3
    fi

    success "Hubble L7 可见性配置完成"
fi

# ══════════════════════════════════════════════════════════════
step "⏳ Step 8/8  等待服务就绪 & 验证"
# ══════════════════════════════════════════════════════════════

log "🔄 等待 Flask deployment 就绪（最长 5 分钟）..."
kubectl rollout status deployment/ai-serving -n ai-serving --timeout=300s
log "🔄 等待 Ray Serve deployment 就绪（最长 6 分钟）..."
kubectl rollout status deployment/ai-serving-ray -n ai-serving --timeout=360s

log "📋 最终 Pod 状态:"
kubectl get pods -n ai-serving -o wide

# ── 确定访问地址 ──────────────────────────────────────────────
if [[ "$PLATFORM" == "k3s" ]]; then
    FLASK_URL="http://localhost:30800"
    RAY_URL="http://localhost:30801"
    NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || echo "localhost")
    HUBBLE_UI_URL="http://localhost:30880"
else
    log "⏳ 等待 GKE LoadBalancer 分配外部 IP（最长 5 分钟）..."
    for i in {1..30}; do
        FLASK_EXT=$(kubectl get svc ai-serving -n ai-serving \
            -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "")
        RAY_EXT=$(kubectl get svc ai-serving-ray -n ai-serving \
            -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "")
        [[ -n "$FLASK_EXT" && -n "$RAY_EXT" ]] && break
        printf "   等待 LoadBalancer IP 分配 %d/30...\r" "$i"
        sleep 10
    done
    echo ""
    FLASK_PORT=$(kubectl get svc ai-serving -n ai-serving \
        -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || echo "8000")
    RAY_PORT=$(kubectl get svc ai-serving-ray -n ai-serving \
        -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || echo "8000")
    FLASK_URL="http://${FLASK_EXT:-<pending>}:${FLASK_PORT}"
    RAY_URL="http://${RAY_EXT:-<pending>}:${RAY_PORT}"
    NODE_IP="${FLASK_EXT:-<pending>}"

    HUBBLE_EXT=$(kubectl get svc hubble-ui -n kube-system \
        -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "<pending>")
    HUBBLE_UI_URL="http://${HUBBLE_EXT}"
fi

# 等待 HTTP 健康检查
log "⏳ 等待 Flask 健康检查通过..."
for i in {1..30}; do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${FLASK_URL}/health" 2>/dev/null || true)
    [ "$HTTP_CODE" = "200" ] && break
    printf "   尝试 %d/30 (HTTP %s)...\r" "$i" "$HTTP_CODE"
    sleep 5
done
echo ""

# ── Flask 验证 ────────────────────────────────────────────────
log "🏥 Flask /health ..."
HEALTH=$(curl -s "${FLASK_URL}/health")
STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "?")
[ "$STATUS" = "healthy" ] && success "Flask /health → ${STATUS}" || error "Flask /health 异常: ${HEALTH}"

log "🔮 Flask /predict ..."
PREDICT=$(curl -s -X POST "${FLASK_URL}/predict" \
    -H "Content-Type: application/json" -d '{"text":"This is absolutely amazing!"}')
LABEL=$(echo "$PREDICT" | python3 -c "import sys,json; print(json.load(sys.stdin)['label'])" 2>/dev/null || echo "?")
LATENCY=$(echo "$PREDICT" | python3 -c "import sys,json; print(json.load(sys.stdin)['latency_ms'])" 2>/dev/null || echo "?")
[ "$LABEL" != "?" ] && success "Flask /predict → ${LABEL}  ${LATENCY}ms" || warn "Flask /predict 解析失败: ${PREDICT}"

# ── Ray Serve 验证 ────────────────────────────────────────────
log "🔮 Ray /predict ..."
PREDICT_RAY=$(curl -s -X POST "${RAY_URL}/predict" \
    -H "Content-Type: application/json" -d '{"text":"Ray Serve is working!"}')
LABEL_RAY=$(echo "$PREDICT_RAY" | python3 -c "import sys,json; print(json.load(sys.stdin)['label'])" 2>/dev/null || echo "?")
LATENCY_RAY=$(echo "$PREDICT_RAY" | python3 -c "import sys,json; print(json.load(sys.stdin)['latency_ms'])" 2>/dev/null || echo "?")
[ "$LABEL_RAY" != "?" ] && success "Ray  /predict → ${LABEL_RAY}  ${LATENCY_RAY}ms" || warn "Ray /predict 解析失败: ${PREDICT_RAY}"

# ── Hubble 验证 ───────────────────────────────────────────────
if [[ "$SKIP_CILIUM" == false ]]; then
    log "👁️  生成流量并检查 Hubble L7 追踪..."
    for _ in {1..3}; do
        curl -s -X POST "${FLASK_URL}/predict" -H "Content-Type: application/json" \
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
fi

# ══════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}${GREEN}"
cat << 'EOF'
  ╔════════════════════════════════════════════════╗
  ║   🎉  部署成功！                               ║
  ╚════════════════════════════════════════════════╝
EOF
echo -e "${NC}"

echo -e "${BOLD}📡 AI 推理服务:${NC}"
echo -e "   Flask   : ${CYAN}${FLASK_URL}${NC}"
echo -e "   Ray     : ${CYAN}${RAY_URL}${NC}"
if [[ "$PLATFORM" == "k3s" ]]; then
    echo -e "   (外网)  Flask: http://${NODE_IP}:30800   Ray: http://${NODE_IP}:30801"
fi
echo ""
if [[ "$SKIP_CILIUM" == false ]]; then
    echo -e "${BOLD}👁️  Hubble 可观测性:${NC}"
    echo -e "   Hubble UI:  ${CYAN}${HUBBLE_UI_URL}${NC}"
    echo -e "   Hubble CLI: ${YELLOW}hubble observe --server localhost:4245 --namespace ai-serving --protocol http${NC}"
    echo ""
fi
echo -e "${BOLD}📖 常用命令:${NC}"
echo -e "   健康检查:   ${YELLOW}curl ${FLASK_URL}/health${NC}"
echo -e "   情感分析:   ${YELLOW}curl -X POST ${FLASK_URL}/predict -H 'Content-Type: application/json' -d '{\"text\":\"Hello\"}'${NC}"
echo -e "   查看日志:   ${YELLOW}kubectl logs -f -n ai-serving -l app=ai-serving${NC}"
if [[ "$SKIP_CILIUM" == false ]]; then
    echo -e "   实时流量:   ${YELLOW}hubble observe --server localhost:4245 --namespace ai-serving --protocol http -f${NC}"
    echo -e "   重启 pf:    ${YELLOW}kubectl port-forward -n kube-system svc/hubble-relay 4245:80 &${NC}"
fi
echo ""
log "🕐 完成时间: $(date)"
