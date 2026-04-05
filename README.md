# AI Serving 快速实验方案

## 🎯 目标
在 AWS 免费服务器上搭建一个轻量 AI 推理服务，支持自动扩缩容，通过 Tailscale 内网访问。

---

## 架构总览

```
Client (内网)
    │
    │ Tailscale VPN
    ▼
┌─────────────────────────────────┐
│  AWS EC2 (t2.micro / t3.micro) │
│                                 │
│  ┌───────────────────────────┐  │
│  │  Ray Serve (port 8000)    │  │
│  │  ┌─────────────────────┐  │  │
│  │  │ Deployment:         │  │  │
│  │  │  - SentimentModel   │  │  │
│  │  │  - num_replicas: 1  │  │  │
│  │  │  - autoscaling:     │  │  │
│  │  │    min=1, max=3     │  │  │
│  │  └─────────────────────┘  │  │
│  └───────────────────────────┘  │
│                                 │
│  Tailscale (100.x.x.x)         │
└─────────────────────────────────┘
```

---

## 第一步：AWS EC2 准备

### 推荐配置
- **实例类型**: `t2.micro`（免费套餐）或 `t3.micro`
- **AMI**: Ubuntu 22.04 LTS
- **存储**: 20GB gp3（免费套餐含 30GB）
- **安全组**: 仅开放 SSH(22)，其余流量走 Tailscale

### 启动后基础设置

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv git curl
```

---

## 第二步：安装 Tailscale（内网穿透）

```bash
# 安装 Tailscale
curl -fsSL https://tailscale.com/install.sh | sh

# 登录（会给你一个链接去浏览器认证）
sudo tailscale up

# 查看你的 Tailscale IP
tailscale ip -4
# 输出类似: 100.64.x.x
```

---

## 第三步：搭建 AI Serving

### 方案 A：Ray Serve（推荐，自带 autoscaling）

```bash
# 创建虚拟环境
python3 -m venv ~/ai-serving
source ~/ai-serving/bin/activate

# 安装依赖（轻量版，适合 t2.micro 内存限制）
pip install "ray[serve]" transformers torch --no-cache-dir

# 如果内存不够装 torch，用 CPU 精简版：
# pip install "ray[serve]" transformers torch --index-url https://download.pytorch.org/whl/cpu
```

### 方案 B：Flask 轻量版（备选，内存更省）

```bash
python3 -m venv ~/ai-serving
source ~/ai-serving/bin/activate
pip install flask transformers torch gunicorn
```

---

## 第四步：代码实现

> 见项目中的具体文件
