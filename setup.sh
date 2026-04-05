#!/bin/bash
# ============================================================
# AI Serving Lab - 一键部署脚本
# 在 AWS EC2 (Ubuntu 22.04) 上运行
# ============================================================

set -e

echo "╔══════════════════════════════════════════╗"
echo "║  AI Serving Lab - 一键部署               ║"
echo "╚══════════════════════════════════════════╝"

# ------------------------------------------------------------
# 1. 系统依赖
# ------------------------------------------------------------
echo ""
echo "[1/5] 安装系统依赖..."
sudo apt update -qq
sudo apt install -y python3-pip python3-venv curl

# ------------------------------------------------------------
# 2. Tailscale
# ------------------------------------------------------------
echo ""
echo "[2/5] 安装 Tailscale..."
if ! command -v tailscale &> /dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
    echo ">>> 请运行: sudo tailscale up"
    echo ">>> 然后在浏览器中完成认证"
else
    echo "Tailscale 已安装"
    TSIP=$(tailscale ip -4 2>/dev/null || echo "未连接")
    echo "Tailscale IP: $TSIP"
fi

# ------------------------------------------------------------
# 3. Python 虚拟环境
# ------------------------------------------------------------
echo ""
echo "[3/5] 创建 Python 环境..."
python3 -m venv ~/ai-serving-env
source ~/ai-serving-env/bin/activate

# ------------------------------------------------------------
# 4. 选择方案安装依赖
# ------------------------------------------------------------
echo ""
echo "请选择部署方案:"
echo "  1) Ray Serve（推荐，自带 autoscaling）"
echo "  2) Flask + Gunicorn（更轻量）"
read -p "选择 [1/2]: " CHOICE

if [ "$CHOICE" = "2" ]; then
    echo ""
    echo "[4/5] 安装 Flask 依赖..."
    pip install flask gunicorn psutil requests \
        torch --index-url https://download.pytorch.org/whl/cpu \
        transformers --no-cache-dir
    
    echo ""
    echo "[5/5] 启动 Flask 服务..."
    echo ">>> 运行命令:"
    echo "    source ~/ai-serving-env/bin/activate"
    echo "    gunicorn -c gunicorn_config.py flask_app:app"
    echo ""
    echo ">>> 或开发模式:"
    echo "    python flask_app.py"
else
    echo ""
    echo "[4/5] 安装 Ray Serve 依赖..."
    pip install "ray[serve]" requests \
        torch --index-url https://download.pytorch.org/whl/cpu \
        transformers --no-cache-dir
    
    echo ""
    echo "[5/5] 启动 Ray Serve..."
    echo ">>> 运行命令:"
    echo "    source ~/ai-serving-env/bin/activate"
    echo "    python ray_serve_app.py"
fi

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  部署完成!                                    ║"
echo "║                                              ║"
echo "║  测试:                                        ║"
echo "║  curl -X POST http://localhost:8000/predict \\ ║"
echo "║    -H 'Content-Type: application/json' \\     ║"
echo "║    -d '{\"text\": \"I love this!\"}'             ║"
echo "║                                              ║"
echo "║  压力测试:                                     ║"
echo "║  python load_test.py                          ║"
echo "║                                              ║"
echo "║  从其他 Tailscale 设备访问:                    ║"
echo "║  curl http://<TAILSCALE_IP>:8000/health       ║"
echo "╚══════════════════════════════════════════════╝"
