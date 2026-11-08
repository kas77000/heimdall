"""Schedules: Sleipnir switches alerts on and off, or runs them, at set times."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from heimdall.bifrost import common as c
from heimdall.core import cron

ZONES = ["Europe/London", "Europe/Paris", "America/New_York", "Asia/Tokyo", "Asia/Hong_Kong",
         "Asia/Singapore", "Australia/Sydney", "UTC"]
ACTIONS = {"enable": "Switch on", "disable": "Switch off", "run": "Run it"}


def render() -> None:
    c.page_header("Schedules", "Sleipnir carries these out in each schedule's own time zone.")
    alerts = c.call(lambda: c.huginn().get("/alerts"))
    if alerts is None:
        return
    names = {a["id"]: a["name"] for a in alerts}
    schedules = c.call(lambda: c.huginn().get("/schedules")) or []
    status = c.call(lambda: c.huginn().get("/status"), quiet=True) or {}
    seen = (status.get("engine") or {}).get("sleipnir_seen")
    if not seen:
        st.warning("Sleipnir has not checked in since Huginn started, so nothing below will happen on time. "
                   "Start it with `python -m heimdall.sleipnir`.", icon=":material/schedule:")

    if schedules:
        df = pd.DataFrame([{
            "Active": s["active"],
            "Alert": names.get(s["alert_id"], f"#{s['alert_id']}"),
            "Does": ACTIONS[s["action"]],
            "When": s["describe"],
            "Next": f"{c.local(s['next_fire_at'], '%a %d %b %H:%M')} ({c.ago(s['next_fire_at'])})" if s.get("next_fire_at") else "",
            "Last": c.ago(s["last_fired_at"]) if s.get("last_fired_at") else "",
            "Note": s.get("note", ""),
        } for s in schedules])
        event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                             selection_mode="single-row", key="sched_table",
                             column_config={"Active": st.column_config.CheckboxColumn(width="small")})
        rows = event.selection.rows if event and event.selection else []
        if rows:
            s = schedules[rows[0]]
            bar = st.columns([1, 1, 4])
            if bar[0].button("Pause" if s["active"] else "Resume", icon=":material/pause:" if s["active"] else ":material/play_arrow:",
                             width="stretch"):
                body = {k: s[k] for k in ("alert_id", "action", "cron", "tz", "note")} | {"active": not s["active"]}
                c.call(lambda: c.huginn().put(f"/schedules/{s['id']}", body))
                st.rerun()
            if bar[1].button("Remove", icon=":material/delete:", width="stretch"):
                c.call(lambda: c.huginn().delete(f"/schedules/{s['id']}"))
                st.rerun()
    else:
        st.caption("No schedules yet.")

    st.markdown("#### Add a schedule")
    if not alerts:
        st.caption("Create an alert first.")
        return
    with st.container(border=True):
        a, b = st.columns([2, 1])
        alert_id = a.selectbox("Alert", list(names), format_func=names.get, key="s_alert")
        action = b.segmented_control("Do", list(ACTIONS), format_func=ACTIONS.get, default="enable", key="s_action")
        mode = st.radio("When", ["simple", "cron"], horizontal=True, key="s_mode",
                        format_func={"simple": "On these days at", "cron": "Cron expression"}.get)
        a, b, z = st.columns([2, 1, 1])
        expr, problem = "", None
        if mode == "simple":
            days = a.pills("Days", cron.DAYS, selection_mode="multi", default=list(cron.DAYS[:5]), key="s_days")
            at = b.time_input("At", value=None, key="s_time", step=300)
            try:
                expr = cron.simple([cron.DAYS.index(d) for d in days or []],
                                   at.strftime("%H:%M") if at else "")
            except cron.CronError as exc:
                problem = str(exc) if at else "Choose a time."
        else:
            expr = a.text_input("Cron", placeholder="30 16 * * 1-5", key="s_cron",
                                help="minute hour day-of-month month day-of-week").strip()
        tz = z.selectbox("Time zone", ZONES, key="s_tz", accept_new_options=True)
        note = st.text_input("Note", placeholder="Why (optional)", key="s_note")
        if expr and not problem:
            try:
                cron.validate(expr, tz)
                nxt = cron.next_fire(expr, tz, pd.Timestamp.now(tz="UTC").to_pydatetime())
                st.caption(f"{ACTIONS[action or 'enable']} **{names[alert_id]}** {cron.describe(expr, tz)}. "
                           f"Next: {nxt.astimezone().strftime('%a %d %b %H:%M')} your time.")
            except cron.CronError as exc:
                problem = str(exc)
        if problem:
            st.caption(f":orange[{problem}]")
        if action == "run":
            st.caption("A run that Sleipnir reaches more than five minutes late is skipped and noted in History.")
        if st.button("Add schedule", type="primary", icon=":material/add:", disabled=bool(problem or not expr or not action)):
            if c.call(lambda: c.huginn().post("/schedules", {"alert_id": alert_id, "action": action,
                                                              "cron": expr, "tz": tz, "note": note})):
                st.toast("Schedule added", icon=":material/check:")
                st.rerun()
