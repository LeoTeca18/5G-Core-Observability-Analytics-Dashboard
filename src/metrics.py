"""
metrics.py — Performance Instrumentation for the 5G Core Dashboard.

Tracks and persists:
  - SBI API call latency (ms) per Network Function and operation
  - CPU % and RAM (MB) of the dashboard process, the host and every
    5G Core Docker container (Docker Engine API via the `docker` SDK)
  - One "telemetry" row per refresh cycle (KPIs + latency + CPU + RAM),
    which is the empirical dataset for the technical report.

Data is stored in-memory (pandas-friendly lists / rolling deques) and appended
to CSV files in real time:
  data/performance_logs.csv  — every SBI call
  data/resource_logs.csv     — CPU / RAM per source (dashboard, host, containers)
  data/telemetry_log.csv     — wide row per refresh cycle

Usage:
    store = MetricsStore(csv_path=PERF_LOG_CSV, resource_csv=..., telemetry_csv=...)
    store.record_latency("AMF", 4.23, success=True, operation="heartbeat")
    res = store.record_system_resources(docker_client=get_docker_client())
    store.record_cycle(snapshot, res)
"""

import csv
import logging
import os
import random
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

import pandas as pd
import psutil

logger = logging.getLogger(__name__)

# Rolling window size for latency history charts
LATENCY_WINDOW = 120   # last 120 samples per NF

LATENCY_FIELDS   = ["timestamp", "nf_name", "operation", "latency_ms", "success", "status_code"]
RESOURCE_FIELDS  = ["timestamp", "source", "kind", "cpu_pct", "ram_mb", "ram_limit_mb", "status"]


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _ensure_csv(path: Optional[str], fields: list[str]) -> None:
    """Create a CSV with header; rotate an existing file whose header differs."""
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header == fields:
            return
        legacy = path.replace(".csv", f"_legacy_{int(time.time())}.csv")
        os.replace(path, legacy)
        logger.info("CSV schema changed — old file kept as %s", legacy)
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()


def _append_csv(path: Optional[str], fields: list[str], rows: list[dict]) -> None:
    if not path or not rows:
        return
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writerows(rows)
    except OSError as exc:
        logger.error("CSV write error (%s): %s", path, exc)


def get_docker_client():
    """
    Return a docker.DockerClient connected to the local engine, or None.

    Works with Docker Desktop on Windows (named pipe, needs pywin32) and with
    the Unix socket under WSL 2 / Linux. Never raises.
    """
    try:
        import docker  # type: ignore
        client = docker.from_env(timeout=5)
        client.ping()
        return client
    except Exception as exc:  # docker not installed / daemon down / no permission
        logger.info("Docker Engine API unavailable: %s", exc)
        return None


def _container_stats(container) -> Optional[dict]:
    """Compute CPU % and RAM MB from a single `docker stats` sample."""
    try:
        stats = container.stats(stream=False)
        cpu = stats.get("cpu_stats", {})
        pre = stats.get("precpu_stats", {})
        cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        n_cpus = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", []) or [1])
        cpu_pct = (cpu_delta / sys_delta) * n_cpus * 100.0 if sys_delta > 0 and cpu_delta > 0 else 0.0

        mem = stats.get("memory_stats", {})
        usage = mem.get("usage", 0)
        # Same formula as the docker CLI: exclude page cache
        cache = mem.get("stats", {}).get("inactive_file", mem.get("stats", {}).get("cache", 0))
        ram_mb = max(usage - cache, 0) / (1024 ** 2)
        limit_mb = mem.get("limit", 0) / (1024 ** 2)
        return {"source": container.name, "kind": "container", "cpu_pct": round(cpu_pct, 2),
                "ram_mb": round(ram_mb, 2), "ram_limit_mb": round(limit_mb, 1), "status": container.status}
    except Exception as exc:
        logger.debug("stats failed for %s: %s", getattr(container, "name", "?"), exc)
        return None


# ---------------------------------------------------------------------------
# MetricsStore
# ---------------------------------------------------------------------------

class MetricsStore:
    """
    Thread-safe in-memory store for all collected metrics.

    Attributes:
        _latency_rows   : list of raw rows for CSV export
        _latency_history: rolling deque per NF for time-series chart
        _resource_rows  : long-format CPU/RAM rows (dashboard, host, containers)
        _telemetry_rows : one wide row per refresh cycle
        _lock           : thread lock for concurrent Streamlit callbacks
    """

    def __init__(self, csv_path: Optional[str] = None,
                 resource_csv: Optional[str] = None,
                 telemetry_csv: Optional[str] = None):
        self._lock = Lock()
        self._latency_rows: list[dict] = []
        self._latency_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=LATENCY_WINDOW))
        self._resource_rows: list[dict] = []
        self._resource_history: deque = deque(maxlen=LATENCY_WINDOW)
        self._telemetry_rows: list[dict] = []
        self._csv_path = csv_path
        self._resource_csv = resource_csv
        self._telemetry_csv = telemetry_csv
        self._telemetry_fields: Optional[list[str]] = None

        # Persistent process handle → cpu_percent() measures the delta between calls
        self._proc = psutil.Process()
        self._proc.cpu_percent(None)
        psutil.cpu_percent(None)

        _ensure_csv(self._csv_path, LATENCY_FIELDS)
        _ensure_csv(self._resource_csv, RESOURCE_FIELDS)

    # ------------------------------------------------------------------
    # Latency tracking
    # ------------------------------------------------------------------

    def record_latency(self, nf_name: str, latency_ms: float, success: bool = True,
                       operation: str = "request", status_code: Optional[int] = None) -> None:
        """
        Record a single SBI call latency measurement.

        Args:
            nf_name    : NF identifier (e.g. "AMF", "SMF")
            latency_ms : Round-trip time in milliseconds
            success    : True if the NF answered, False on connection error / timeout
            operation  : logical operation (heartbeat, nf-list, nf-profile, metrics…)
            status_code: HTTP status returned by the NF (None on transport error)
        """
        ts = datetime.now(timezone.utc).isoformat()
        row = {
            "timestamp":   ts,
            "nf_name":     nf_name,
            "operation":   operation,
            "latency_ms":  round(latency_ms, 3),
            "success":     success,
            "status_code": status_code,
        }
        with self._lock:
            self._latency_rows.append(row)
            self._latency_history[nf_name].append({"ts": ts, "latency_ms": latency_ms,
                                                   "success": success, "operation": operation})
        # Append to CSV immediately for real-time persistence
        _append_csv(self._csv_path, LATENCY_FIELDS, [row])

    def get_latency_df(self) -> pd.DataFrame:
        """Return all latency rows as a pandas DataFrame."""
        with self._lock:
            return pd.DataFrame(list(self._latency_rows), columns=LATENCY_FIELDS)

    def get_latency_history(self, nf_name: str) -> pd.DataFrame:
        """Return rolling latency history for a single NF as DataFrame."""
        with self._lock:
            return pd.DataFrame(list(self._latency_history[nf_name]))

    def get_all_latency_history(self) -> pd.DataFrame:
        """Return merged rolling latency history for all NFs."""
        frames = []
        with self._lock:
            for nf_name, dq in self._latency_history.items():
                df = pd.DataFrame(list(dq))
                if not df.empty:
                    df["nf_name"] = nf_name
                    frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def get_summary_stats(self) -> pd.DataFrame:
        """
        Return per-NF summary: count, mean/min/max/p50/p95/p99 latency, success rate.
        Used for the performance table in the dashboard.
        """
        df = self.get_latency_df()
        if df.empty:
            return pd.DataFrame()
        agg = (
            df.groupby("nf_name")["latency_ms"]
              .agg(samples="count", mean_ms="mean", min_ms="min", max_ms="max",
                   p50_ms=lambda x: x.quantile(0.50),
                   p95_ms=lambda x: x.quantile(0.95),
                   p99_ms=lambda x: x.quantile(0.99))
              .reset_index()
        )
        success_rate = (
            df.assign(success=df["success"].astype(bool))
              .groupby("nf_name")["success"].mean()
              .reset_index().rename(columns={"success": "success_rate"})
        )
        merged = agg.merge(success_rate, on="nf_name")
        for col in ("mean_ms", "min_ms", "max_ms", "p50_ms", "p95_ms", "p99_ms"):
            merged[col] = merged[col].round(2)
        merged["success_rate"] = (merged["success_rate"] * 100).round(1)
        return merged

    # ------------------------------------------------------------------
    # System resource tracking (CPU + RAM)
    # ------------------------------------------------------------------

    def record_system_resources(self, docker_client=None,
                                container_prefixes: tuple = ("open5gs-", "ueransim-", "prometheus"),
                                simulate_containers: bool = False) -> dict:
        """
        Sample CPU % and RAM MB for the dashboard process, the host and the
        5G Core Docker containers.

        Args:
            docker_client      : docker.DockerClient (see get_docker_client()); if
                                 None, container metrics are skipped.
            container_prefixes : only containers whose name starts with one of
                                 these prefixes are sampled.
            simulate_containers: generate synthetic container rows when Docker
                                 is unavailable (Simulator Mode only).

        Returns:
            dict with keys: timestamp, dashboard_cpu_pct, dashboard_ram_mb,
            host_cpu_pct, host_ram_mb, host_ram_total_mb, containers (list),
            docker_available (bool)
        """
        ts = datetime.now(timezone.utc).isoformat()
        dash_cpu = self._proc.cpu_percent(None) / max(psutil.cpu_count() or 1, 1)  # normalised 0-100
        dash_ram = self._proc.memory_info().rss / (1024 ** 2)
        host_cpu = psutil.cpu_percent(None)
        vm = psutil.virtual_memory()
        host_ram = (vm.total - vm.available) / (1024 ** 2)

        containers: list[dict] = []
        docker_ok = False
        if docker_client is not None:
            try:
                targets = [c for c in docker_client.containers.list()
                           if c.name.startswith(container_prefixes)]
                # `docker stats` blocks ~1-2 s per container → sample in parallel
                with ThreadPoolExecutor(max_workers=max(len(targets), 1)) as pool:
                    containers = [r for r in pool.map(_container_stats, targets) if r]
                docker_ok = True
            except Exception as exc:
                logger.warning("Docker stats error: %s", exc)
        if not docker_ok and simulate_containers:
            for name in ("open5gs-nrf", "open5gs-amf", "open5gs-smf", "open5gs-upf",
                         "open5gs-pcf", "open5gs-udm", "open5gs-mongodb"):
                containers.append({"source": name + " (sim)", "kind": "container",
                                   "cpu_pct": round(random.uniform(0.2, 6.0), 2),
                                   "ram_mb": round(random.uniform(8, 60) if "mongo" not in name
                                                   else random.uniform(120, 200), 2),
                                   "ram_limit_mb": 0, "status": "simulated"})
        containers.sort(key=lambda c: c["source"])

        row = {
            "timestamp":          ts,
            "dashboard_cpu_pct":  round(dash_cpu, 2),
            "dashboard_ram_mb":   round(dash_ram, 2),
            "host_cpu_pct":       round(host_cpu, 2),
            "host_ram_mb":        round(host_ram, 2),
            "host_ram_total_mb":  round(vm.total / (1024 ** 2), 1),
            "containers":         containers,
            "docker_available":   docker_ok,
            "core_cpu_pct":       round(sum(c["cpu_pct"] for c in containers), 2),
            "core_ram_mb":        round(sum(c["ram_mb"] for c in containers), 2),
        }
        long_rows = [
            {"timestamp": ts, "source": "dashboard", "kind": "process", "cpu_pct": row["dashboard_cpu_pct"],
             "ram_mb": row["dashboard_ram_mb"], "ram_limit_mb": "", "status": "running"},
            {"timestamp": ts, "source": "host", "kind": "host", "cpu_pct": row["host_cpu_pct"],
             "ram_mb": row["host_ram_mb"], "ram_limit_mb": row["host_ram_total_mb"], "status": "running"},
        ] + [{"timestamp": ts, **c} for c in containers]

        with self._lock:
            self._resource_rows.extend(long_rows)
            self._resource_history.append(row)
        _append_csv(self._resource_csv, RESOURCE_FIELDS, long_rows)
        return row

    def get_resource_history_df(self) -> pd.DataFrame:
        """Return rolling resource history as DataFrame (drops containers column)."""
        with self._lock:
            rows = [{k: v for k, v in r.items() if k != "containers"} for r in self._resource_history]
        return pd.DataFrame(rows)

    def get_resource_log_df(self) -> pd.DataFrame:
        """Full long-format resource log (dashboard, host, containers)."""
        with self._lock:
            return pd.DataFrame(list(self._resource_rows), columns=RESOURCE_FIELDS)

    def get_latest_containers(self) -> list[dict]:
        """Return the latest container stats snapshot."""
        with self._lock:
            if self._resource_history:
                return self._resource_history[-1].get("containers", [])
        return []

    # ------------------------------------------------------------------
    # Per-cycle telemetry (KPIs + latency + resources in one row)
    # ------------------------------------------------------------------

    def record_cycle(self, snapshot: dict, resources: Optional[dict] = None) -> dict:
        """
        Store one wide telemetry row combining the dashboard snapshot and the
        resource sample of the same refresh cycle.
        """
        kpis = snapshot.get("kpis", {})
        row = {
            "timestamp":       snapshot.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "mode":            "simulated" if snapshot.get("simulated") else "live",
            "registered_ues":  kpis.get("registered_ues"),
            "active_pdu":      kpis.get("active_pdu"),
            "subscribers":     kpis.get("subscribers"),
            "nfs_online":      kpis.get("nfs_online"),
            "avg_latency_ms":  kpis.get("avg_latency_ms"),
            "upf_dl_mbps":     snapshot.get("upf", {}).get("throughput_dl_mbps"),
            "upf_ul_mbps":     snapshot.get("upf", {}).get("throughput_ul_mbps"),
            "collect_ms":      snapshot.get("collect_ms"),
        }
        for nf, h in snapshot.get("nf_health", {}).items():
            row[f"lat_{nf}_ms"] = h.get("latency_ms")
            row[f"up_{nf}"] = int(bool(h.get("online")))
        if resources:
            for key in ("dashboard_cpu_pct", "dashboard_ram_mb", "host_cpu_pct",
                        "host_ram_mb", "core_cpu_pct", "core_ram_mb"):
                row[key] = resources.get(key)

        with self._lock:
            self._telemetry_rows.append(row)
            if self._telemetry_fields is None:
                self._telemetry_fields = list(row.keys())
                _ensure_csv(self._telemetry_csv, self._telemetry_fields)
        _append_csv(self._telemetry_csv, self._telemetry_fields, [row])
        return row

    def get_telemetry_df(self) -> pd.DataFrame:
        with self._lock:
            return pd.DataFrame(list(self._telemetry_rows))

    # ------------------------------------------------------------------
    # Export utilities
    # ------------------------------------------------------------------

    def flush_to_csv(self, path: Optional[str] = None) -> str:
        """
        Export all latency rows to CSV.

        Args:
            path: override output path; defaults to self._csv_path.

        Returns:
            Absolute path to the written CSV file.
        """
        out = path or self._csv_path
        if not out:
            raise ValueError("No CSV path configured")
        df = self.get_latency_df()
        df.to_csv(out, index=False, encoding="utf-8")
        logger.info("Exported %d rows to %s", len(df), out)
        return out

    def export_summary_csv(self, path: str) -> str:
        """Export per-NF summary statistics to a separate CSV."""
        df = self.get_summary_stats()
        df.to_csv(path, index=False, encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # Convenience: decorator for measuring arbitrary function latency
    # ------------------------------------------------------------------

    def measure(self, nf_name: str):
        """
        Decorator factory that wraps a function and records its execution
        time as a latency sample.

        Usage:
            @store.measure("AMF")
            def fetch_amf():
                ...
        """
        def decorator(func):
            def wrapper(*args, **kwargs):
                t0 = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    latency = (time.perf_counter() - t0) * 1000
                    self.record_latency(nf_name, latency, success=True, operation=func.__name__)
                    return result
                except Exception as exc:
                    latency = (time.perf_counter() - t0) * 1000
                    self.record_latency(nf_name, latency, success=False, operation=func.__name__)
                    raise exc
            return wrapper
        return decorator


# ---------------------------------------------------------------------------
# Standalone helper (used by tests without a full MetricsStore)
# ---------------------------------------------------------------------------

def measure_latency(func, *args, **kwargs) -> tuple:
    """
    Execute func(*args, **kwargs) and return (result, latency_ms).
    Lightweight alternative to the MetricsStore.measure decorator.
    """
    t0 = time.perf_counter()
    result = func(*args, **kwargs)
    return result, (time.perf_counter() - t0) * 1000
