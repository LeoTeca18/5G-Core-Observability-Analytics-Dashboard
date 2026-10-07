"""
test_collector.py — Unit & integration tests for the CollectorService.

Test categories:
  1. Unit tests with Simulator (no network / Docker required).
  2. Integration tests against live 5G Core containers (skipped if unavailable).
  3. Latency measurement validation.
  4. MetricsStore CSV export.

Run:
    pytest tests/ -v
    pytest tests/ -v -k "not integration"   # skip live-core tests
"""

import os
import sys
import time
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from collector import CollectorService, Simulator, _parse_prometheus_text
from metrics import MetricsStore, measure_latency

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    """MetricsStore backed by a temporary CSV file."""
    csv_path = str(tmp_path / "test_perf.csv")
    return MetricsStore(csv_path=csv_path)

@pytest.fixture
def collector_sim(store):
    """CollectorService running in simulator mode (no real network needed)."""
    return CollectorService(metrics_store=store, use_simulator=True)

# ---------------------------------------------------------------------------
# Simulator unit tests
# ---------------------------------------------------------------------------

class TestSimulator:
    def test_nrf_instances_non_empty(self):
        data = Simulator.nrf_instances()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_nrf_instance_schema(self):
        data = Simulator.nrf_instances()
        for inst in data:
            assert "nfType"       in inst
            assert "nfInstanceId" in inst
            assert "nfStatus"     in inst

    def test_amf_ue_contexts_range(self):
        data = Simulator.amf_ue_contexts()
        assert 30 <= len(data) <= 50

    def test_amf_ue_has_supi(self):
        data = Simulator.amf_ue_contexts()
        for ue in data:
            assert "supi" in ue
            assert ue["supi"].startswith("imsi-")

    def test_smf_pdu_sessions_range(self):
        data = Simulator.smf_pdu_sessions()
        assert 15 <= len(data) <= 30

    def test_smf_pdu_has_dnn(self):
        data = Simulator.smf_pdu_sessions()
        for pdu in data:
            assert "dnn" in pdu
            assert pdu["dnn"] in ("internet", "ims", "iot")

    def test_upf_metrics_keys(self):
        data = Simulator.upf_metrics()
        assert "upf_throughput_dl_mbps" in data
        assert "upf_throughput_ul_mbps" in data
        assert "upf_active_sessions"    in data
        assert data["upf_active_sessions"] > 0

    def test_system_kpis(self):
        kpis = Simulator.system_kpis()
        assert kpis["registered_ues"] > 0
        assert kpis["avg_latency_ms"] > 0

# ---------------------------------------------------------------------------
# CollectorService unit tests (simulator mode)
# ---------------------------------------------------------------------------

class TestCollectorSimulator:
    def test_collect_all_keys(self, collector_sim):
        snap = collector_sim.collect_all()
        for key in ("timestamp", "nrf", "amf", "smf", "upf", "nf_health", "kpis"):
            assert key in snap, f"Missing key: {key}"

    def test_kpis_positive(self, collector_sim):
        snap = collector_sim.collect_all()
        assert snap["kpis"]["registered_ues"] > 0
        assert snap["kpis"]["active_pdu"]     > 0
        assert snap["kpis"]["nfs_online"]     > 0

    def test_nf_health_all_present(self, collector_sim):
        snap = collector_sim.collect_all()
        for nf in ("NRF", "AMF", "SMF", "UPF", "PCF", "UDM"):
            assert nf in snap["nf_health"]

    def test_simulated_flag(self, collector_sim):
        snap = collector_sim.collect_all()
        assert snap["nrf"]["simulated"] is True
        assert snap["amf"]["simulated"] is True

    def test_latency_recorded(self, collector_sim, store):
        collector_sim.collect_all()
        df = store.get_latency_df()
        assert len(df) > 0

    def test_multiple_refreshes_accumulate(self, collector_sim, store):
        for _ in range(3):
            collector_sim.collect_all()
        df = store.get_latency_df()
        assert len(df) >= 6   # at least 6 records (one per NF per run)

# ---------------------------------------------------------------------------
# MetricsStore tests
# ---------------------------------------------------------------------------

class TestMetricsStore:
    def test_record_and_retrieve_latency(self, store):
        store.record_latency("AMF", 3.14, success=True)
        df = store.get_latency_df()
        assert len(df) == 1
        assert abs(df.iloc[0]["latency_ms"] - 3.14) < 0.01

    def test_summary_stats_columns(self, store):
        for i in range(10):
            store.record_latency("AMF", float(i), success=True)
        summary = store.get_summary_stats()
        assert "mean_ms" in summary.columns
        assert "p95_ms"  in summary.columns

    def test_success_rate_calculation(self, store):
        store.record_latency("SMF", 5.0, success=True)
        store.record_latency("SMF", 5.0, success=True)
        store.record_latency("SMF", 5.0, success=False)
        summary = store.get_summary_stats()
        smf_row = summary[summary["nf_name"] == "SMF"].iloc[0]
        assert abs(smf_row["success_rate"] - 66.7) < 1.0

    def test_csv_export(self, store, tmp_path):
        store.record_latency("NRF", 2.5, success=True)
        out = str(tmp_path / "export.csv")
        store.flush_to_csv(out)
        import csv
        with open(out) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["nf_name"] == "NRF"

    def test_latency_history_rolling_window(self, store):
        for i in range(130):
            store.record_latency("UPF", float(i), success=True)
        hist = store.get_latency_history("UPF")
        assert len(hist) <= 120   # capped at LATENCY_WINDOW

    def test_system_resources_keys(self, store):
        row = store.record_system_resources(docker_client=None)
        assert "host_cpu_pct" in row
        assert "host_ram_mb"  in row
        assert row["host_ram_mb"] > 0

    def test_measure_decorator(self, store):
        @store.measure("PCF")
        def dummy():
            time.sleep(0.01)
            return 42

        result = dummy()
        assert result == 42
        df = store.get_latency_df()
        assert len(df) == 1
        assert df.iloc[0]["latency_ms"] >= 10.0

# ---------------------------------------------------------------------------
# Prometheus parser tests
# ---------------------------------------------------------------------------

class TestPrometheusParser:
    SAMPLE = """
# HELP upf_bytes_forwarded_total Total bytes forwarded
# TYPE upf_bytes_forwarded_total counter
upf_bytes_forwarded_total{direction="ul"} 1234567.0
upf_bytes_forwarded_total{direction="dl"} 9876543.0
# HELP upf_active_sessions Number of active sessions
# TYPE upf_active_sessions gauge
upf_active_sessions 25
"""

    def test_parses_gauge(self):
        result = _parse_prometheus_text(self.SAMPLE)
        assert "upf_active_sessions" in result
        assert result["upf_active_sessions"] == 25.0

    def test_skips_help_type_lines(self):
        result = _parse_prometheus_text(self.SAMPLE)
        for key in result:
            assert not key.startswith("#")

    def test_empty_string(self):
        result = _parse_prometheus_text("")
        assert result == {}

# ---------------------------------------------------------------------------
# measure_latency helper
# ---------------------------------------------------------------------------

class TestMeasureLatency:
    def test_returns_result(self):
        result, lat = measure_latency(lambda: 99)
        assert result == 99

    def test_latency_positive(self):
        _, lat = measure_latency(time.sleep, 0.005)
        assert lat >= 4.0

# ---------------------------------------------------------------------------
# Integration tests (require live 5G Core — skip automatically if offline)
# ---------------------------------------------------------------------------

def requires_live_core():
    """Skip marker for tests requiring Docker / real NFs."""
    import requests
    try:
        requests.get("http://127.0.0.1:7777/nnrf-nfm/v1/nf-instances", timeout=1)
        return False
    except Exception:
        return True

@pytest.mark.skipif(requires_live_core(), reason="5G Core containers not running")
class TestIntegrationLive:
    def test_nrf_returns_list(self, store):
        svc = CollectorService(store, use_simulator=False)
        result = svc.get_nrf_instances()
        assert isinstance(result["data"], list)

    def test_health_check_nrf_online(self, store):
        svc = CollectorService(store, use_simulator=False)
        health = svc.check_nf_health()
        assert health["NRF"]["online"] is True
