# 5G Core Observability and Analytics Dashboard

> **Graduate Program in Applied Computing — UNISINOS**
> Mobile Computing Systems Development · Prof. Dr. Cristiano Bonato Both

A real-time observability and analytics dashboard for 5G Standalone Core networks, built on Service-Based Interfaces (SBI) and OpenAPI specifications (3GPP Rel-16).

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│  Streamlit Dashboard (src/app.py)                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │  KPI Cards  │  │  Latency    │  │  UE / PDU       │  │
│  │  NF Health  │  │  Charts     │  │  Session Tables │  │
│  └─────────────┘  └─────────────┘  └─────────────────┘  │
└──────────────┬────────────────────────────────────────────┘
               │ HTTP/REST (SBI)
┌──────────────▼────────────────────────────────────────────┐
│  CollectorService (src/collector.py)                      │
│  ┌─────┐  ┌─────┐  ┌─────┐  ┌─────┐  ┌─────┐  ┌─────┐  │
│  │ NRF │  │ AMF │  │ SMF │  │ UPF │  │ PCF │  │ UDM │  │
│  └──┬──┘  └──┬──┘  └──┬──┘  └──┬──┘  └──┬──┘  └──┬──┘  │
└─────┼────────┼────────┼────────┼─────────┼────────┼──────┘
      │        │  Docker / WSL 2 │         │        │
┌─────▼────────▼────────▼────────▼─────────▼────────▼──────┐
│  Open5GS 5G Core (docker/docker-compose.5gcore.yml)       │
│  NRF · AMF · SMF · UPF · PCF · UDM · AUSF · MongoDB      │
└──────────────────────────────────────────────────────────┘
```

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | ≥ 3.11 | WSL 2 Ubuntu or Windows |
| Docker Desktop | ≥ 4.28 | WSL 2 backend enabled |
| pip | latest | `pip install --upgrade pip` |

## Quick Start

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 2. (Optional) Start the 5G Core containers
```bash
cd docker
docker compose -f docker-compose.5gcore.yml up -d
# Wait ~30s for all NFs to register with NRF
docker compose -f docker-compose.5gcore.yml ps
```

### 3. Launch the dashboard
```bash
streamlit run src/app.py
# Opens http://localhost:8501
```

> **No 5G Core?** Enable **Simulator Mode** in the sidebar (on by default).
> The dashboard will generate realistic synthetic data for all NFs.

## Project Structure

```
5g-core-dashboard/
├── docker/
│   ├── docker-compose.5gcore.yml   # Open5GS multi-NF stack
│   └── config/
│       └── prometheus.yml          # Prometheus scrape targets
├── src/
│   ├── config.py                   # SBI endpoints & configuration
│   ├── collector.py                # NF API consumer + Simulator
│   ├── metrics.py                  # Latency / CPU / RAM tracking
│   └── app.py                      # Streamlit dashboard UI
├── tests/
│   └── test_collector.py           # Unit + integration tests
├── data/
│   ├── performance_logs.csv        # Auto-generated latency log
│   └── exports/                    # Downloaded reports
├── requirements.txt
└── README.md
```

## Running Tests

```bash
# All tests (simulator mode — no Docker needed)
pytest tests/ -v

# Skip integration tests that require a live 5G Core
pytest tests/ -v -k "not integration"

# With coverage report
pytest tests/ -v --tb=short --cov=src
```

## Key Metrics Collected

| Metric | Source | Export |
|--------|--------|--------|
| SBI call latency (ms) | Per NF HTTP call | CSV |
| Success rate (%) | Per NF HTTP call | CSV |
| P95 latency | Rolling window | CSV |
| Active PDU Sessions | SMF API | CSV |
| Registered UEs | AMF / WebUI API | CSV |
| UPF throughput (Mbps) | Prometheus /metrics | Chart |
| Host CPU % / RAM MB | psutil | Chart |
| Container CPU % / RAM MB | Docker API | Table |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CORE_HOST` | `127.0.0.1` | 5G Core host IP |
| `REFRESH_INTERVAL` | `5` | Dashboard auto-refresh (seconds) |
| `REQUEST_TIMEOUT` | `4` | HTTP timeout per NF call |
| `NRF_URL` | `http://127.0.0.1:7777` | NRF SBI base URL |
| `AMF_URL` | `http://127.0.0.1:7779` | AMF SBI base URL |
| `SMF_URL` | `http://127.0.0.1:7780` | SMF SBI base URL |
| `UPF_URL` | `http://127.0.0.1:9090` | UPF Prometheus URL |

## 3GPP References

- **TS 29.500** — 5G System; Technical Realization of Service Based Architecture
- **TS 29.510** — Network Function Repository Services (NRF nnrf-nfm)
- **TS 29.518** — Access and Mobility Management Services (AMF namf-comm)
- **TS 29.502** — Session Management Services (SMF nsmf-pdusession)
- **TS 29.244** — Interface between the Control Plane and the User Plane Nodes (PFCP)

## License

Academic project — PPGCA UNISINOS, 2024. All rights reserved.
