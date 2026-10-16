# 5G Core Observability and Analytics Dashboard

A real-time observability, telemetry, and health monitoring dashboard for 5G Standalone Core networks (5G SA Core), built on the 3GPP Release 16 Service-Based Architecture (SBA), HTTP/2 Service-Based Interfaces (SBI), and Prometheus exposition standards.

---

## 🏛️ System Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│  Streamlit Dashboard (src/app.py)                                      │
│  ┌────────────────┐ ┌────────────────┐ ┌────────────────┐ ┌─────────┐ │
│  │   KPI Cards    │ │  Status NFs    │ │ SBI Latency &  │ │ Tables  │ │
│  │ UEs/PDU/NFs/Lat│ │ (Online/Off)   │ │  Host CPU/RAM  │ │ UE/PDU  │ │
│  └────────────────┘ └────────────────┘ └────────────────┘ └─────────┘ │
│  ┌───────────────────────────────────────────────────────────────────┐ │
│  │   Telemetry Export (.csv) & Infrastructure Resource Metrics       │ │
│  └───────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTP/2 (SBI) + HTTP/1.1 (Metrics)
┌───────────────────────────────────▼────────────────────────────────────┐
│  Collection Backend (src/collector.py & src/metrics.py)                │
│  - Async HTTP/2 Client (h2c with Prior Knowledge for Open5GS/free5GC)  │
│  - Prometheus Scraper (TS 28.552 Metrics: AMF, SMF, UPF, PCF)          │
│  - MongoDB Connector (Provisioned Subscribers / UDR backend)           │
│  - System Resources: psutil (Dashboard/Host) + Docker SDK (Containers) │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Docker Bridge Network (172.25.0.0/24)
┌───────────────────────────────────▼────────────────────────────────────┐
│  5G Core Stack (docker/docker-compose.5gcore.yml)                      │
│  - NRF, SCP, AMF, SMF, UPF, PCF, UDM, UDR, AUSF, NSSF, BSF, MongoDB   │
│  - Optional RAN Profile: UERANSIM gNB + UEs for Load Testing           │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 🧰 Technology Stack

| Layer | Technology | Role in Project |
|---|---|---|
| **OS & Virtualization** | Windows 11 with WSL 2 / Docker Desktop | Linux microservice containerization and orchestration |
| **5G Core Engine** | Open5GS 2.7.0 (3GPP Rel-16) | Standards-compliant SBI OpenAPI endpoints and real NF telemetry |
| **RAN Simulator** | UERANSIM 3.2.6 (`nr-gnb` + `nr-ue`) | Simulates gNodeB and generates realistic UE traffic / PDU sessions |
| **Programming Language**| Python 3.11+ (Python 3.12 supported) | Data collection pipeline, instrumentation, and dashboard frontend |
| **Backend / Collector** | `httpx[http2]`, `pymongo`, `psutil`, `docker` | Async HTTP/2 calls to NFs, MongoDB queries, and host/container CPU/RAM |
| **Frontend Dashboard** | Streamlit, Plotly, Pandas | Real-time reactive web interface, responsive charts, and data tables |
| **Version Control** | Git & GitHub | Source code tracking and technical report empirical data persistence |

---

## 🚀 Quick Start Guide

### 1. Prerequisites
- **Python 3.11+** installed locally
- **Docker Desktop** (with WSL 2 integration enabled on Windows)

### 2. Install Python Dependencies
```bash
pip install -r requirements.txt
```

### 3. Launch the 5G Core Stack

#### Option A: Full 5G Core + Load Testing with Simulated UEs (Recommended)
Starts all 5G Core control and user plane NFs along with **UERANSIM** (20 provisioned UEs registering with the AMF and establishing active PDU sessions with the UPF):
```bash
cd docker
docker compose -f docker-compose.5gcore.yml --profile ran up -d
```

#### Option B: Standalone 5G Core (without RAN traffic)
```bash
cd docker
docker compose -f docker-compose.5gcore.yml up -d
```

Check the health and status of all microservices:
```bash
docker compose -f docker-compose.5gcore.yml ps
```

### 4. Launch the Observability Dashboard
From the root of the repository:
```bash
streamlit run src/app.py
```
Open your browser at `http://localhost:8501`.

> 💡 **Offline / Fallback Note:** If Docker or the 5G Core containers are not running, toggle **"Simulator Mode"** in the sidebar. The dashboard generates realistic synthetic data conforming to 3GPP patterns without crashing.

---

## 📋 Implemented Core Features

1. **Network Function Health Monitoring (NF Health):**
   - High-contrast visual grid (🟢 Online, 🔴 Offline, 🟡 Simulated) for key microservices: **AMF, SMF, UPF, PCF, UDM, NRF**.
   - Probes SBI health endpoints via HTTP/2 and cross-references registration profiles in the **NRF**.

2. **Subscriber & Session Analytics:**
   - Real-time registered UE counter (via AMF 3GPP TS 28.552 metrics and MongoDB subscriber DB).
   - Active PDU sessions count and data tunnels on the UPF partitioned by Data Network Name (DNN) and Network Slice (S-NSSAI).
   - Interactive tables for Registered Subscribers and Active PDU Sessions.

3. **Control Plane Observability (SBI Latency):**
   - Real-time Plotly time-series tracking response times (milliseconds) for every NF call.
   - Comprehensive performance statistics table (Mean, Min, Max, P50, P95, P99, and Success Rate %).

4. **System & Infrastructure Resource Metrics:**
   - Real-time CPU % and RAM (MB) monitoring for the dashboard process and total host machine via `psutil`.
   - Detailed container resource table via Docker SDK inspecting CPU % and RAM MB across all 5G Core containers.

5. **Telemetry & Empirical Data Export (CSV):**
   - Three one-click export buttons on the UI for technical report generation:
     - `Download Full Telemetry (CSV)`: Cycle-by-cycle dataset containing KPIs, latencies, and resource consumption.
     - `Download SBI Latency Log (CSV)`: Granular log of each individual HTTP/2 request per NF.
     - `Download CPU/RAM Resource Log (CSV)`: Host, dashboard, and container utilization records.

6. **Fault Tolerance & Resilient Error Handling:**
   - Robust non-blocking error handling: if an NF or MongoDB becomes unreachable, clear English warning banners appear (*"5G Core Connection Failure"* or *"Core Database Connection Failure"*), preventing interface crashes.

---

## 🧪 Automated Test Suite

The project includes an end-to-end test suite covering unit tests, Prometheus parsing, MetricsStore telemetry, and live integration against running 5G Core containers:

```bash
# Run all tests (including live integration if containers are online)
pytest tests/ -v

# Run only unit/simulator tests (no Docker required)
pytest tests/ -v -k "not integration"
```

---

## 📚 3GPP Technical References

- **3GPP TS 29.500:** 5G System; Technical Realization of Service Based Architecture (SBA)
- **3GPP TS 29.510:** Network Function Repository Services (NRF)
- **3GPP TS 29.518:** Access and Mobility Management Services (AMF)
- **3GPP TS 29.502:** Session Management Services (SMF)
- **3GPP TS 29.244:** Interface between Control Plane and User Plane Nodes (PFCP / UPF)
- **3GPP TS 28.552:** Management and Orchestration; 5G Performance Measurements

---

## 📄 License

Academic project developed for the Graduate Program in Applied Computing (PPGCA), UNISINOS, 2026. All rights reserved.
