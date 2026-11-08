"""Overview: is anything wrong right now, and what is everything doing."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from heimdall.bifrost import common as c

ORDER = {"triggered": 0, "error": 1, "running": 2, "ok": 3, "never": 4, "off": 5}
DOT = {"triggered": "● Fired", "error": "● Error", "running": "◌ Running", "ok": "● OK",
       "never": "○ Not run yet", "off": "○ Off"}


def go(page: str, **state) -> None:
    for k, v in state.items():
        st.session_state[k] = v
    st.switch_page(st.session_state["_pages"][page])


def render() -> None:
    c.page_header("Overview")
    _live()


@st.fragment(run_every=10)
def _live() -> None:
    status = c.call(lambda: c.huginn().get("/status"))
    if status is None:
        return
    eng, today, alerts = status["engine"], status["today"], status["alerts"]

    top = st.columns([3, 1], vertical_alignment="center")
    with top[0]:
        if eng["paused"]:
            st.markdown(":orange-badge[:material/pause: Paused] Huginn is not running alerts on their "
                        "intervals. Schedules and runs by hand still work.")
        else:
            st.caption(f"Huginn last looked {c.ago(eng['last_tick'])} · "
                       f"running since {c.local(eng['started_at'], '%a %d %b %H:%M')}")
    with top[1]:
        if eng["paused"]:
            if st.button("Resume checking", icon=":material/play_arrow:", type="primary", width="stretch"):
                c.call(lambda: c.huginn().post("/engine/resume"))
                st.rerun()
        elif st.button("Pause all checks", icon=":material/pause:", width="stretch"):
            c.call(lambda: c.huginn().post("/engine/pause"))
            st.rerun()

    k = st.columns(4)
    k[0].metric("Runs today", f"{today['runs']:,}")
    k[1].metric("Fired today", f"{today['firings']:,}")
    k[2].metric("Errors today", f"{today['errors']:,}")
    k[3].metric("To acknowledge", f"{today['unacknowledged']:,}")

    if not alerts:
        st.info("**No alerts yet.** Build one in the Builder, or start Huginn with `--demo` "
                "to load three working examples.", icon=":material/lightbulb:")
        if st.button("Open the Builder", icon=":material/construction:", type="primary"):
            go("builder", builder_request={"id": None})
        return

    for a in alerts:
        a["_status"] = c.status_key(a)

    attention = [a for a in alerts if a["enabled"] and a["_status"] in ("triggered", "error")
                 or a["unacknowledged"]]
    attention.sort(key=lambda a: (ORDER.get(a["_status"], 9), a["name"].lower()))
    st.markdown("#### Needs attention")
    if not attention:
        st.caption("Nothing has fired or failed. Every enabled alert's last check was fine.")
    for a in attention:
        _attention_row(a)

    st.markdown("#### All alerts")
    _alerts_table(alerts)


def _attention_row(a: dict) -> None:
    with st.container(border=True):
        cols = st.columns([5, 2, 1, 1, 1], vertical_alignment="center")
        with cols[0]:
            unack = f" :red[{a['unacknowledged']} unacknowledged]" if a["unacknowledged"] else ""
            st.markdown(f"{c.badge(a['_status'])} **{a['name']}**{unack}")
            msg = a.get("last_message") or ""
            st.caption(msg.split(": ", 1)[-1] if msg else a["condition"])
        cols[1].caption(f"Last run {c.ago(a['last_run_at'])}")
        if cols[2].button("History", key=f"h{a['id']}", icon=":material/history:", width="stretch"):
            go("history", history_alert=a["id"], history_run=a.get("last_run_id"))
        if cols[3].button("Ack", key=f"a{a['id']}", icon=":material/done_all:", width="stretch",
                          disabled=not a["unacknowledged"], help="Acknowledge every firing of this alert"):
            c.call(lambda: c.huginn().post("/runs/acknowledge", {"alert_id": a["id"]}))
            st.rerun(scope="fragment")
        if cols[4].button("Run", key=f"r{a['id']}", icon=":material/play_arrow:", width="stretch",
                          help="Run it now and record the result"):
            with st.spinner("Running…"):
                c.call(lambda: c.huginn().post(f"/alerts/{a['id']}/run"))
            st.rerun(scope="fragment")


def _alerts_table(alerts: list[dict]) -> None:
    rows = sorted(alerts, key=lambda a: (ORDER.get(a["_status"], 9), a["name"].lower()))
    df = pd.DataFrame([{
        "Status": DOT[a["_status"]],
        "Alert": a["name"],
        "Sources": " · ".join(c.KIND[k][0] for k in a["kinds"]),
        "Checks": c.every(a["interval_secs"]) if a["enabled"] else "Disabled",
        "Last run": c.ago(a["last_run_at"]),
        "Next": c.ago(a["next_due_at"]) if a["next_due_at"] else "",
        "Unack.": str(a["unacknowledged"]) if a["unacknowledged"] else "",
        "Condition": a["condition"],
    } for a in rows])
    event = st.dataframe(c.coloured(df, "Status"), hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key="overview_table", height=c.height(len(df)),
                         column_config={"Status": st.column_config.TextColumn(width="small"),
                                        "Unack.": st.column_config.TextColumn(width="small"),
                                        "Condition": st.column_config.TextColumn(width="large")})
    picked = event.selection.rows if event and event.selection else []
    if not picked:
        st.caption("Select a row to run, switch on or off, or edit that alert.")
        return
    a = rows[picked[0]]
    bar = st.columns([1, 1, 1, 1, 3])
    if bar[0].button("Run now", icon=":material/play_arrow:", type="primary", width="stretch"):
        with st.spinner(f"Running {a['name']}…"):
            run = c.call(lambda: c.huginn().post(f"/alerts/{a['id']}/run"))
        if run:
            st.toast(f"{a['name']}: {c.STATUS[run['status']][0]}", icon=c.STATUS[run["status"]][2])
        st.rerun(scope="fragment")
    label, path = ("Switch off", "disable") if a["enabled"] else ("Switch on", "enable")
    if bar[1].button(label, icon=":material/power_settings_new:", width="stretch"):
        c.call(lambda: c.huginn().post(f"/alerts/{a['id']}/{path}"))
        st.rerun(scope="fragment")
    if bar[2].button("Edit", icon=":material/edit:", width="stretch"):
        go("builder", builder_request={"id": a["id"]})
    if bar[3].button("History", icon=":material/history:", width="stretch"):
        go("history", history_alert=a["id"], history_run=None)
