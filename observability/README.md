[English](#observability-stack) | [中文](#可观测性栈)

---

# Observability Stack

Distributed tracing, metrics collection, and visualization for the YOLO Microservices Pipeline.

## Components

| Component | Image | Port | Role |
|-----------|-------|------|------|
| Jaeger | `jaegertracing/all-in-one:1.57` | 16686 (UI) | Distributed trace storage and query |
| OTel Collector | `otel/opentelemetry-collector-contrib:0.101.0` | 4317 (OTLP gRPC), 8889 (Prometheus scrape) | Receives OTLP from services, fans out to Jaeger + Prometheus |
| Prometheus | `prom/prometheus:v2.52.0` | 9090 | Scrapes metrics from OTel Collector every 15 s |
| Grafana | `grafana/grafana:10.4.3` | 3000 | Dashboard and trace visualization |

## Data Flow

```
Python services
      │  OTLP gRPC (traces + metrics)
      ▼
OTel Collector :4317
      ├── traces ──► Jaeger :4317
      └── metrics ─► Prometheus scrape endpoint :8889
                              │
                         Prometheus :9090
                              │
                          Grafana :3000
```

## Starting the Stack

```bash
# Start only the observability containers (services run locally)
docker compose up jaeger otel-collector prometheus grafana

# Start everything together
docker compose up
```

## Running Python Services Locally

Each service must export to the OTel Collector. Set `OTEL_EXPORTER_OTLP_ENDPOINT` before starting:

```bash
# vehicle
cd services/vehicle-service
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 python server.py

# pedestrian (separate terminal)
cd services/pedestrian-service
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 python server.py

# router (separate terminal)
cd services/router-service
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 python server.py

# gateway (separate terminal)
cd services/gateway
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 python server.py
```

Confirm the env var is active — the service log should show:

```
[otel] traces → http://localhost:4317
```

## Access URLs

| UI | URL | Credentials |
|----|-----|-------------|
| Grafana | http://localhost:3000 | admin / admin |
| Prometheus | http://localhost:9090 | — |
| Jaeger | http://localhost:16686 | — |

## Grafana Dashboard

The dashboard **AI Serving Pipeline** is provisioned automatically on startup (no manual import needed).

| Panel | Metric |
|-------|--------|
| Avg Latency (stat, ×4) | Per-service average over the last 1 min |
| Request Rate (QPS) | `rate(…_count[1m])` for all four services |
| Gateway Latency Percentiles | P50 / P95 / P99 for HTTP response and end-to-end pipeline |
| Router Latency Percentiles | Total detect latency vs. YOLO inference |
| Pedestrian Latency Percentiles | Total vs. pose inference |
| Vehicle Latency Percentiles | Total vs. IoU tracking |
| Traces (Jaeger) | Live trace search across all services |

Metrics use the names defined in `telemetry.py` (dots replaced with underscores):

```
gateway_response_time_ms   pipeline_transaction_time_ms
router_response_time_ms    router_processing_time_ms
pedestrian_response_time_ms  pedestrian_processing_time_ms
vehicle_response_time_ms   vehicle_processing_time_ms
```

## Configuration Files

```
observability/
├── otel-collector-config.yaml          # Receiver / exporter / pipeline config
├── prometheus.yml                      # Scrape targets
└── grafana/
    └── provisioning/
        ├── datasources/default.yml     # Prometheus + Jaeger datasources (auto)
        └── dashboards/
            ├── default.yml             # Dashboard file-provider config
            └── pipeline.json           # AI Serving Pipeline dashboard
```

## Troubleshooting

**Metrics empty in Grafana / Prometheus**

1. Confirm services are sending to the Collector:
   ```bash
   curl -s http://localhost:8889/metrics | grep gateway_response_time
   ```
2. If the env var was not set, restart services with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`.
3. Metrics are pushed every 60 s by default; wait one interval after the first request.

**Metric names have `_milliseconds` suffix**

The OTel Collector prometheus exporter is configured with `add_metric_suffixes: false` to keep names consistent with the code. If you see `_milliseconds` suffixes, restart the Collector:
```bash
docker compose restart otel-collector
```

**Jaeger shows no traces**

Verify the Collector is forwarding traces:
```bash
docker logs otel-collector | grep "data_type"
```
Both `traces` and `metrics` pipelines should appear.

---

# 可观测性栈

YOLO 微服务 Pipeline 的分布式追踪、指标采集与可视化方案。

## 组件

| 组件 | 镜像 | 端口 | 职责 |
|------|------|------|------|
| Jaeger | `jaegertracing/all-in-one:1.57` | 16686 (UI) | 分布式 trace 存储与查询 |
| OTel Collector | `otel/opentelemetry-collector-contrib:0.101.0` | 4317 (OTLP gRPC), 8889 (Prometheus scrape) | 接收服务 OTLP 数据，分发给 Jaeger 和 Prometheus |
| Prometheus | `prom/prometheus:v2.52.0` | 9090 | 每 15 秒 scrape OTel Collector 的指标 |
| Grafana | `grafana/grafana:10.4.3` | 3000 | Dashboard 与 trace 可视化 |

## 数据流

```
Python 各服务
      │  OTLP gRPC（traces + metrics）
      ▼
OTel Collector :4317
      ├── traces ──► Jaeger :4317
      └── metrics ─► Prometheus scrape 端点 :8889
                              │
                         Prometheus :9090
                              │
                          Grafana :3000
```

## 启动观测栈

```bash
# 只启动观测容器（服务本地运行）
docker compose up jaeger otel-collector prometheus grafana

# 全部一起启动
docker compose up
```

## 本地启动 Python 服务

每个服务必须通过环境变量指向 OTel Collector：

```bash
# vehicle
cd services/vehicle-service
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 python server.py

# pedestrian（另一个终端）
cd services/pedestrian-service
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 python server.py

# router（另一个终端）
cd services/router-service
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 python server.py

# gateway（另一个终端）
cd services/gateway
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 python server.py
```

服务日志里出现以下内容说明配置生效：

```
[otel] traces → http://localhost:4317
```

## 访问地址

| 界面 | 地址 | 登录信息 |
|------|------|----------|
| Grafana | http://localhost:3000 | admin / admin |
| Prometheus | http://localhost:9090 | — |
| Jaeger | http://localhost:16686 | — |

## Grafana Dashboard

Dashboard **AI Serving Pipeline** 在容器启动时自动加载，无需手动 import。

| 面板 | 指标 |
|------|------|
| Avg Latency（stat，×4） | 各服务过去 1 分钟平均时延 |
| Request Rate (QPS) | 四个服务的 `rate(…_count[1m])` |
| Gateway Latency Percentiles | HTTP 响应和全链路 P50/P95/P99 |
| Router Latency Percentiles | 总时延 vs. YOLO 推理时延 |
| Pedestrian Latency Percentiles | 总时延 vs. Pose 推理时延 |
| Vehicle Latency Percentiles | 总时延 vs. IoU 追踪时延 |
| Traces (Jaeger) | 跨服务实时 trace 搜索 |

指标名称来自 `telemetry.py`，点号转为下划线：

```
gateway_response_time_ms   pipeline_transaction_time_ms
router_response_time_ms    router_processing_time_ms
pedestrian_response_time_ms  pedestrian_processing_time_ms
vehicle_response_time_ms   vehicle_processing_time_ms
```

## 配置文件

```
observability/
├── otel-collector-config.yaml          # receiver / exporter / pipeline 配置
├── prometheus.yml                      # scrape 目标
└── grafana/
    └── provisioning/
        ├── datasources/default.yml     # Prometheus + Jaeger 数据源（自动）
        └── dashboards/
            ├── default.yml             # Dashboard 文件提供器配置
            └── pipeline.json           # AI Serving Pipeline dashboard
```

## 常见问题

**Grafana / Prometheus 指标为空**

1. 确认服务正在向 Collector 推送数据：
   ```bash
   curl -s http://localhost:8889/metrics | grep gateway_response_time
   ```
2. 如果环境变量未设置，带上变量重启服务：`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`
3. 指标默认每 60 秒推送一次，第一次请求后等待一个周期。

**指标名出现 `_milliseconds` 后缀**

OTel Collector 已配置 `add_metric_suffixes: false`，保证名称与代码一致。若仍出现该后缀，重启 Collector：
```bash
docker compose restart otel-collector
```

**Jaeger 没有 trace**

确认 Collector 正在转发 traces：
```bash
docker logs otel-collector | grep "data_type"
```
日志中应同时出现 `traces` 和 `metrics` 两条 pipeline 启动记录。
