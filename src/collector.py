"""
collector.py — 5G Core Network Function API Consumer.

This module provides the CollectorService class that polls each 5GCN Network
Function via its SBI (Service-Based Interface) OpenAPI endpoint, parses the
response, and returns a unified snapshot for the dashboard.

Transport Architecture:
  * SBI calls use `httpx.AsyncClient(http1=False, http2=True)`:
    - 3GPP TS 29.500 §5.2 mandates HTTP/2 on the Service-Based Interface.
    - Open5GS/free5GC daemons implement HTTP/2 cleartext (h2c) with *prior knowledge*.
    - HTTP/1.1 client handshakes are rejected by nghttp2 server with "bad client magic byte string".
    - Queries across all NFs (NRF, AMF, SMF, PCF, UDM) run concurrently via `asyncio.gather`.
  * Prometheus exposition endpoints (/metrics on AMF, SMF, UPF, PCF) are plain HTTP/1.1.
  * Subscriber profiles and provisioning data are queried from Open5GS MongoDB (UDR backend).

3GPP Specification References:
  - TS 29.500: Technical Realization of the Service Based Architecture (SBA)
  - TS 29.510: NRF NF Management (nnrf-nfm) & NF Discovery (nnrf-disc)
  - TS 29.518: AMF Communication Service (namf-comm)
  - TS 29.502: SMF PDU Session Service (nsmf-pdusession)
  - TS 28.552: 5G performance measurements (fivegs_* metrics counters)

Operating Modes:
  * use_simulator=True:
      Generates statistically plausible synthetic telemetry (for offline demos & testing).
  * use_simulator=False:
      Connects to live 5G Core containers. If unreachable, produces explicit error flags
      and marks NFs OFFLINE without crashing the interface.
"""

import asyncio
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

sys.path.insert(0, os.path.dirname(__file__))

from config import (                                   # noqa: E402
    CORE_HOST, NF_ORDER, NRF_NF_INSTANCES_PATH, REQUEST_TIMEOUT_SEC, DB_TIMEOUT_MS,
    build_nf_registry, build_mongo_uri,
)
from metrics import MetricsStore, measure_latency     # noqa: E402,F401

logger = logging.getLogger(__name__)

# Standard English user-facing error messages
CORE_DOWN_MSG = "5G Core Connection Failure — No Network Function responded on the SBI."
DB_DOWN_MSG   = "Core Database Connection Failure (MongoDB unreachable)."

# ---------------------------------------------------------------------------
# Simulator — Generates realistic 5G Core mock data when requested
# ---------------------------------------------------------------------------

# Predefined UE pool (SUPI = Subscription Permanent Identifier, PLMN MCC 999 / MNC 70)
_UE_POOL = [
    {"supi": f"imsi-99970{i:010d}", "guti": f"99970{i:05d}",
     "state": random.choice(["REGISTERED", "REGISTERED", "REGISTERED", "IDLE"]),
     "ran_id": f"gNB-{random.randint(1, 4):02d}", "slice": "SST 1",
     "dnn": "internet",
     "registered_at": datetime.now(timezone.utc).isoformat()}
    for i in range(1, 51)
]

# Predefined PDU Session pool representing active SM contexts
_PDU_SESSION_POOL = [
    {"sm_context_ref": f"ctx-{i:04d}",
     "supi": f"imsi-99970{random.randint(1, 50):010d}",
     "pdu_session_id": (i % 15) + 1,
     "dnn": random.choice(["internet", "ims", "iot"]),
     "s_nssai": "SST 1",
     "upf_node_id": f"upf{random.randint(1, 2)}.local",
     "ipv4_address": f"10.45.{random.randint(0, 5)}.{random.randint(2, 254)}",
     "ambr_ul_kbps": random.randint(5_000, 100_000),
     "ambr_dl_kbps": random.randint(10_000, 150_000),
     "state": "ACTIVE"}
    for i in range(1, 31)
]

_NF_TYPES = ["NRF", "SCP", "AMF", "SMF", "PCF", "UDM", "UDR", "AUSF", "NSSF", "BSF"]


class Simulator:
    """
    Generates statistically plausible 5G Core data for demonstrations.
    Introduces natural fluctuations per refresh to simulate live network traffic.
    """

    @staticmethod
    def nrf_instances() -> list[dict]:
        """Return simulated NF registration profiles conforming to 3GPP TS 29.510."""
        return [
            {
                "nfInstanceId": f"nf-{nf_type.lower()}-01",
                "nfType": nf_type,
                "nfStatus": "REGISTERED",
                "ipv4Addresses": f"172.25.0.{10 + i}",
                "heartBeatTimer": 10,
                "load": random.randint(5, 80),
                "capacity": 100,
            }
            for i, nf_type in enumerate(_NF_TYPES)
        ]

    @staticmethod
    def amf_ue_contexts() -> list[dict]:
        """Return simulated UE contexts (subset with dynamic variation)."""
        return random.sample(_UE_POOL, random.randint(35, 50))

    @staticmethod
    def smf_pdu_sessions() -> list[dict]:
        """Return simulated PDU Sessions (SM Contexts) per 3GPP TS 29.502."""
        return random.sample(_PDU_SESSION_POOL, random.randint(20, 30))

    @staticmethod
    def upf_metrics() -> dict:
        """Return simulated User Plane Function telemetry (throughput, packet rates)."""
        return {
            "active_sessions":    random.randint(18, 30),
            "throughput_dl_mbps": round(random.uniform(50, 800), 2),
            "throughput_ul_mbps": round(random.uniform(10, 200), 2),
            "n3_in_pps":          round(random.uniform(1_000, 9_000), 1),
            "n3_out_pps":         round(random.uniform(1_000, 9_000), 1),
            "qos_flows_by_dnn":   {"internet": 20},
            "drop_rate_percent":  round(random.uniform(0, 0.5), 4),
        }

    @staticmethod
    def system_kpis() -> dict:
        """Aggregate KPI summary for the dashboard hero metric cards."""
        return {
            "registered_ues": random.randint(35, 50),
            "active_pdu":     random.randint(20, 30),
            "nfs_online":     len(NF_ORDER),
            "avg_latency_ms": round(random.uniform(1.5, 8.0), 2),
        }


# ---------------------------------------------------------------------------
# Prometheus text format parser utilities
# ---------------------------------------------------------------------------

def _parse_prometheus_text(text: str) -> dict:
    """
    Parse Prometheus text exposition format into a dictionary.
    Metric values with identical names across label sets are summed.
    """
    result: dict[str, Any] = {}
    for name, _labels, value in _parse_prometheus_samples(text):
        result[name] = result.get(name, 0.0) + value
    return result


def _parse_prometheus_samples(text: str) -> list[tuple[str, dict, float]]:
    """Parse text into a list of tuples: (metric_name, labels_dict, float_value)."""
    samples: list[tuple[str, dict, float]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if "{" in line:
                name, rest = line.split("{", 1)
                label_str, value_str = rest.rsplit("}", 1)
                labels = {}
                for pair in label_str.split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        labels[k.strip()] = v.strip().strip('"')
            else:
                name, value_str = line.split()[:2]
                labels = {}
            samples.append((name.strip(), labels, float(value_str.split()[0])))
        except (ValueError, IndexError):
            continue
    return samples


# ---------------------------------------------------------------------------
# CollectorService — Core API Consumer & Latency Instrumenter
# ---------------------------------------------------------------------------

class CollectorService:
    """
    Orchestrates asynchronous polling of 5G Core Network Functions via HTTP/2,
    scrapes Prometheus metrics, measures SBI round-trip latencies, and compiles
    a unified telemetry snapshot for the Streamlit dashboard.

    Attributes:
        metrics_store: Thread-safe MetricsStore for persistence and stats.
        use_simulator: When True, uses synthetic simulation data.
        core_host    : IP or hostname where 5G Core container ports are mapped.
    """

    def __init__(self, metrics_store: MetricsStore, use_simulator: bool = False,
                 core_host: str = CORE_HOST, mongo_uri: Optional[str] = None):
        self.metrics_store = metrics_store
        self.use_simulator = use_simulator
        self.core_host = core_host
        self._mongo_uri_override = mongo_uri
        self.registry = build_nf_registry(core_host)
        self._prev_counters: dict[str, tuple[float, float]] = {}

    def set_core_host(self, host: str) -> None:
        """Update the target 5G Core host and rebuild endpoint URLs."""
        host = (host or CORE_HOST).strip()
        if host != self.core_host:
            self.core_host = host
            self.registry = build_nf_registry(host)
            self._prev_counters.clear()

    @property
    def mongo_uri(self) -> str:
        return self._mongo_uri_override or build_mongo_uri(self.core_host)

    async def _timed_get(self, client: httpx.AsyncClient, nf: str, url: str,
                         operation: str) -> dict:
        """
        Execute a timed HTTP GET request and record latency in the MetricsStore.

        Note on 5G Core HTTP semantics:
        Any response status < 500 proves the microservice is active and responding
        (e.g., bare service endpoints often return 400 Bad Request or 404 Not Found
        according to 3GPP ProblemDetails specifications, confirming NF liveness).
        """
        t0 = time.perf_counter()
        try:
            resp = await client.get(url)
            latency_ms = (time.perf_counter() - t0) * 1000
            ok = resp.status_code < 500
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            self.metrics_store.record_latency(
                nf_name=nf,
                latency_ms=latency_ms,
                success=ok,
                operation=operation,
                status_code=resp.status_code
            )
            return {"data": data, "latency_ms": latency_ms, "ok": ok,
                    "status_code": resp.status_code, "error": None if ok else f"HTTP {resp.status_code}"}
        except (httpx.HTTPError, OSError) as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            self.metrics_store.record_latency(
                nf_name=nf,
                latency_ms=latency_ms,
                success=False,
                operation=operation
            )
            logger.warning("[%s] %s unreachable — %s", nf, url, type(exc).__name__)
            return {"data": None, "latency_ms": latency_ms, "ok": False,
                    "status_code": None, "error": type(exc).__name__}

    async def _heartbeat(self, h2: httpx.AsyncClient, h1: httpx.AsyncClient, nf: str) -> dict:
        """Probe an NF's designated health endpoint using its supported protocol."""
        meta = self.registry[nf]
        client = h2 if meta["health_proto"] == "h2" else h1
        res = await self._timed_get(client, nf, meta["health_url"], "heartbeat")
        return {
            "online": res["ok"],
            "latency_ms": round(res["latency_ms"], 2),
            "status_code": res["status_code"],
            "error": res["error"],
            "simulated": False
        }

    async def _nrf_profiles(self, h2: httpx.AsyncClient) -> list[dict]:
        """
        Query NRF for registered NF instances (TS 29.510 NFManagement) and
        retrieve individual NF profiles concurrently over HTTP/2.
        """
        base = self.registry["NRF"]["sbi_url"]
        listing = await self._timed_get(h2, "NRF", base + NRF_NF_INSTANCES_PATH, "nf-list")
        if not listing["ok"] or not isinstance(listing["data"], dict):
            return []
        hrefs = [item.get("href", "") for item in listing["data"].get("_links", {}).get("items", [])]
        paths = [h[h.find("/nnrf-nfm"):] for h in hrefs if "/nnrf-nfm" in h]

        results = await asyncio.gather(*[
            self._timed_get(h2, "NRF", base + p, "nf-profile") for p in paths
        ])
        profiles = []
        for r in results:
            if r["ok"] and isinstance(r["data"], dict):
                d = r["data"]
                profiles.append({
                    "nfType": d.get("nfType"),
                    "nfInstanceId": d.get("nfInstanceId"),
                    "nfStatus": d.get("nfStatus"),
                    "ipv4Addresses": ", ".join(d.get("ipv4Addresses", [])),
                    "heartBeatTimer": d.get("heartBeatTimer"),
                    "load": d.get("load"),
                    "capacity": d.get("capacity"),
                })
        return sorted(profiles, key=lambda p: (p["nfType"] or ""))

    async def _prometheus(self, h1: httpx.AsyncClient, nf: str) -> Optional[list]:
        """Scrape Prometheus /metrics exposition endpoint for a specific NF."""
        url = self.registry[nf].get("metrics_url")
        if not url:
            return None
        res = await self._timed_get(h1, nf, url, "metrics")
        if res["ok"] and isinstance(res["data"], str):
            return _parse_prometheus_samples(res["data"])
        return None

    def _read_subscribers(self) -> tuple[list[dict], Optional[str]]:
        """Query provisioned subscriber data from Open5GS MongoDB (blocking thread)."""
        try:
            from pymongo import MongoClient
            from pymongo.errors import PyMongoError
        except ImportError:
            return [], "pymongo library not installed"

        t0 = time.perf_counter()
        try:
            client = MongoClient(
                self.mongo_uri,
                serverSelectionTimeoutMS=DB_TIMEOUT_MS,
                connectTimeoutMS=DB_TIMEOUT_MS
            )
            try:
                docs = list(client.get_default_database("open5gs").subscribers.find(
                    {}, {"_id": 0, "imsi": 1, "slice": 1, "ambr": 1, "subscriber_status": 1}
                ).limit(5000))
            finally:
                client.close()
            self.metrics_store.record_latency(
                "MongoDB", (time.perf_counter() - t0) * 1000,
                success=True, operation="subscribers"
            )
        except PyMongoError as exc:
            self.metrics_store.record_latency(
                "MongoDB", (time.perf_counter() - t0) * 1000,
                success=False, operation="subscribers"
            )
            logger.warning("MongoDB unreachable: %s", type(exc).__name__)
            return [], DB_DOWN_MSG

        rows = []
        for d in docs:
            sl = (d.get("slice") or [{}])[0]
            sess = (sl.get("session") or [{}])[0]
            ambr = d.get("ambr", {})
            rows.append({
                "supi": f"imsi-{d.get('imsi')}",
                "slice": f"SST {sl.get('sst', '-')}" + (f" / SD {sl['sd']}" if sl.get("sd") else ""),
                "dnn": sess.get("name", "-"),
                "ambr_dl": _fmt_bitrate(ambr.get("downlink")),
                "ambr_ul": _fmt_bitrate(ambr.get("uplink")),
                "state": "PROVISIONED" if d.get("subscriber_status", 0) == 0 else "BARRED",
            })
        return rows, None

    def _rate(self, key: str, value: Optional[float]) -> Optional[float]:
        """Compute per-second rate of a monotonic counter between refresh passes."""
        if value is None:
            return None
        now = time.monotonic()
        prev = self._prev_counters.get(key)
        self._prev_counters[key] = (now, value)
        if not prev or now <= prev[0] or value < prev[1]:
            return 0.0
        return round((value - prev[1]) / (now - prev[0]), 2)

    async def _collect_live(self) -> dict:
        """Execute concurrent live collection pass against all active 5G Core NFs."""
        timeout = httpx.Timeout(REQUEST_TIMEOUT_SEC)
        limits = httpx.Limits(max_connections=50)

        async with httpx.AsyncClient(http1=False, http2=True, timeout=timeout, limits=limits) as h2, \
                   httpx.AsyncClient(timeout=timeout, limits=limits) as h1:
            heartbeat_tasks = [self._heartbeat(h2, h1, nf) for nf in NF_ORDER]
            metric_nfs = ["AMF", "SMF", "UPF", "PCF"]

            results = await asyncio.gather(
                asyncio.gather(*heartbeat_tasks),
                self._nrf_profiles(h2),
                asyncio.gather(*[self._prometheus(h1, nf) for nf in metric_nfs]),
                asyncio.to_thread(self._read_subscribers),
            )

        heartbeats, nrf_profiles, metric_results, (subscribers, db_error) = results
        health = dict(zip(NF_ORDER, heartbeats))
        prom = dict(zip(metric_nfs, metric_results))

        # Cross-reference with NRF registry
        registered_types = {p["nfType"]: p["nfStatus"] for p in nrf_profiles}
        for nf, h in health.items():
            h["nrf_status"] = registered_types.get(nf, "—" if nf == "UPF" else "NOT REGISTERED")

        def flat(nf):
            return _parse_prometheus_text("\n") if prom.get(nf) is None else \
                {k: v for k, v in _sum_samples(prom[nf]).items()}

        amf_m, smf_m, upf_m, pcf_m = flat("AMF"), flat("SMF"), flat("UPF"), flat("PCF")

        registered_ues = amf_m.get("fivegs_amffunction_rm_registeredsubnbr", amf_m.get("ran_ue"))
        active_pdu = smf_m.get("fivegs_smffunction_sm_sessionnbr", smf_m.get("ues_active"))

        # PDU sessions breakdown
        pdu_rows = []
        for name, labels, value in (prom.get("SMF") or []):
            if name == "fivegs_smffunction_sm_sessionnbr":
                pdu_rows.append({
                    "source": "SMF",
                    "plmn": labels.get("plmnid", "-"),
                    "s_nssai": f"SST {labels.get('snssai', '-')}",
                    "dnn": "-",
                    "sessions": int(value)
                })

        qos_by_dnn = {}
        for name, labels, value in (prom.get("UPF") or []):
            if name == "fivegs_upffunction_upf_qosflows":
                qos_by_dnn[labels.get("dnn", "?")] = int(value)
                pdu_rows.append({
                    "source": "UPF",
                    "plmn": "-",
                    "s_nssai": "-",
                    "dnn": labels.get("dnn", "?"),
                    "sessions": int(value)
                })

        upf = {
            "active_sessions":    upf_m.get("fivegs_upffunction_upf_sessionnbr"),
            "n3_in_pps":          self._rate("upf_in",  upf_m.get("fivegs_ep_n3_gtp_indatapktn3upf")),
            "n3_out_pps":         self._rate("upf_out", upf_m.get("fivegs_ep_n3_gtp_outdatapktn3upf")),
            "throughput_dl_mbps": None,
            "throughput_ul_mbps": None,
            "qos_flows_by_dnn":   qos_by_dnn,
        }

        control_plane = {
            "AMF registrations (init req/succ)":
                f"{int(amf_m.get('fivegs_amffunction_rm_reginitreq', 0))} / "
                f"{int(amf_m.get('fivegs_amffunction_rm_reginitsucc', 0))}" if amf_m else "—",
            "AMF auth (req/fail)":
                f"{int(amf_m.get('fivegs_amffunction_amf_authreq', 0))} / "
                f"{int(amf_m.get('fivegs_amffunction_amf_authfail', 0))}" if amf_m else "—",
            "gNBs connected (N2)": int(amf_m["gnb"]) if "gnb" in amf_m else "—",
            "SMF PDU creation (req/succ)":
                f"{int(smf_m.get('fivegs_smffunction_sm_pdusessioncreationreq', 0))} / "
                f"{int(smf_m.get('fivegs_smffunction_sm_pdusessioncreationsucc', 0))}" if smf_m else "—",
            "PCF SM policy assoc (succ)": int(pcf_m.get("fivegs_pcffunction_pa_policysmassosucc", 0)) if pcf_m else "—",
        }

        nfs_online = sum(1 for h in health.values() if h["online"])
        core_error = CORE_DOWN_MSG if nfs_online == 0 else None

        return {
            "simulated":     False,
            "nf_health":     health,
            "nrf_instances": nrf_profiles,
            "ues":           subscribers,
            "pdu_sessions":  pdu_rows,
            "upf":           upf,
            "control_plane": control_plane,
            "raw_metrics":   {"AMF": amf_m, "SMF": smf_m, "UPF": upf_m, "PCF": pcf_m},
            "kpis": {
                "registered_ues": int(registered_ues) if registered_ues is not None else None,
                "active_pdu":     int(active_pdu) if active_pdu is not None else None,
                "subscribers":    len(subscribers) if not db_error else None,
                "nfs_online":     nfs_online,
                "nfs_total":      len(NF_ORDER),
                "avg_latency_ms": _avg([h["latency_ms"] for h in health.values() if h["online"]]),
            },
            "errors": {"core": core_error, "db": db_error},
        }

    def _collect_simulated(self) -> dict:
        """Generate synthetic telemetry snapshot."""
        health = {}
        for nf in NF_ORDER:
            lat = round(random.uniform(0.5, 5.0), 2)
            self.metrics_store.record_latency(
                nf_name=nf, latency_ms=lat, success=True,
                operation="heartbeat", status_code=200
            )
            health[nf] = {
                "online": True, "latency_ms": lat, "status_code": 200, "error": None,
                "simulated": True, "nrf_status": "REGISTERED" if nf != "UPF" else "—"
            }
        ues = Simulator.amf_ue_contexts()
        pdus = Simulator.smf_pdu_sessions()
        upf = Simulator.upf_metrics()
        upf["active_sessions"] = len(pdus)

        return {
            "simulated":     True,
            "nf_health":     health,
            "nrf_instances": Simulator.nrf_instances(),
            "ues":           ues,
            "pdu_sessions":  pdus,
            "upf":           upf,
            "control_plane": {
                "AMF registrations (init req/succ)": f"{len(ues)} / {len(ues)}",
                "gNBs connected (N2)": 4,
                "SMF PDU creation (req/succ)": f"{len(pdus)} / {len(pdus)}"
            },
            "raw_metrics":   {},
            "kpis": {
                "registered_ues": len(ues),
                "active_pdu":     len(pdus),
                "subscribers":    len(_UE_POOL),
                "nfs_online":     len(NF_ORDER),
                "nfs_total":      len(NF_ORDER),
                "avg_latency_ms": _avg([h["latency_ms"] for h in health.values()]),
            },
            "errors": {"core": None, "db": None},
        }

    def collect_all(self) -> dict:
        """
        Execute full collection pass and return a snapshot.
        Guaranteed not to raise: unexpected failures are converted to clean error states.
        """
        t0 = time.perf_counter()
        ts = datetime.now(timezone.utc).isoformat()
        try:
            snap = self._collect_simulated() if self.use_simulator else asyncio.run(self._collect_live())
        except Exception as exc:
            logger.exception("Collection pass failed")
            snap = self._empty_snapshot(f"{CORE_DOWN_MSG} ({type(exc).__name__}: {exc})")
        snap["timestamp"] = ts
        snap["collect_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return snap

    def check_nf_health(self) -> dict[str, dict]:
        """Probe NF heartbeats and return current status map."""
        return self.collect_all()["nf_health"]

    def is_core_reachable(self) -> bool:
        """Fast HTTP/2 check to determine whether the 5G Core is reachable."""
        try:
            with httpx.Client(http1=False, http2=True, timeout=1.5) as c:
                return c.get(self.registry["NRF"]["health_url"]).status_code < 500
        except Exception:
            return False

    def _empty_snapshot(self, error: str) -> dict:
        """Fallback empty snapshot on unhandled system failure."""
        health = {
            nf: {"online": False, "latency_ms": 0.0, "status_code": None,
                 "error": "exception", "simulated": False, "nrf_status": "—"}
            for nf in NF_ORDER
        }
        return {
            "simulated": False, "nf_health": health, "nrf_instances": [], "ues": [],
            "pdu_sessions": [], "upf": {}, "control_plane": {}, "raw_metrics": {},
            "kpis": {
                "registered_ues": None, "active_pdu": None, "subscribers": None,
                "nfs_online": 0, "nfs_total": len(NF_ORDER), "avg_latency_ms": None
            },
            "errors": {"core": error, "db": None}
        }


# ---------------------------------------------------------------------------
# Internal Helper Functions
# ---------------------------------------------------------------------------

def _sum_samples(samples: list) -> dict:
    out: dict[str, float] = {}
    for name, _labels, value in samples:
        out[name] = out.get(name, 0.0) + value
    return out


def _avg(values: list) -> Optional[float]:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


def _fmt_bitrate(b: Optional[dict]) -> str:
    """Format Open5GS AMBR bitrate structure {value, unit} into human-readable string."""
    if not b:
        return "-"
    units = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"]
    try:
        return f"{b.get('value')} {units[int(b.get('unit', 0))]}"
    except (IndexError, ValueError, TypeError):
        return str(b)
