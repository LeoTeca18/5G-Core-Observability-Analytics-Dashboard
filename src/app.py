"""
app.py — Main Streamlit Dashboard for the 5G Core Observability Platform.

Layout Structure (Single-Page, Auto-Refreshing):
  - Sidebar:
      Target host configuration, simulator toggle, continuous refresh cadence,
      and Docker daemon status indicators.
  - Header & Status Banners:
      High-visibility alerts informing users if 5G Core or MongoDB are unreachable,
      or if Simulator mode is engaged.
  - Hero KPI Row:
      High-level metrics cards for registered UEs, active PDU sessions, online NFs,
      and average SBI round-trip latency.
  - Network Function Health Grid:
      Visual badge matrix (AMF, SMF, UPF, PCF, UDM, NRF) indicating online/offline
      status, live latency (ms), and NRF registration state.
  - Real-Time Charts:
      Plotly dynamic time-series for per-NF SBI response times and dual CPU %
      comparison (Dashboard vs Total Host vs Core Containers).
  - User Plane & Control Plane Signals:
      UPF session count, GTP-U N3 packet rates, PDU breakdown by Data Network Name (DNN).
  - Subscribers & PDU Session Tables:
      Real-time active subscribers and PDU session context records.
  - Infrastructure & Docker Resources:
      Resource utilization breakdown (CPU %, RAM MB, memory limits) across all
      running 5G Core microservice containers.
  - Data Export Panel:
      One-click CSV downloads for cycle telemetry, SBI latencies, and resource consumption.
"""

import time
import logging
import os
import sys
from datetime import datetime, timezone

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# Local imports
sys.path.insert(0, os.path.dirname(__file__))
from config import (
    NF_ORDER, NF_COLORS, REFRESH_INTERVAL_SEC, PERF_LOG_CSV, RESOURCE_LOG_CSV,
    TELEMETRY_CSV, EXPORT_DIR, DATA_DIR, CORE_HOST
)
from collector import CollectorService
from metrics import MetricsStore, get_docker_client

# ---------------------------------------------------------------------------
# Page Configuration
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="5G Core Observability Dashboard",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Custom CSS — High-Contrast Dark Glassmorphism Theme
# ---------------------------------------------------------------------------
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

  html, body, [class*="css"] { 
    font-family: 'Inter', sans-serif;
  }

  .stApp {
    background-color: #0b0f19;
    color: #f8fafc;
  }

  /* Sidebar styling */
  [data-testid="stSidebar"] {
    background-color: #0f172a !important;
    border-right: 1.5px solid #1e3a5f !important;
  }
  [data-testid="stSidebar"] * {
    color: #f1f5f9 !important;
  }

  /* Hero KPI Cards */
  .kpi-card {
    background: #111c30;
    border: 1.5px solid #1e3a5f;
    border-top: 3px solid #38bdf8;
    border-radius: 14px;
    padding: 20px 14px;
    text-align: center;
    position: relative;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
    transition: transform 0.2s, border-color 0.2s;
  }
  .kpi-card:hover {
    transform: translateY(-2px);
    border-color: #38bdf8;
    box-shadow: 0 8px 30px rgba(56, 189, 248, 0.2);
  }
  .kpi-value {
    font-size: 2.7rem;
    font-weight: 800;
    line-height: 1.1;
    color: #38bdf8;
    text-shadow: 0 0 24px rgba(56, 189, 248, 0.45);
  }
  .kpi-label {
    font-size: 0.82rem;
    font-weight: 700;
    color: #e2e8f0;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-top: 8px;
  }
  .kpi-icon { font-size: 1.7rem; margin-bottom: 4px; }

  /* NF Status Badges */
  .nf-badge-online {
    display: flex;
    flex-direction: column;
    justify-content: center;
    background: #064e3b;
    border: 1.5px solid #10b981;
    border-radius: 12px;
    padding: 12px 14px;
    font-size: 0.95rem;
    font-weight: 700;
    color: #ffffff;
    width: 100%;
    box-shadow: 0 2px 10px rgba(16, 185, 129, 0.2);
  }
  .nf-badge-offline {
    display: flex;
    flex-direction: column;
    justify-content: center;
    background: #450a0a;
    border: 1.5px solid #ef4444;
    border-radius: 12px;
    padding: 12px 14px;
    font-size: 0.95rem;
    font-weight: 700;
    color: #ffffff;
    width: 100%;
    box-shadow: 0 2px 10px rgba(239, 68, 68, 0.2);
  }
  .nf-badge-sim {
    display: flex;
    flex-direction: column;
    justify-content: center;
    background: #451a03;
    border: 1.5px solid #f59e0b;
    border-radius: 12px;
    padding: 12px 14px;
    font-size: 0.95rem;
    font-weight: 700;
    color: #ffffff;
    width: 100%;
    box-shadow: 0 2px 10px rgba(245, 158, 11, 0.2);
  }

  /* Section Title Headers */
  .section-header {
    font-size: 1.15rem;
    font-weight: 700;
    color: #38bdf8;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin: 28px 0 14px 0;
    padding-bottom: 8px;
    border-bottom: 2px solid #1e3a5f;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  /* Notice Badges & Warning Banners */
  .ts-badge {
    font-size: 0.85rem;
    font-weight: 600;
    color: #f1f5f9;
    background: #1e293b;
    border: 1.5px solid #38bdf8;
    border-radius: 20px;
    padding: 6px 16px;
    display: inline-block;
  }
  .sim-banner {
    background: #422006;
    border: 1.5px solid #f59e0b;
    border-radius: 12px;
    padding: 12px 18px;
    font-size: 0.90rem;
    color: #fef08a;
    font-weight: 500;
    margin-bottom: 14px;
  }
  .error-banner {
    background: #450a0a;
    border: 1.5px solid #ef4444;
    border-radius: 12px;
    padding: 14px 18px;
    font-size: 0.92rem;
    color: #fecaca;
    font-weight: 600;
    margin-bottom: 14px;
  }

  /* Interactive DataFrames */
  [data-testid="stDataFrame"] {
    border: 1.5px solid #1e3a5f !important;
    border-radius: 12px !important;
    background: #0f172a !important;
  }
  header { background-color: transparent !important; }
  footer { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Session-State Initialisation
# ---------------------------------------------------------------------------
if "metrics_store" not in st.session_state:
    st.session_state.metrics_store = MetricsStore(
        csv_path=PERF_LOG_CSV,
        resource_csv=RESOURCE_LOG_CSV,
        telemetry_csv=TELEMETRY_CSV
    )

if "docker_client" not in st.session_state:
    st.session_state.docker_client = get_docker_client()

if "collector" not in st.session_state:
    # Auto-detect if Core is reachable to configure sensible default mode
    probe_collector = CollectorService(
        st.session_state.metrics_store,
        use_simulator=False,
        core_host=CORE_HOST
    )
    initial_sim = not probe_collector.is_core_reachable()
    probe_collector.use_simulator = initial_sim
    st.session_state.collector = probe_collector
    st.session_state.use_sim = initial_sim

if "snapshot" not in st.session_state:
    st.session_state.snapshot = None
if "resources" not in st.session_state:
    st.session_state.resources = None
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = 0.0
if "auto_refresh" not in st.session_state:
    st.session_state.auto_refresh = True
if "refresh_count" not in st.session_state:
    st.session_state.refresh_count = 0

metrics_store: MetricsStore = st.session_state.metrics_store
collector: CollectorService = st.session_state.collector

# ---------------------------------------------------------------------------
# Sidebar Controls
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("""
    <div style="text-align:center; padding: 12px 0 20px;">
        <div style="font-size:2.4rem;">📡</div>
        <div style="font-size:1.15rem; font-weight:800; color:#38bdf8;">5G Core Command Center</div>
        <div style="font-size:0.75rem; color:#94a3b8; letter-spacing:0.1em;">OBSERVABILITY & ANALYTICS</div>
    </div>""", unsafe_allow_html=True)

    st.markdown("---")

    # Simulator toggle
    use_sim = st.toggle(
        "🎮 Simulator Mode (Mock)",
        value=st.session_state.collector.use_simulator,
        help="Enable to use synthetic data when 5G Core containers are offline."
    )
    if use_sim != st.session_state.collector.use_simulator:
        st.session_state.collector.use_simulator = use_sim
        st.session_state.last_refresh = 0.0

    st.markdown("---")
    st.markdown("**⚙️ Network Configuration**")
    core_host = st.text_input(
        "Core Host",
        value=collector.core_host,
        help="IP address or hostname where 5G Core ports are exposed"
    )
    if core_host != collector.core_host:
        collector.set_core_host(core_host)
        st.session_state.last_refresh = 0.0

    st.markdown("---")
    st.markdown("**🔄 Continuous Refresh**")
    auto_refresh = st.toggle("Auto-refresh", value=st.session_state.auto_refresh)
    st.session_state.auto_refresh = auto_refresh
    refresh_interval = st.slider("Interval (seconds)", 2, 30, REFRESH_INTERVAL_SEC)

    if st.button("🔁 Refresh Now", use_container_width=True):
        st.session_state.last_refresh = 0.0

    st.markdown("---")
    st.markdown("**🐳 Docker Engine**")
    if st.session_state.docker_client:
        st.success("Docker API Connected")
    else:
        st.warning("Docker API Unavailable (psutil active)")

    st.markdown("---")
    st.markdown("**📊 Session Stats**")
    st.metric("Total Refresh Cycles", st.session_state.refresh_count)
    st.metric("Latency Measurements", len(metrics_store.get_latency_df()))

# ---------------------------------------------------------------------------
# Data Collection Cycle
# ---------------------------------------------------------------------------
now = time.time()
if (now - st.session_state.last_refresh) >= refresh_interval or st.session_state.snapshot is None:
    with st.spinner("Polling 5G Core Network Functions & system resources…"):
        snap = collector.collect_all()
        st.session_state.snapshot = snap

        # Collect host, dashboard and container resource metrics
        res = metrics_store.record_system_resources(
            docker_client=st.session_state.docker_client,
            simulate_containers=collector.use_simulator
        )
        st.session_state.resources = res

        # Record unified cycle telemetry row for empirical report data
        metrics_store.record_cycle(snap, res)

    st.session_state.last_refresh = time.time()
    st.session_state.refresh_count += 1

snapshot = st.session_state.snapshot
resources = st.session_state.resources

# ---------------------------------------------------------------------------
# Header Section
# ---------------------------------------------------------------------------
col_title, col_ts = st.columns([3, 1])
with col_title:
    st.markdown("""
    <h1 style="font-size:2.0rem; font-weight:800; color:#ffffff; margin:0; padding:0; letter-spacing:-0.01em;">
        📡 5G Core Observability & Analytics Dashboard
    </h1>
    <p style="color:#93c5fd; font-size:0.90rem; font-weight:500; margin:4px 0 0 0;">
        3GPP Rel-16 · Open5GS / free5GC · Service-Based Interface (SBI) & User Plane (UPF)
    </p>
    """, unsafe_allow_html=True)
with col_ts:
    ts_obj = datetime.fromisoformat(snapshot["timestamp"].replace("Z", "+00:00"))
    ts_str = ts_obj.strftime("%H:%M:%S UTC")
    st.markdown(f"""
    <div style="text-align:right; padding-top:12px;">
        <span class="ts-badge">⏱ Updated: {ts_str}</span>
    </div>""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Resilient Exception Handling & Notification Banners
# ---------------------------------------------------------------------------
errors = snapshot.get("errors", {})
if errors.get("core"):
    st.markdown(f"""
    <div class="error-banner">
        🚨 <strong>5G Core Connection Failure:</strong> {errors['core']}
        <br><span style="font-size:0.82rem; font-weight:400; color:#fca5a5;">
        Ensure Open5GS Docker containers are running or activate 'Simulator Mode' in the sidebar.
        </span>
    </div>""", unsafe_allow_html=True)

if errors.get("db"):
    st.markdown(f"""
    <div class="error-banner">
        ⚠️ <strong>Core Database Connection Failure (MongoDB):</strong> {errors['db']}
    </div>""", unsafe_allow_html=True)

if snapshot.get("simulated"):
    st.markdown("""
    <div class="sim-banner">
        🎮 <strong>Simulator Mode Active:</strong> Displaying synthetic telemetry.
        To stream real telemetry from the Open5GS/free5GC cluster, disable Simulator Mode in the sidebar.
    </div>""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Hero KPI Row
# ---------------------------------------------------------------------------
kpis = snapshot["kpis"]
k1, k2, k3, k4 = st.columns(4)
kpi_defs = [
    (k1, "👥", kpis.get("registered_ues") if kpis.get("registered_ues") is not None else 0, "Registered UEs"),
    (k2, "🔗", kpis.get("active_pdu") if kpis.get("active_pdu") is not None else 0, "Active PDU Sessions"),
    (k3, "🏥", f"{kpis.get('nfs_online', 0)} / {kpis.get('nfs_total', len(NF_ORDER))}", "NFs Online"),
    (k4, "⚡", f"{kpis['avg_latency_ms']} ms" if kpis.get('avg_latency_ms') is not None else "—", "Avg SBI Latency"),
]
for col, icon, val, label in kpi_defs:
    with col:
        st.markdown(f"""
        <div class="kpi-card">
            <div class="kpi-icon">{icon}</div>
            <div class="kpi-value">{val}</div>
            <div class="kpi-label">{label}</div>
        </div>""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Network Function Health Monitoring Grid
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">🏥 Network Function Health Monitoring (NF Health)</div>', unsafe_allow_html=True)
health = snapshot["nf_health"]
cols = st.columns(len(NF_ORDER))
for col, nf in zip(cols, NF_ORDER):
    status = health.get(nf, {})
    online = status.get("online", False)
    simulated = status.get("simulated", False)
    lat = status.get("latency_ms", 0.0)
    lat_str = f"{lat:.1f} ms" if online else "TIMEOUT"
    nrf_st = status.get("nrf_status", "—")

    if simulated:
        css_class = "nf-badge-sim"
        badge_status = "🟡 SIMULATED"
        status_color = "#fef08a"
    elif online:
        css_class = "nf-badge-online"
        badge_status = "🟢 ONLINE"
        status_color = "#86efac"
    else:
        css_class = "nf-badge-offline"
        badge_status = "🔴 OFFLINE"
        status_color = "#fca5a5"

    with col:
        st.markdown(f"""
        <div class="{css_class}">
            <div style="font-size:1.05rem; font-weight:800; color:#ffffff; margin-bottom:2px;">{nf}</div>
            <div style="font-size:0.80rem; font-weight:700; color:{status_color};">{badge_status}</div>
            <div style="font-size:0.78rem; font-weight:600; color:#ffffff; margin-top:3px;">⏱ {lat_str}</div>
            <div style="font-size:0.70rem; color:#cbd5e1; margin-top:2px;">NRF: {nrf_st}</div>
        </div>""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Telemetry & Dynamic Charts
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">📈 Control Plane (SBI) & Resource Observability</div>', unsafe_allow_html=True)

chart_col1, chart_col2 = st.columns([1.5, 1])

with chart_col1:
    st.markdown("<strong style='color:#f8fafc; font-size:1rem;'>Service-Based Interface (SBI) Call Latency (ms)</strong>", unsafe_allow_html=True)
    lat_df = metrics_store.get_all_latency_history()
    if not lat_df.empty:
        lat_df_plot = lat_df.copy()
        lat_df_plot["ts"] = pd.to_datetime(lat_df_plot["ts"])
        fig_lat = px.line(
            lat_df_plot, x="ts", y="latency_ms", color="nf_name",
            labels={"ts": "Timestamp", "latency_ms": "Latency (ms)", "nf_name": "NF"},
            color_discrete_map=NF_COLORS,
        )
        fig_lat.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(15,23,42,0.6)",
            font_color="#f1f5f9",
            legend=dict(font=dict(color="#f8fafc", size=11), bgcolor="rgba(15,23,42,0.85)"),
            margin=dict(l=0, r=0, t=10, b=0), height=280,
            xaxis=dict(gridcolor="rgba(255,255,255,0.12)", tickfont=dict(color="#cbd5e1")),
            yaxis=dict(gridcolor="rgba(255,255,255,0.12)", tickfont=dict(color="#cbd5e1")),
        )
        st.plotly_chart(fig_lat, use_container_width=True)
    else:
        st.info("Awaiting latency samples...")

with chart_col2:
    st.markdown("<strong style='color:#f8fafc; font-size:1rem;'>CPU Utilization: Dashboard vs Host (%)</strong>", unsafe_allow_html=True)
    res_df = metrics_store.get_resource_history_df()
    if not res_df.empty:
        res_plot = res_df.copy().tail(60)
        res_plot["ts"] = pd.to_datetime(res_plot["timestamp"])
        fig_cpu = go.Figure()
        fig_cpu.add_trace(go.Scatter(
            x=res_plot["ts"], y=res_plot["dashboard_cpu_pct"],
            name="Dashboard (Python)", line=dict(color="#38bdf8", width=2)
        ))
        fig_cpu.add_trace(go.Scatter(
            x=res_plot["ts"], y=res_plot["host_cpu_pct"],
            name="Total Host", line=dict(color="#a855f7", width=1.5, dash="dot")
        ))
        if "core_cpu_pct" in res_plot.columns:
            fig_cpu.add_trace(go.Scatter(
                x=res_plot["ts"], y=res_plot["core_cpu_pct"],
                name="5G Core Containers", line=dict(color="#10b981", width=2)
            ))
        fig_cpu.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(15,23,42,0.6)",
            font_color="#f1f5f9",
            legend=dict(font=dict(color="#f8fafc", size=10), bgcolor="rgba(15,23,42,0.8)"),
            margin=dict(l=0, r=0, t=10, b=0), height=280,
            xaxis=dict(gridcolor="rgba(255,255,255,0.12)", tickfont=dict(color="#cbd5e1")),
            yaxis=dict(gridcolor="rgba(255,255,255,0.12)", tickfont=dict(color="#cbd5e1"), title="%"),
        )
        st.plotly_chart(fig_cpu, use_container_width=True)
    else:
        st.info("Awaiting resource metrics...")

# ---------------------------------------------------------------------------
# User Plane Function (UPF) & PDU Session Analytics
# ---------------------------------------------------------------------------
c_upf1, c_upf2, c_ctrl = st.columns([1, 1, 1])
upf_data = snapshot.get("upf", {})

with c_upf1:
    st.markdown("<strong style='color:#f8fafc; font-size:1rem;'>User Plane Function (UPF)</strong>", unsafe_allow_html=True)
    n3_in = upf_data.get("n3_in_pps")
    n3_out = upf_data.get("n3_out_pps")
    dl_mbps = upf_data.get("throughput_dl_mbps")
    ul_mbps = upf_data.get("throughput_ul_mbps")

    st.metric("Active UPF Sessions", upf_data.get("active_sessions", 0))
    if dl_mbps is not None:
        st.metric("Throughput DL / UL", f"{dl_mbps} / {ul_mbps} Mbps")
    else:
        st.metric("N3 Interface (GTP-U)", f"{n3_in or 0:.0f} pps (IN) / {n3_out or 0:.0f} pps (OUT)")

with c_upf2:
    st.markdown("<strong style='color:#f8fafc; font-size:1rem;'>PDU Sessions by DNN / Slice</strong>", unsafe_allow_html=True)
    pdu_list = snapshot.get("pdu_sessions", [])
    if pdu_list:
        df_pdu_pie = pd.DataFrame(pdu_list)
        if "dnn" in df_pdu_pie.columns and df_pdu_pie["dnn"].ne("-").any():
            cnts = df_pdu_pie[df_pdu_pie["dnn"] != "-"]["dnn"].value_counts().reset_index()
            cnts.columns = ["dnn", "count"]
            fig_p = px.pie(
                cnts, values="count", names="dnn", hole=0.55,
                color_discrete_sequence=["#38bdf8", "#34d399", "#fbbf24", "#f87171"]
            )
            fig_p.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", font_color="#f8fafc",
                margin=dict(l=0, r=0, t=5, b=0), height=170
            )
            st.plotly_chart(fig_p, use_container_width=True)
        else:
            st.info("Active PDU sessions established on default DNN 'internet'")
    else:
        st.info("No active PDU sessions recorded at this time.")

with c_ctrl:
    st.markdown("<strong style='color:#f8fafc; font-size:1rem;'>Control Plane Key Indicators</strong>", unsafe_allow_html=True)
    ctrl = snapshot.get("control_plane", {})
    if ctrl:
        for k, v in ctrl.items():
            st.write(f"• **{k}**: `{v}`")
    else:
        st.info("Awaiting signaling telemetry...")

# ---------------------------------------------------------------------------
# Registered Subscribers & PDU Sessions Tables
# ---------------------------------------------------------------------------
t_col1, t_col2 = st.columns(2)

with t_col1:
    st.markdown('<div class="section-header">👥 Registered Subscribers & UEs (AMF / UDR)</div>', unsafe_allow_html=True)
    ue_data = snapshot.get("ues", [])
    if ue_data:
        df_ue = pd.DataFrame(ue_data)
        st.dataframe(df_ue, use_container_width=True, height=220)
    else:
        st.warning("No UE context data available.")

with t_col2:
    st.markdown('<div class="section-header">🔗 PDU Sessions & SM Contexts (SMF)</div>', unsafe_allow_html=True)
    if pdu_list:
        st.dataframe(pd.DataFrame(pdu_list), use_container_width=True, height=220)
    else:
        st.warning("No active PDU sessions.")

# ---------------------------------------------------------------------------
# NRF Registered Instances & System Infrastructure Metrics
# ---------------------------------------------------------------------------
r_col1, r_col2 = st.columns([1.2, 1])

with r_col1:
    st.markdown('<div class="section-header">🗂️ Registered Instances in NRF (TS 29.510)</div>', unsafe_allow_html=True)
    nrf_instances = snapshot.get("nrf_instances", [])
    if nrf_instances:
        st.dataframe(pd.DataFrame(nrf_instances), use_container_width=True, height=200)
    else:
        st.info("No registered instances returned by NRF.")

with r_col2:
    st.markdown('<div class="section-header">💻 System & Infrastructure Resources</div>', unsafe_allow_html=True)
    if resources:
        res_m1, res_m2 = st.columns(2)
        with res_m1:
            st.metric("Dashboard CPU (psutil)", f"{resources['dashboard_cpu_pct']}%")
            st.metric("Dashboard RAM", f"{resources['dashboard_ram_mb']:.1f} MB")
        with res_m2:
            st.metric("Total Host CPU", f"{resources['host_cpu_pct']}%")
            st.metric("Host RAM Used", f"{resources['host_ram_mb']:.0f} MB")

# Detailed Docker container consumption table
containers = metrics_store.get_latest_containers()
if containers:
    st.markdown('<div class="section-header">🐳 5G Core Docker Container Utilization</div>', unsafe_allow_html=True)
    df_cont = pd.DataFrame(containers)
    df_cont_display = df_cont.rename(columns={
        "source": "Container",
        "cpu_pct": "CPU %",
        "ram_mb": "RAM (MB)",
        "ram_limit_mb": "RAM Limit (MB)",
        "status": "Status"
    })
    st.dataframe(df_cont_display, use_container_width=True, height=200)

# ---------------------------------------------------------------------------
# SBI Performance & Latency Statistics Table
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">⚡ API Performance & Latency Statistics (SBI)</div>', unsafe_allow_html=True)
summary_df = metrics_store.get_summary_stats()
if not summary_df.empty:
    st.dataframe(
        summary_df.rename(columns={
            "nf_name": "NF",
            "samples": "Samples",
            "mean_ms": "Mean (ms)",
            "min_ms": "Min (ms)",
            "max_ms": "Max (ms)",
            "p50_ms": "P50 (ms)",
            "p95_ms": "P95 (ms)",
            "p99_ms": "P99 (ms)",
            "success_rate": "Success Rate (%)"
        }),
        use_container_width=True
    )
else:
    st.info("Accumulating API call statistics...")

# ---------------------------------------------------------------------------
# Telemetry & Performance Report Export Panel
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">📤 Telemetry & Performance Report Export</div>', unsafe_allow_html=True)
exp1, exp2, exp3 = st.columns(3)

with exp1:
    telemetry_df = metrics_store.get_telemetry_df()
    if not telemetry_df.empty:
        csv_tel = telemetry_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download Full Telemetry (CSV)",
            data=csv_tel,
            file_name=f"telemetry_5g_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=True,
            help="Contains cycle-by-cycle KPIs, latency metrics, and CPU/RAM usage."
        )
    else:
        st.button("⬇️ Download Full Telemetry (CSV)", disabled=True, use_container_width=True)

with exp2:
    lat_full = metrics_store.get_latency_df()
    if not lat_full.empty:
        csv_lat = lat_full.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download SBI Latency Log (CSV)",
            data=csv_lat,
            file_name=f"sbi_latency_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=True,
            help="Individual round-trip HTTP/2 latency record for each Network Function call."
        )
    else:
        st.button("⬇️ Download SBI Latency Log (CSV)", disabled=True, use_container_width=True)

with exp3:
    res_full = metrics_store.get_resource_log_df()
    if not res_full.empty:
        csv_res = res_full.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download CPU/RAM Resource Log (CSV)",
            data=csv_res,
            file_name=f"resources_cpu_ram_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=True,
            help="Resource logs tracking dashboard process, total host, and Docker containers."
        )
    else:
        st.button("⬇️ Download CPU/RAM Resource Log (CSV)", disabled=True, use_container_width=True)

# ---------------------------------------------------------------------------
# Continuous Refresh Loop (Streamlit Rerun Cadence)
# ---------------------------------------------------------------------------
if st.session_state.auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
