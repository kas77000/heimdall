"""Bifrost — where alerts are built and their history is read.

    streamlit run heimdall/bifrost/app.py      (from the project root, so the dark theme loads)
"""
from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run` puts this file's folder on the path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from heimdall.bifrost import common  # noqa: E402
from heimdall.bifrost.views import alerts, builder, history, overview, schedules, settings  # noqa: E402

st.set_page_config(page_title="Bifrost · Heimdall", page_icon=":material/visibility:",
                   layout="wide", initial_sidebar_state="expanded")

PAGES = {
    "Watch": [
        st.Page(overview.render, title="Overview", icon=":material/monitor_heart:", url_path="overview", default=True),
        st.Page(history.render, title="History", icon=":material/history:", url_path="history"),
    ],
    "Build": [
        st.Page(alerts.render, title="Alerts", icon=":material/notifications:", url_path="alerts"),
        st.Page(builder.render, title="Builder", icon=":material/construction:", url_path="builder"),
        st.Page(schedules.render, title="Schedules", icon=":material/calendar_month:", url_path="schedules"),
    ],
    "Admin": [
        st.Page(settings.render, title="Settings", icon=":material/settings:", url_path="settings"),
    ],
}
nav = st.navigation(PAGES, position="sidebar")
st.session_state["_pages"] = {p.url_path: p for group in PAGES.values() for p in group}


@st.fragment(run_every=15)
def _service_strip() -> None:
    status = common.call(lambda: common.huginn().get("/status"), quiet=True)
    if status is None:
        st.markdown(":red-badge[:material/cloud_off: Huginn offline]")
        return
    eng = status["engine"]
    if eng["paused"]:
        st.markdown(":orange-badge[:material/pause: Huginn paused]")
    else:
        st.markdown(":green-badge[:material/radar: Huginn watching]")
    seen = eng.get("sleipnir_seen")
    age = None
    if seen:
        age = (common.parse(status["now"]) - common.parse(seen)).total_seconds()
    if age is not None and age < 90:
        st.markdown(":green-badge[:material/schedule: Sleipnir on time]")
    else:
        st.markdown(":gray-badge[:material/schedule: Sleipnir not seen]")
    unack = status["today"]["unacknowledged"]
    if unack:
        st.markdown(f":red-badge[{unack} to acknowledge]")


with st.sidebar:
    st.markdown("### Heimdall")
    st.caption("Huginn checks · Muninn remembers · Sleipnir keeps time")
    _service_strip()

nav.run()
