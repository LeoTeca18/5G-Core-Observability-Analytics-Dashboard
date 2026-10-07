"""
metrics.py — Performance Instrumentation for the 5G Core Dashboard.

Tracks and persists:
  - SBI API call latency (ms) per Network Function
  - CPU % and RAM (MB) usage of Docker containers and host dashboard process
  - Request success/failure rates

Data is stored in-memory (pandas DataFrame) and flushed to CSV for export
to the technical report.

Usage:
    store = MetricsStore()
    store.record_latency("AMF", 4.23, success=True)
    store.record_system_resources()
    store.flush_to_csv()
"""

import csv
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

import pandas as pd
import psutil

logger = logging.getLogger(__name__)

# Rolling window size for latency history charts
LATENCY_WINDOW = 120   # last 120 samples per NF

# ---------------------------------------------------------------------------
# MetricsStore
# ---------------------------------------------------------------------------

class MetricsStore:
    """
    Thread-safe in-memory store for all collected metrics.

    Attributes:
        _latency_rows   : list of raw rows for CSV export
        _latency_history: rolling deque per NF for time-series chart
        _lock           : thread lock for concurrent Streamlit callbacks
    """

    def __init__(self, csv_path: Optional[str] = None):
        self._lock = Lock()
        self._latency_rows: list[dict] = []
        self._latency_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=LATENCY_WINDOW))
        self._resource_rows: list[dict] = []
        self._resource_history: deque = deque(maxlen=LATENCY_WINDOW)
        self._csv_path = csv_path

        # Create CSV with header if it does not exist
        if self._csv_path:
            os.makedirs(os.path.dirname(self._csv_path), exist_ok=True)
            if not os.path.exists(self._csv_path):
                with open(self._csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=[
                        "timestamp", "nf_name", "latency_ms", "success"
                    ])
                    writer.writeheader()

    # ------------------------------------------------------------------
    # Latency tracking
    # ------------------------------------------------------------------

    def record_latency(self, nf_name: str, latency_ms: float, success: bool = True) -> None:
        """
        Record a single SBI call latency measurement.

        Args:
            nf_name   : NF identifier (e.g. "AMF", "SMF")
            latency_ms: Round-trip time in milliseconds
            success   : True if HTTP 2xx, False on connection error / timeout
        """
        ts = datetime.now(timezone.utc).isoformat()
        row = {
            "timestamp":  ts,
            "nf_name":    nf_name,
            "latency_ms": round(latency_ms, 3),
            "success":    success,
        }
        with self._lock:
            self._latency_rows.append(row)
            self._latency_history[nf_name].append({"ts": ts, "latency_ms": latency_ms, "success": success})

        # Append to CSV immediately for real-time persistence
        if self._csv_path:
            try:
                with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=["timestamp", "nf_name", "latency_ms", "success"])
                    writer.writerow(row)
            except OSError as exc:
                logger.error("CSV write error: %s", exc)

    def get_latency_df(self) -> pd.DataFrame:
        """Return all latency rows as a pandas DataFrame."""
        with self._lock:
            return pd.DataFrame(list(self._latency_rows))

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
        Return per-NF summary: count, mean/min/max latency, success rate.
        Used for the performance table in the dashboard.
        """
        df = self.get_latency_df()
        if df.empty:
            return pd.DataFrame()
        agg = (
            df.groupby("nf_name")["latency_ms"]
              .agg(samples="count", mean_ms="mean", min_ms="min", max_ms="max", p95_ms=lambda x: x.quantile(0.95))
              .reset_index()
        )
        success_rate = (
            df.groupby("nf_name")["success"]
              .mean()
              .reset_index()
              .rename(columns={"success": "success_rate"})
        )
        merged = agg.merge(success_rate, on="nf_name")
        merged["mean_ms"]     = merged["mean_ms"].round(2)
        merged["min_ms"]      = merged["min_ms"].round(2)
        merged["max_ms"]      = merged["max_ms"].round(2)
        merged["p95_ms"]      = merged["p95_ms"].round(2)
        merged["success_rate"] = (merged["success_rate"] * 100).round(1)
        return merged

    # ------------------------------------------------------------------
    # System resource tracking (CPU + RAM)
    # ------------------------------------------------------------------

    def record_system_resources(self, docker_client=None) -> dict:
        """
        Sample CPU % and RAM MB for the dashboard process and Docker containers.

        Args:
            docker_client: optional docker.DockerClient — if None, only host
                           process metrics are collected.

        Returns:
            dict with keys: timestamp, host_cpu_pct, host_ram_mb, containers
        """
        ts = datetime.now(timezone.utc).isoformat()
        proc = psutil.Process()
        host_cpu = psutil.cpu_percent(interval=0.1)
        host_ram = proc.memory_info().rss / (1024 ** 2)   # bytes → MB

        containers: list[dict] = []
        if docker_client:
            try:
                for container in docker_client.containers.list():
                    stats = container.stats(stream=False)
                    cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - \
                                stats["precpu_stats"]["cpu_usage"]["total_usage"]
                    sys_delta = stats["cpu_stats"]["system_cpu_usage"] - \
                                stats["precpu_stats"]["system_cpu_usage"]
                    num_cpus = stats["cpu_stats"].get("online_cpus", 1)
                    cpu_pct = (cpu_delta / max(sys_delta, 1)) * num_cpus * 100

                    mem_usage = stats["memory_stats"].get("usage", 0)
                    mem_limit = stats["memory_stats"].get("limit", 1)
                    mem_mb = mem_usage / (1024 ** 2)

                    containers.append({
                        "name":    container.name,
                        "cpu_pct": round(cpu_pct, 2),
                        "ram_mb":  round(mem_mb, 2),
                        "status":  container.status,
                    })
            except Exception as exc:
                logger.warning("Docker stats error: %s", exc)

        row = {
            "timestamp":    ts,
            "host_cpu_pct": round(host_cpu, 2),
            "host_ram_mb":  round(host_ram, 2),
            "containers":   containers,
        }
        with self._lock:
            self._resource_rows.append(row)
            self._resource_history.append(row)
        return row

    def get_resource_history_df(self) -> pd.DataFrame:
        """Return rolling resource history as DataFrame (drops containers column)."""
        with self._lock:
            rows = [{"timestamp": r["timestamp"],
                     "host_cpu_pct": r["host_cpu_pct"],
                     "host_ram_mb":  r["host_ram_mb"]}
                    for r in self._resource_history]
        return pd.DataFrame(rows)

    def get_latest_containers(self) -> list[dict]:
        """Return the latest container stats snapshot."""
        with self._lock:
            if self._resource_history:
                return self._resource_history[-1].get("containers", [])
        return []

    # ------------------------------------------------------------------
    # Export utilities
    # ------------------------------------------------------------------

    def flush_to_csv(self, path: Optional[str] = None) -> str:
        """
        Export all latency + resource rows to CSV.

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
                    self.record_latency(nf_name, latency, success=True)
                    return result
                except Exception as exc:
                    latency = (time.perf_counter() - t0) * 1000
                    self.record_latency(nf_name, latency, success=False)
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
