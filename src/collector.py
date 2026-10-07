"""
collector.py — 5G Core Network Function API Consumer.

This module provides the CollectorService class that polls each 5GCN Network
Function via its SBI (Service-Based Interface) OpenAPI endpoint, parses the
response, and returns structured Python dictionaries for the dashboard.

3GPP References:
  - TS 29.510: NRF NF Management (nnrf-nfm)
  - TS 29.518: AMF Communication (namf-comm)
  - TS 29.502: SMF PDU Session (nsmf-pdusession)

When a real 5G Core is unavailable, the collector transparently falls back to
a built-in Simulator that generates statistically plausible mock data so the
dashboard UI remains fully functional for demonstrations and testing.
"""

import time
import random
import logging
from datetime import datetime, timezone
from typing import Any

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from config import (
    NRF_BASE_URL, NRF_NF_INSTANCES_PATH,
    AMF_BASE_URL, AMF_REGISTERED_UES,
    SMF_BASE_URL, SMF_PDU_SESSIONS,
    UPF_BASE_URL, UPF_METRICS_PATH,
    REQUEST_TIMEOUT_SEC, NF_REGISTRY,
)
from metrics import measure_latency, MetricsStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Simulator — generates realistic 5G Core mock data when NFs are unreachable
# ---------------------------------------------------------------------------

# Predefined UE pool (SUPI = Subscription Permanent Identifier, format imsi-MCCMNC...)
_UE_POOL = [
    {"supi": f"imsi-724070000000{i:03d}", "guti": f"72407000{i:05d}",
     "state": random.choice(["REGISTERED", "REGISTERED", "REGISTERED", "IDLE"]),
     "ran_id": f"gNB-{random.randint(1,4):02d}", "slice": "01-000001",
     "registered_at": datetime.now(timezone.utc).isoformat()}
    for i in range(1, 51)
]

_PDU_SESSION_POOL = [
    {"sm_context_ref": f"ctx-{i:04d}",
     "supi": f"imsi-724070000000{random.randint(1, 50):03d}",
     "pdu_session_id": (i % 15) + 1,
     "dnn": random.choice(["internet", "ims", "iot"]),
     "s_nssai": {"sst": 1, "sd": "000001"},
     "upf_node_id": f"upf{random.randint(1,2)}.local",
     "ipv4_address": f"10.45.{random.randint(0,5)}.{random.randint(2,254)}",
     "ambr_ul_kbps": random.randint(5_000, 100_000),
     "ambr_dl_kbps": random.randint(10_000, 150_000),
     "state": "ACTIVE"}
    for i in range(1, 31)
]

_NF_TYPES = ["AMF", "SMF", "UPF", "PCF", "UDM", "AUSF", "NSSF", "BSF"]


class Simulator:
    """
    Generates statistically plausible 5G Core data when NFs are unreachable.
    Introduces small random deltas per refresh to simulate live traffic.
    """

    @staticmethod
    def nrf_instances() -> list[dict]:
        """Return simulated NF instance list as per TS 29.510."""
        return [
            {
                "nfInstanceId": f"nf-{nf_type.lower()}-01",
                "nfType": nf_type,
                "nfStatus": "REGISTERED",
                "ipv4Addresses": ["127.0.0.1"],
                "nfServices": [{"serviceName": f"n{nf_type.lower()}-svc", "versions": [{"apiVersionInUri": "v1"}]}],
                "load": random.randint(5, 80),
                "capacity": 100,
            }
            for nf_type in _NF_TYPES
        ]

    @staticmethod
    def amf_ue_contexts() -> list[dict]:
        """Return simulated UE context list (subset, with small variation)."""
        sample_size = random.randint(35, 50)
        pool = random.sample(_UE_POOL, sample_size)
        return pool

    @staticmethod
    def smf_pdu_sessions() -> list[dict]:
        """Return simulated PDU Session (SM Context) list per TS 29.502."""
        sample_size = random.randint(20, 30)
        return random.sample(_PDU_SESSION_POOL, sample_size)

    @staticmethod
    def upf_metrics() -> dict:
        """Return simulated UPF telemetry (throughput, packet counters)."""
        return {
            "upf_packets_forwarded_total":  {"ul": random.randint(800_000, 2_000_000),
                                              "dl": random.randint(1_000_000, 5_000_000)},
            "upf_bytes_forwarded_total":    {"ul": random.randint(500_000_000, 2_000_000_000),
                                              "dl": random.randint(1_000_000_000, 8_000_000_000)},
            "upf_active_sessions":           random.randint(18, 30),
            "upf_drop_rate_percent":         round(random.uniform(0, 0.5), 4),
            "upf_throughput_ul_mbps":        round(random.uniform(10, 200), 2),
            "upf_throughput_dl_mbps":        round(random.uniform(50, 800), 2),
        }

    @staticmethod
    def system_kpis() -> dict:
        """Aggregate KPI summary for the dashboard hero metrics."""
        return {
            "registered_ues":   random.randint(35, 50),
            "active_pdu":       random.randint(20, 30),
            "nfs_online":       len(_NF_TYPES),
            "avg_latency_ms":   round(random.uniform(1.5, 8.0), 2),
        }


# ---------------------------------------------------------------------------
# CollectorService — main API consumer with latency instrumentation
# ---------------------------------------------------------------------------

class CollectorService:
    """
    Polls each 5G Core NF endpoint, measures SBI call latency, stores metrics,
    and falls back to the Simulator on connectivity failures.

    Attributes:
        metrics_store: shared MetricsStore instance for latency logging.
        use_simulator: if True, bypass real HTTP calls (offline demo mode).
    """

    def __init__(self, metrics_store: MetricsStore, use_simulator: bool = False):
        self.metrics_store = metrics_store
        self.use_simulator = use_simulator
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, nf_name: str, url: str) -> tuple[Any, float, bool]:
        """
        Perform a timed HTTP GET.

        Returns:
            (parsed_json_or_text, latency_ms, success_flag)
        """
        t0 = time.perf_counter()
        try:
            resp = self._session.get(url, timeout=REQUEST_TIMEOUT_SEC)
            latency_ms = (time.perf_counter() - t0) * 1000
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            self.metrics_store.record_latency(nf_name, latency_ms, success=True)
            logger.debug("[%s] GET %s → %d (%.1f ms)", nf_name, url, resp.status_code, latency_ms)
            return data, latency_ms, True
        except requests.exceptions.RequestException as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            self.metrics_store.record_latency(nf_name, latency_ms, success=False)
            logger.warning("[%s] Unreachable — %s (%.1f ms)", nf_name, exc, latency_ms)
            return None, latency_ms, False

    # ------------------------------------------------------------------
    # Public collectors
    # ------------------------------------------------------------------

    def get_nrf_instances(self) -> dict:
        """
        Query the NRF for all registered NF instances.
        Endpoint: GET /nnrf-nfm/v1/nf-instances  (3GPP TS 29.510)

        Returns dict with keys: data (list), latency_ms, simulated (bool).
        """
        if self.use_simulator:
            lat = round(random.uniform(0.5, 2.0), 2)
            self.metrics_store.record_latency("NRF", lat, success=True)
            return {"data": Simulator.nrf_instances(), "latency_ms": lat, "simulated": True}

        url = NRF_BASE_URL + NRF_NF_INSTANCES_PATH
        raw, lat, ok = self._get("NRF", url)
        if ok and raw:
            instances = raw if isinstance(raw, list) else raw.get("nfInstances", raw)
            return {"data": instances, "latency_ms": lat, "simulated": False}
        # Fallback to simulator
        return {"data": Simulator.nrf_instances(), "latency_ms": lat, "simulated": True}

    def get_amf_ue_contexts(self) -> dict:
        """
        Query the AMF for registered UE contexts.
        In Open5GS the webui exposes subscriber list at /api/v1/subscribers.
        Endpoint: GET /namf-comm/v1/ue-contexts  (3GPP TS 29.518)
        """
        if self.use_simulator:
            lat = round(random.uniform(0.5, 3.0), 2)
            self.metrics_store.record_latency("AMF", lat, success=True)
            return {"data": Simulator.amf_ue_contexts(), "latency_ms": lat, "simulated": True}

        # Try Open5GS WebUI subscriber API first
        url = f"http://{AMF_BASE_URL.split('://')[1].split(':')[0]}:3000/api/v1/subscribers"
        raw, lat, ok = self._get("AMF", url)
        if ok and raw:
            return {"data": raw if isinstance(raw, list) else [], "latency_ms": lat, "simulated": False}
        return {"data": Simulator.amf_ue_contexts(), "latency_ms": lat, "simulated": True}

    def get_smf_pdu_sessions(self) -> dict:
        """
        Query the SMF for active PDU Sessions (SM Contexts).
        Endpoint: GET /nsmf-pdusession/v1/sm-contexts  (3GPP TS 29.502)
        """
        if self.use_simulator:
            lat = round(random.uniform(0.5, 3.0), 2)
            self.metrics_store.record_latency("SMF", lat, success=True)
            return {"data": Simulator.smf_pdu_sessions(), "latency_ms": lat, "simulated": True}

        url = SMF_BASE_URL + SMF_PDU_SESSIONS
        raw, lat, ok = self._get("SMF", url)
        if ok and raw:
            sessions = raw if isinstance(raw, list) else []
            return {"data": sessions, "latency_ms": lat, "simulated": False}
        return {"data": Simulator.smf_pdu_sessions(), "latency_ms": lat, "simulated": True}

    def get_upf_metrics(self) -> dict:
        """
        Scrape UPF Prometheus metrics endpoint for data-plane statistics.
        Endpoint: GET /metrics  (Prometheus text exposition format)
        """
        if self.use_simulator:
            lat = round(random.uniform(0.5, 2.0), 2)
            self.metrics_store.record_latency("UPF", lat, success=True)
            return {"data": Simulator.upf_metrics(), "latency_ms": lat, "simulated": True}

        url = UPF_BASE_URL + UPF_METRICS_PATH
        raw, lat, ok = self._get("UPF", url)
        if ok:
            parsed = _parse_prometheus_text(raw) if isinstance(raw, str) else Simulator.upf_metrics()
            return {"data": parsed, "latency_ms": lat, "simulated": False}
        return {"data": Simulator.upf_metrics(), "latency_ms": lat, "simulated": True}

    def check_nf_health(self) -> dict[str, dict]:
        """
        Probe each NF health endpoint and return online/offline status.
        Used for the dashboard status badges.
        """
        status: dict[str, dict] = {}
        for nf_name, meta in NF_REGISTRY.items():
            if self.use_simulator:
                lat = round(random.uniform(0.5, 5.0), 2)
                self.metrics_store.record_latency(nf_name, lat, success=True)
                status[nf_name] = {"online": True, "latency_ms": lat, "simulated": True}
                continue
            url = meta["base_url"] + meta["health_path"]
            _, lat, ok = self._get(nf_name, url)
            status[nf_name] = {"online": ok, "latency_ms": round(lat, 2), "simulated": False}
        return status

    def collect_all(self) -> dict:
        """
        Full data collection pass — calls all NF endpoints and returns
        a single unified snapshot for the dashboard to render.
        """
        ts = datetime.now(timezone.utc).isoformat()
        nrf   = self.get_nrf_instances()
        amf   = self.get_amf_ue_contexts()
        smf   = self.get_smf_pdu_sessions()
        upf   = self.get_upf_metrics()
        health = self.check_nf_health()

        return {
            "timestamp":    ts,
            "nrf":          nrf,
            "amf":          amf,
            "smf":          smf,
            "upf":          upf,
            "nf_health":    health,
            "kpis": {
                "registered_ues": len(amf["data"]),
                "active_pdu":     len(smf["data"]),
                "nfs_online":     sum(1 for v in health.values() if v["online"]),
                "avg_latency_ms": round(
                    sum(v["latency_ms"] for v in health.values()) / max(len(health), 1), 2
                ),
            },
        }


# ---------------------------------------------------------------------------
# Prometheus text format mini-parser
# ---------------------------------------------------------------------------

def _parse_prometheus_text(text: str) -> dict:
    """
    Minimal parser for Prometheus text exposition format (# HELP / # TYPE lines + metric lines).
    Returns a flat dict of {metric_name: float_value}.
    """
    result: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            metric_name = parts[0].split("{")[0]   # strip label set
            try:
                result[metric_name] = float(parts[-1])
            except ValueError:
                pass
    return result
