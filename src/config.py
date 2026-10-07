"""
config.py — Central configuration for the 5G Core Observability Dashboard.

This module defines the SBI (Service-Based Interface) endpoint URLs for each
Network Function (NF) in the 5G Core (5GCN) as specified by 3GPP TS 29.500.

Network Functions covered:
  - NRF  (Network Repository Function): NF discovery and registration
  - AMF  (Access and Mobility Management Function): UE registration/state
  - SMF  (Session Management Function): PDU Session lifecycle
  - UPF  (User Plane Function): data-plane statistics (via Prometheus metrics)
  - PCF  (Policy Control Function): policy data
  - UDM  (Unified Data Management): subscriber data

All URLs point to localhost ports mapped by docker-compose.5gcore.yml.
Adjust HOST / ports to match your Open5GS or free5GC deployment.
"""

import os

# ---------------------------------------------------------------------------
# Host resolution — override via environment variables for CI / containers
# ---------------------------------------------------------------------------
CORE_HOST = os.getenv("CORE_HOST", "127.0.0.1")

# ---------------------------------------------------------------------------
# Open5GS REST API base URLs (management/metrics APIs)
# Reference: https://open5gs.org/open5gs/docs/
# ---------------------------------------------------------------------------
NRF_BASE_URL  = os.getenv("NRF_URL",  f"http://{CORE_HOST}:7777")   # NRF SBI
AMF_BASE_URL  = os.getenv("AMF_URL",  f"http://{CORE_HOST}:7779")   # AMF SBI (metrics)
SMF_BASE_URL  = os.getenv("SMF_URL",  f"http://{CORE_HOST}:7780")   # SMF SBI (metrics)
UPF_BASE_URL  = os.getenv("UPF_URL",  f"http://{CORE_HOST}:9090")   # UPF Prometheus metrics
PCF_BASE_URL  = os.getenv("PCF_URL",  f"http://{CORE_HOST}:7781")
UDM_BASE_URL  = os.getenv("UDM_URL",  f"http://{CORE_HOST}:7782")

# Open5GS MongoDB management REST gateway (open5gs-webui backend)
WEBUI_BASE_URL = os.getenv("WEBUI_URL", f"http://{CORE_HOST}:3000")

# ---------------------------------------------------------------------------
# 3GPP SBI API paths (TS 29.510 / TS 29.518 / TS 29.502)
# ---------------------------------------------------------------------------
NRF_NF_INSTANCES_PATH = "/nnrf-nfm/v1/nf-instances"
AMF_REGISTERED_UES    = "/namf-comm/v1/ue-contexts"
SMF_PDU_SESSIONS      = "/nsmf-pdusession/v1/sm-contexts"
UPF_METRICS_PATH      = "/metrics"   # Prometheus exposition format

# ---------------------------------------------------------------------------
# Dashboard behaviour
# ---------------------------------------------------------------------------
REFRESH_INTERVAL_SEC = int(os.getenv("REFRESH_INTERVAL", "5"))
REQUEST_TIMEOUT_SEC  = int(os.getenv("REQUEST_TIMEOUT", "4"))

# ---------------------------------------------------------------------------
# Data / logging paths
# ---------------------------------------------------------------------------
DATA_DIR     = os.path.join(os.path.dirname(__file__), "..", "data")
PERF_LOG_CSV = os.path.join(DATA_DIR, "performance_logs.csv")
EXPORT_DIR   = os.path.join(DATA_DIR, "exports")

# ---------------------------------------------------------------------------
# NF display metadata (name, icon emoji, colour hint for UI)
# ---------------------------------------------------------------------------
NF_REGISTRY: dict = {
    "NRF": {
        "label": "Network Repository Function",
        "emoji": "🗂️",
        "base_url": NRF_BASE_URL,
        "health_path": "/nnrf-nfm/v1/nf-instances?nf-type=AMF&limit=1",
        "color": "#6C63FF",
    },
    "AMF": {
        "label": "Access & Mobility Management Function",
        "emoji": "📡",
        "base_url": AMF_BASE_URL,
        "health_path": "/",
        "color": "#00C9A7",
    },
    "SMF": {
        "label": "Session Management Function",
        "emoji": "🔗",
        "base_url": SMF_BASE_URL,
        "health_path": "/",
        "color": "#F7B731",
    },
    "UPF": {
        "label": "User Plane Function",
        "emoji": "🚀",
        "base_url": UPF_BASE_URL,
        "health_path": "/metrics",
        "color": "#FC5C65",
    },
    "PCF": {
        "label": "Policy Control Function",
        "emoji": "📋",
        "base_url": PCF_BASE_URL,
        "health_path": "/",
        "color": "#45AAF2",
    },
    "UDM": {
        "label": "Unified Data Management",
        "emoji": "🗄️",
        "base_url": UDM_BASE_URL,
        "health_path": "/",
        "color": "#26DE81",
    },
}

NF_ORDER = ["NRF", "AMF", "SMF", "UPF", "PCF", "UDM"]
