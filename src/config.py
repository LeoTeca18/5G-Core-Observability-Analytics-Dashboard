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

All URLs point to localhost ports mapped by docker/docker-compose.5gcore.yml.

Transport notes (verified against Open5GS 2.7.0):
  * SBI = HTTP/2 cleartext with *prior knowledge* (h2c). HTTP/1.1 clients are
    rejected by nghttp2 ("bad client magic byte string"), therefore the
    collector uses ``httpx`` with ``http1=False, http2=True``.
  * Prometheus /metrics endpoints (AMF/SMF/UPF/PCF) are plain HTTP/1.1.
"""

import os

# ---------------------------------------------------------------------------
# Host resolution — override via environment variables for CI / containers
# ---------------------------------------------------------------------------
CORE_HOST = os.getenv("CORE_HOST", "127.0.0.1")

# Host-side port mapping (see docker-compose.5gcore.yml header table)
SBI_PORTS: dict[str, int] = {
    "NRF": int(os.getenv("NRF_PORT", "7777")),
    "AMF": int(os.getenv("AMF_PORT", "7779")),
    "SMF": int(os.getenv("SMF_PORT", "7780")),
    "PCF": int(os.getenv("PCF_PORT", "7781")),
    "UDM": int(os.getenv("UDM_PORT", "7782")),
}
METRICS_PORTS: dict[str, int] = {
    "AMF": int(os.getenv("AMF_METRICS_PORT", "9091")),
    "SMF": int(os.getenv("SMF_METRICS_PORT", "9092")),
    "UPF": int(os.getenv("UPF_METRICS_PORT", "9093")),
    "PCF": int(os.getenv("PCF_METRICS_PORT", "9094")),
}
MONGO_PORT = int(os.getenv("MONGO_PORT", "27017"))

# ---------------------------------------------------------------------------
# 3GPP SBI API paths (TS 29.510 / TS 29.518 / TS 29.502)
# ---------------------------------------------------------------------------
NRF_NF_INSTANCES_PATH = "/nnrf-nfm/v1/nf-instances"          # TS 29.510 NFManagement
NRF_DISCOVERY_PATH    = "/nnrf-disc/v1/nf-instances"         # TS 29.510 NFDiscovery
METRICS_PATH          = "/metrics"                           # Prometheus exposition format

# ---------------------------------------------------------------------------
# Dashboard behaviour
# ---------------------------------------------------------------------------
REFRESH_INTERVAL_SEC = int(os.getenv("REFRESH_INTERVAL", "5"))
REQUEST_TIMEOUT_SEC  = float(os.getenv("REQUEST_TIMEOUT", "3"))
DB_TIMEOUT_MS        = int(os.getenv("DB_TIMEOUT_MS", "1500"))

# Docker containers considered part of the 5G Core / RAN for resource metrics
CORE_CONTAINER_PREFIXES = tuple(
    os.getenv("CORE_CONTAINER_PREFIXES", "open5gs-,ueransim-,prometheus").split(",")
)

# ---------------------------------------------------------------------------
# Data / logging paths
# ---------------------------------------------------------------------------
DATA_DIR         = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
PERF_LOG_CSV     = os.path.join(DATA_DIR, "performance_logs.csv")   # per-call SBI latency
RESOURCE_LOG_CSV = os.path.join(DATA_DIR, "resource_logs.csv")      # CPU / RAM per source
TELEMETRY_CSV    = os.path.join(DATA_DIR, "telemetry_log.csv")      # one row per refresh cycle
EXPORT_DIR       = os.path.join(DATA_DIR, "exports")


# ---------------------------------------------------------------------------
# NF registry — built dynamically so the Core host can be changed at runtime
# ---------------------------------------------------------------------------
_NF_META: dict[str, dict] = {
    "NRF": {"label": "Network Repository Function",          "emoji": "🗂️", "color": "#818cf8",
            "health_path": NRF_NF_INSTANCES_PATH + "?limit=1"},
    "AMF": {"label": "Access & Mobility Management Function", "emoji": "📡", "color": "#34d399",
            "health_path": "/namf-comm/v1/"},
    "SMF": {"label": "Session Management Function",           "emoji": "🔗", "color": "#fbbf24",
            "health_path": "/nsmf-pdusession/v1/"},
    "UPF": {"label": "User Plane Function",                   "emoji": "🚀", "color": "#f87171",
            "health_path": METRICS_PATH},                      # UPF has no SBI → probe /metrics
    "PCF": {"label": "Policy Control Function",               "emoji": "📋", "color": "#38bdf8",
            "health_path": "/npcf-am-policy-control/v1/"},
    "UDM": {"label": "Unified Data Management",               "emoji": "🗄️", "color": "#a3e635",
            "health_path": "/nudm-sdm/v2/"},
}

NF_ORDER = ["NRF", "AMF", "SMF", "UPF", "PCF", "UDM"]
NF_COLORS = {nf: meta["color"] for nf, meta in _NF_META.items()}


def build_nf_registry(host: str = CORE_HOST) -> dict[str, dict]:
    """
    Return the NF registry for a given Core host.

    Each entry contains:
      sbi_url     — base URL of the NF SBI (HTTP/2 h2c) or None (UPF)
      metrics_url — Prometheus endpoint (HTTP/1.1) or None
      health_url  — URL used for the heartbeat probe
      health_proto— "h2" (SBI) or "http1" (metrics)
    """
    registry: dict[str, dict] = {}
    for nf in NF_ORDER:
        meta = dict(_NF_META[nf])
        sbi_port = SBI_PORTS.get(nf)
        met_port = METRICS_PORTS.get(nf)
        meta["sbi_url"] = os.getenv(f"{nf}_URL", f"http://{host}:{sbi_port}") if sbi_port else None
        meta["metrics_url"] = f"http://{host}:{met_port}{METRICS_PATH}" if met_port else None
        if meta["sbi_url"]:
            meta["health_url"] = meta["sbi_url"] + meta["health_path"]
            meta["health_proto"] = "h2"
        else:
            meta["health_url"] = meta["metrics_url"]
            meta["health_proto"] = "http1"
        registry[nf] = meta
    return registry


def build_mongo_uri(host: str = CORE_HOST) -> str:
    """MongoDB URI of the Open5GS subscriber database (UDR backend)."""
    return os.getenv("MONGO_URI", f"mongodb://{host}:{MONGO_PORT}/open5gs")


# Backwards-compatible module-level registry (default host)
NF_REGISTRY: dict = build_nf_registry(CORE_HOST)
NRF_BASE_URL = NF_REGISTRY["NRF"]["sbi_url"]
