"""
app.py — Main Streamlit Dashboard for the 5G Core Observability Platform.

Layout (single-page, auto-refreshing):
  - Sidebar     : connection settings, simulator toggle, refresh control
  - Hero row    : 4 KPI cards (UEs registered, PDU sessions, NFs online, avg latency)
  - NF Health   : status badge grid for AMF/SMF/UPF/PCF/UDM/NRF
  - Charts row  : latency time-series (Plotly), UPF throughput gauge, PDU donut
  - Tables      : UE Contexts table, PDU Sessions table
  - Performance : latency summary stats, container resource usage
  - Export panel: download CSV / PNG buttons
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
    NF_REGISTRY, NF_ORDER, REFRESH_INTERVAL_SEC, PERF_LOG_CSV, EXPORT_DIR, DATA_DIR
)
from collector import CollectorService
from metrics import MetricsStore

# ---------------------------------------------------------------------------
# Page config  (must be first Streamlit call)
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
# Custom CSS — dark glassmorphism theme
# ---------------------------------------------------------------------------
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

  html, body, [class*="css"] { 
    font-family: 'Inter', sans-serif;
  }

  /* High-contrast solid dark background */
  .stApp {
    background-color: #0b0f19;
    color: #f8fafc;
  }

  /* High-contrast Sidebar */
  [data-testid="stSidebar"] {
    background-color: #0f172a !important;
    border-right: 1.5px solid #1e3a5f !important;
  }
  [data-testid="stSidebar"] * {
    color: #f1f5f9 !important;
  }

  /* KPI cards */
  .kpi-card {
    background: #111c30;
    border: 1.5px solid #1e3a5f;
    border-top: 3px solid #38bdf8;
    border-radius: 14px;
    padding: 22px 16px;
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
    font-size: 2.9rem;
    font-weight: 800;
    line-height: 1.1;
    color: #38bdf8;
    text-shadow: 0 0 24px rgba(56, 189, 248, 0.45);
  }
  .kpi-label {
    font-size: 0.84rem;
    font-weight: 700;
    color: #e2e8f0;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-top: 8px;
  }
  .kpi-icon { font-size: 1.8rem; margin-bottom: 6px; }

  /* NF status badges — bold, high-contrast, fully readable */
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

  /* Section headers */
  .section-header {
    font-size: 1.15rem;
    font-weight: 700;
    color: #38bdf8;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin: 32px 0 16px 0;
    padding-bottom: 8px;
    border-bottom: 2px solid #1e3a5f;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  /* Timestamp badge */
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

  /* Simulator warning banner */
  .sim-banner {
    background: #422006;
    border: 1.5px solid #f59e0b;
    border-radius: 12px;
    padding: 14px 20px;
    font-size: 0.92rem;
    color: #fef08a;
    font-weight: 500;
    margin-bottom: 18px;
  }

  /* DataFrames and tables */
  [data-testid="stDataFrame"] {
    border: 1.5px solid #1e3a5f !important;
    border-radius: 12px !important;
    background: #0f172a !important;
  }

  /* Allow Streamlit header and settings menu to be visible and accessible */
  header {
    background-color: transparent !important;
  }
  footer { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Session-state initialisation
# ---------------------------------------------------------------------------
if "metrics_store" not in st.session_state:
    st.session_state.metrics_store = MetricsStore(csv_path=PERF_LOG_CSV)
if "collector"     not in st.session_state:
    st.session_state.collector = CollectorService(st.session_state.metrics_store, use_simulator=True)
if "snapshot"      not in st.session_state:
    st.session_state.snapshot = None
if "last_refresh"  not in st.session_state:
    st.session_state.last_refresh = 0.0
if "auto_refresh"  not in st.session_state:
    st.session_state.auto_refresh = True
if "refresh_count" not in st.session_state:
    st.session_state.refresh_count = 0

metrics_store: MetricsStore = st.session_state.metrics_store

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("""
    <div style="text-align:center; padding: 16px 0 24px;">
        <div style="font-size:2.5rem;">📡</div>
        <div style="font-size:1.15rem; font-weight:700; color:#63B3ED;">5G Core</div>
        <div style="font-size:0.78rem; color:#64748b; letter-spacing:0.12em;">OBSERVABILITY DASHBOARD</div>
    </div>""", unsafe_allow_html=True)

    st.markdown("---")

    # Simulator toggle
    use_sim = st.toggle("🎮 Simulator Mode", value=True,
                        help="Enable to use generated mock data when the 5G Core containers are not running.")
    if use_sim != st.session_state.collector.use_simulator:
        st.session_state.collector.use_simulator = use_sim

    st.markdown("---")
    st.markdown("**⚙️ Connection Settings**")

    core_host = st.text_input("Core Host", value="127.0.0.1", help="IP/hostname of the 5G Core node")
    st.caption("Port mapping from docker-compose.5gcore.yml")

    st.markdown("---")
    st.markdown("**🔄 Refresh Settings**")
    auto_refresh = st.toggle("Auto-refresh", value=st.session_state.auto_refresh)
    st.session_state.auto_refresh = auto_refresh
    refresh_interval = st.slider("Interval (s)", 2, 30, REFRESH_INTERVAL_SEC)

    if st.button("🔁 Refresh Now", use_container_width=True):
        st.session_state.last_refresh = 0.0   # force immediate refresh

    st.markdown("---")
    st.markdown("**📊 Dashboard Stats**")
    st.metric("Total Refreshes", st.session_state.refresh_count)
    st.metric("Log Entries",     len(metrics_store.get_latency_df()))


# ---------------------------------------------------------------------------
# Data collection (auto-refresh logic)
# ---------------------------------------------------------------------------
now = time.time()
if (now - st.session_state.last_refresh) >= refresh_interval or st.session_state.snapshot is None:
    with st.spinner("Polling 5G Core Network Functions…"):
        st.session_state.snapshot = st.session_state.collector.collect_all()
        st.session_state.collector.metrics_store.record_system_resources()
    st.session_state.last_refresh = time.time()
    st.session_state.refresh_count += 1

snapshot = st.session_state.snapshot

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
col_title, col_ts = st.columns([3, 1])
with col_title:
    st.markdown("""
    <h1 style="font-size:2.1rem; font-weight:800; color:#ffffff; margin:0; padding:0; letter-spacing:-0.01em;">
        📡 5G Core Observability & Analytics
    </h1>
    <p style="color:#93c5fd; font-size:0.92rem; font-weight:500; margin:6px 0 0 0;">
        3GPP Rel-16 · Open5GS / free5GC · Service-Based Interface (SBI)
    </p>
    """, unsafe_allow_html=True)
with col_ts:
    ts_str = datetime.fromisoformat(snapshot["timestamp"].replace("Z", "+00:00")).strftime("%H:%M:%S UTC")
    st.markdown(f"""
    <div style="text-align:right; padding-top:14px;">
        <span class="ts-badge">⏱ Updated: {ts_str}</span>
    </div>""", unsafe_allow_html=True)

# Simulator banner
if any(v.get("simulated") for v in snapshot["nf_health"].values()):
    st.markdown("""
    <div class="sim-banner">
        ⚠️ <strong style="color:#ffffff;">Simulator Mode Active</strong> — The dashboard is generating synthetic 5G Core telemetry.
        When the Docker containers are running, disable the Simulator in the sidebar.
    </div>""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# KPI Hero Row
# ---------------------------------------------------------------------------
kpis = snapshot["kpis"]
k1, k2, k3, k4 = st.columns(4)
kpi_defs = [
    (k1, "👥", kpis["registered_ues"], "Registered UEs"),
    (k2, "🔗", kpis["active_pdu"],     "Active PDU Sessions"),
    (k3, "✅", kpis["nfs_online"],     "NFs Online"),
    (k4, "⚡", f"{kpis['avg_latency_ms']} ms", "Avg SBI Latency"),
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
# NF Health Status Grid
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">🏥 Network Function Health (NF Health)</div>', unsafe_allow_html=True)
health = snapshot["nf_health"]
cols = st.columns(len(NF_ORDER))
for col, nf in zip(cols, NF_ORDER):
    meta   = NF_REGISTRY[nf]
    status = health.get(nf, {})
    online = status.get("online", False)
    simulated = status.get("simulated", True)
    lat    = status.get("latency_ms", 0)
    emoji  = meta["emoji"]
    label  = nf
    lat_str = f"{lat:.1f} ms"

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
            <div style="font-size:1.05rem; font-weight:800; color:#ffffff; margin-bottom:4px;">{emoji} {label}</div>
            <div style="font-size:0.8rem; font-weight:700; color:{status_color};">{badge_status}</div>
            <div style="font-size:0.8rem; font-weight:600; color:#ffffff; margin-top:2px;">⏱ {lat_str}</div>
        </div>""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Charts Row
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">📈 Real-Time Telemetry</div>', unsafe_allow_html=True)

chart_col1, chart_col2, chart_col3 = st.columns([2, 1, 1])

# — Latency time-series
with chart_col1:
    st.markdown("<strong style='color:#f8fafc; font-size:1rem;'>SBI Call Latency (ms)</strong>", unsafe_allow_html=True)
    lat_df = metrics_store.get_all_latency_history()
    if not lat_df.empty:
        lat_df["ts"] = pd.to_datetime(lat_df["ts"])
        fig_lat = px.line(
            lat_df, x="ts", y="latency_ms", color="nf_name",
            labels={"ts": "Time", "latency_ms": "Latency (ms)", "nf_name": "Network Function"},
            color_discrete_map={
                "NRF": "#818cf8", "AMF": "#34d399", "SMF": "#fbbf24",
                "UPF": "#f87171", "PCF": "#38bdf8", "UDM": "#a3e635",
            },
        )
        fig_lat.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(15,23,42,0.6)",
            font_color="#f1f5f9",
            legend=dict(font=dict(color="#f8fafc", size=12), bgcolor="rgba(15,23,42,0.85)"),
            margin=dict(l=0, r=0, t=10, b=0), height=270,
            xaxis=dict(gridcolor="rgba(255,255,255,0.12)", tickfont=dict(color="#cbd5e1")),
            yaxis=dict(gridcolor="rgba(255,255,255,0.12)", tickfont=dict(color="#cbd5e1")),
        )
        st.plotly_chart(fig_lat, use_container_width=True)
    else:
        st.info("Collecting latency measurements…")

# — UPF throughput gauge
with chart_col2:
    st.markdown("<strong style='color:#f8fafc; font-size:1rem;'>UPF Throughput (User Plane)</strong>", unsafe_allow_html=True)
    upf_data = snapshot["upf"]["data"]
    dl_mbps  = upf_data.get("upf_throughput_dl_mbps", 0)
    ul_mbps  = upf_data.get("upf_throughput_ul_mbps", 0)

    fig_gauge = go.Figure()
    fig_gauge.add_trace(go.Indicator(
        mode="gauge+number",
        value=dl_mbps,
        title={"text": "Download (DL Mbps)", "font": {"color": "#f1f5f9", "size": 13}},
        number={"font": {"color": "#38bdf8", "size": 32, "weight": 800}},
        gauge={
            "axis": {"range": [0, 1000], "tickcolor": "#cbd5e1", "tickfont": {"color": "#e2e8f0"}},
            "bar":  {"color": "#38bdf8"},
            "bgcolor": "rgba(15,23,42,0.7)",
            "bordercolor": "rgba(255,255,255,0.2)",
            "steps": [
                {"range": [0, 400],   "color": "rgba(16,185,129,0.25)"},
                {"range": [400, 700], "color": "rgba(245,158,11,0.25)"},
                {"range": [700, 1000],"color": "rgba(239,68,68,0.25)"},
            ],
        },
    ))
    fig_gauge.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", font_color="#f8fafc",
        margin=dict(l=20, r=20, t=35, b=10), height=220,
    )
    st.plotly_chart(fig_gauge, use_container_width=True)
    st.markdown(f"<div style='text-align:center; font-weight:700; color:#34d399; font-size:1.1rem;'>⬆️ Upload: {ul_mbps:.1f} Mbps</div>", unsafe_allow_html=True)

# — PDU session donut
with chart_col3:
    st.markdown("<strong style='color:#f8fafc; font-size:1rem;'>PDU Sessions by DNN</strong>", unsafe_allow_html=True)
    pdu_data = snapshot["smf"]["data"]
    if pdu_data:
        df_pdu = pd.DataFrame(pdu_data)
        if "dnn" in df_pdu.columns:
            dnn_counts = df_pdu["dnn"].value_counts().reset_index()
            dnn_counts.columns = ["dnn", "count"]
            fig_donut = px.pie(
                dnn_counts, values="count", names="dnn", hole=0.55,
                color_discrete_sequence=["#818cf8", "#34d399", "#fbbf24", "#f87171"],
            )
            fig_donut.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", font_color="#f8fafc",
                legend=dict(font=dict(color="#f8fafc", size=11), bgcolor="rgba(15,23,42,0.7)"),
                showlegend=True,
                margin=dict(l=0, r=0, t=10, b=0), height=220,
            )
            fig_donut.update_traces(textposition="inside", textinfo="percent+label", textfont=dict(color="#ffffff", size=12))
            st.plotly_chart(fig_donut, use_container_width=True)
    else:
        st.info("No PDU session data.")

# ---------------------------------------------------------------------------
# UE Contexts Table
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">👥 Registered UE Contexts (AMF)</div>', unsafe_allow_html=True)
ue_data = snapshot["amf"]["data"]
if ue_data:
    df_ue = pd.DataFrame(ue_data)
    display_cols = [c for c in ["supi", "state", "ran_id", "slice", "registered_at"] if c in df_ue.columns]
    df_ue_display = df_ue[display_cols].copy()
    col_rename = {"supi": "SUPI (IMSI)", "state": "State", "ran_id": "RAN Node",
                  "slice": "S-NSSAI", "registered_at": "Registered At"}
    df_ue_display.rename(columns={k: v for k, v in col_rename.items() if k in df_ue_display.columns}, inplace=True)

    # Colour-code State column
    def state_colour(val):
        if val == "REGISTERED":
            return "color: #00C9A7; font-weight:600"
        elif val == "IDLE":
            return "color: #F7B731; font-weight:600"
        return "color: #FC5C65"

    if "State" in df_ue_display.columns:
        style_mapper = getattr(df_ue_display.style, "map", getattr(df_ue_display.style, "applymap", None))
        if style_mapper:
            styled = style_mapper(state_colour, subset=["State"])
            st.dataframe(styled, use_container_width=True, height=260)
        else:
            st.dataframe(df_ue_display, use_container_width=True, height=260)
    else:
        st.dataframe(df_ue_display, use_container_width=True, height=260)
else:
    st.warning("No UE context data available.")

# ---------------------------------------------------------------------------
# PDU Sessions Table
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">🔗 Active PDU Sessions (SMF)</div>', unsafe_allow_html=True)
if pdu_data:
    df_smf = pd.DataFrame(pdu_data)
    pdu_cols = [c for c in ["supi", "pdu_session_id", "dnn", "ipv4_address",
                             "ambr_dl_kbps", "ambr_ul_kbps", "state", "upf_node_id"] if c in df_smf.columns]
    df_smf_display = df_smf[pdu_cols].copy()
    pdu_rename = {
        "supi": "SUPI", "pdu_session_id": "PSI",
        "dnn": "DNN", "ipv4_address": "UE IPv4",
        "ambr_dl_kbps": "AMBR DL (kbps)", "ambr_ul_kbps": "AMBR UL (kbps)",
        "state": "State", "upf_node_id": "UPF Node",
    }
    df_smf_display.rename(columns={k: v for k, v in pdu_rename.items() if k in df_smf_display.columns}, inplace=True)
    st.dataframe(df_smf_display, use_container_width=True, height=240)
else:
    st.warning("No PDU session data available.")

# ---------------------------------------------------------------------------
# NRF Registered Instances
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">🗂️ NRF — Registered NF Instances</div>', unsafe_allow_html=True)
nrf_data = snapshot["nrf"]["data"]
if nrf_data:
    df_nrf = pd.DataFrame(nrf_data)
    nrf_cols = [c for c in ["nfType", "nfInstanceId", "nfStatus", "load", "capacity"] if c in df_nrf.columns]
    st.dataframe(df_nrf[nrf_cols] if nrf_cols else df_nrf, use_container_width=True, height=200)

# ---------------------------------------------------------------------------
# Performance Analysis
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">⚡ SBI Performance Statistics</div>', unsafe_allow_html=True)
perf_col1, perf_col2 = st.columns([2, 1])

with perf_col1:
    summary_df = metrics_store.get_summary_stats()
    if not summary_df.empty:
        st.dataframe(
            summary_df.rename(columns={
                "nf_name":     "NF",
                "samples":     "Samples",
                "mean_ms":     "Mean (ms)",
                "min_ms":      "Min (ms)",
                "max_ms":      "Max (ms)",
                "p95_ms":      "P95 (ms)",
                "success_rate":"Success %",
            }),
            use_container_width=True,
        )
    else:
        st.info("Accumulating statistics…")

with perf_col2:
    # Resource mini chart
    res_df = metrics_store.get_resource_history_df()
    if not res_df.empty:
        fig_res = px.area(
            res_df.tail(60), x="timestamp", y="host_cpu_pct",
            labels={"timestamp": "", "host_cpu_pct": "CPU %"},
            color_discrete_sequence=["#6C63FF"],
        )
        fig_res.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font_color="#94a3b8", margin=dict(l=0,r=0,t=10,b=0), height=180,
            xaxis=dict(visible=False), yaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
        )
        st.markdown("**Dashboard CPU %**")
        st.plotly_chart(fig_res, use_container_width=True)

# ---------------------------------------------------------------------------
# Container Stats
# ---------------------------------------------------------------------------
containers = metrics_store.get_latest_containers()
if containers:
    st.markdown('<div class="section-header">🐳 Docker Container Resources</div>', unsafe_allow_html=True)
    df_cont = pd.DataFrame(containers)
    st.dataframe(df_cont, use_container_width=True)

# ---------------------------------------------------------------------------
# Export Panel
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">📤 Export & Reports</div>', unsafe_allow_html=True)
exp_col1, exp_col2, exp_col3 = st.columns(3)

with exp_col1:
    lat_df_full = metrics_store.get_latency_df()
    if not lat_df_full.empty:
        csv_bytes = lat_df_full.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download Latency Log (CSV)",
            data=csv_bytes,
            file_name=f"latency_log_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=True,
        )

with exp_col2:
    summary_df2 = metrics_store.get_summary_stats()
    if not summary_df2.empty:
        csv_sum = summary_df2.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download Summary Stats (CSV)",
            data=csv_sum,
            file_name=f"summary_stats_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=True,
        )

with exp_col3:
    if ue_data:
        df_ue_export = pd.DataFrame(ue_data)
        csv_ue = df_ue_export.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download UE Contexts (CSV)",
            data=csv_ue,
            file_name=f"ue_contexts_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=True,
        )

# ---------------------------------------------------------------------------
# Auto-refresh (Streamlit rerun loop)
# ---------------------------------------------------------------------------
if st.session_state.auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
