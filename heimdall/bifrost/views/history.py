"""History: every run and every change, what each one saw, and cleaning it up."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from heimdall.bifrost import common as c

PERIODS = {"Last hour": timedelta(hours=1), "Today": None, "24 hours": timedelta(days=1),
           "7 days": timedelta(days=7), "30 days": timedelta(days=30), "Everything": "all"}
STATUSES = {"Any status": None, "Fired": "triggered", "OK": "ok", "Error": "error"}
PAGE = 200


def render() -> None:
    c.page_header("History", "Every check Huginn made, every notification it sent, every change made to an alert.")
    q = st.query_params
    if "run" in q:
        try:
            st.session_state["history_run"] = int(q["run"])
        except ValueError:
            pass
        del st.query_params["run"]
    view = st.segmented_control("View", ["Runs", "Changes", "Clean up"], default="Runs",
                                key="history_view", label_visibility="collapsed")
    if view == "Changes":
        _events()
    elif view == "Clean up":
        _cleanup()
    else:
        _runs()


def _alerts() -> list[dict]:
    return c.call(lambda: c.huginn().get("/alerts"), quiet=True) or []


def _runs() -> None:
    alerts = _alerts()
    names = {a["id"]: a["name"] for a in alerts}
    preset = st.session_state.pop("history_alert", None)
    if preset is not None:
        st.session_state["h_alert"] = preset
    f = st.columns([2, 1, 1, 1, 1], vertical_alignment="bottom")
    alert_id = f[0].selectbox("Alert", [None] + list(names), format_func=lambda i: "All alerts" if i is None else names.get(i, f"#{i}"),
                              key="h_alert")
    status = STATUSES[f[1].selectbox("Status", list(STATUSES), key="h_status")]
    period = f[2].selectbox("When", list(PERIODS), index=3, key="h_period")
    unack = f[3].toggle("Unacknowledged", key="h_unack", help="Firings nobody has acknowledged yet")
    page = int(f[4].number_input("Page", min_value=1, value=1, key="h_page"))

    now = datetime.now(timezone.utc)
    span = PERIODS[period]
    since = None if span == "all" else (now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
                                         if span is None else now - span)
    res = c.call(lambda: c.huginn().get("/runs", alert_id=alert_id, status=status,
                                        since=since.isoformat() if since else None,
                                        unacknowledged=unack or None, limit=PAGE, offset=(page - 1) * PAGE))
    if res is None:
        return
    runs, total = res["runs"], res["total"]

    focus = st.session_state.pop("history_run", None)
    if focus:
        _detail(focus)
        st.divider()

    if not runs:
        st.caption("No runs match. Alerts appear here as soon as Huginn has checked them once.")
        return
    daily = c.call(lambda: c.huginn().get("/history/daily", days=14, alert_id=alert_id), quiet=True) or []
    if daily and len(daily) > 1:
        chart = pd.DataFrame(daily).rename(columns={"day": "Day", "firings": "Fired", "errors": "Errors"})
        chart["OK"] = chart["runs"] - chart["Fired"].fillna(0) - chart["Errors"].fillna(0)
        st.bar_chart(chart.set_index("Day")[["OK", "Fired", "Errors"]], height=150,
                     color=["#3ecf8e", "#f06470", "#e8a33d"], stack=True)

    df = pd.DataFrame([{
        "When": c.local(r["started_at"]),
        "Alert": r["alert_name"],
        "Result": {"triggered": "● Fired" if r["triggered"] else "● Still firing", "ok": "● OK",
                   "error": "● Error"}[r["status"]],
        "Rows": r["row_count"],
        "Told": {"trigger": "Fired", "error": "Error", "recovery": "Cleared"}.get(r["notified"] or "", ""),
        "Started by": c.SOURCE.get(r["source"], r["source"]),
        "Took": f"{r['duration']:.2f}s",
        "Ack": "✓" if r.get("acknowledged_at") else ("pending" if r["triggered"] else ""),
        "Message": (r.get("error") or r["message"].split(": ", 1)[-1]),
    } for r in runs])
    st.caption(f"{total:,} run(s) · showing {(page - 1) * PAGE + 1}–{(page - 1) * PAGE + len(runs)}")
    event = st.dataframe(c.coloured(df, "Result"), hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="multi-row", key="history_table", height=c.height(len(df)),
                         column_config={"Message": st.column_config.TextColumn(width="large"),
                                        "Rows": st.column_config.NumberColumn(width="small")})
    picked = [runs[i] for i in (event.selection.rows if event and event.selection else [])]
    if not picked:
        st.caption("Select a run to see what each step asked and returned.")
        return
    firing = [r["id"] for r in picked if r["triggered"] and not r.get("acknowledged_at")]
    if firing and st.button(f"Acknowledge {len(firing)} firing(s)", icon=":material/done_all:"):
        c.call(lambda: c.huginn().post("/runs/acknowledge", {"run_ids": firing}))
        st.rerun()
    for r in picked[:3]:
        _detail(r["id"])
    if len(picked) > 3:
        st.caption(f"Showing the first 3 of {len(picked)} selected runs.")


def _detail(run_id: int) -> None:
    r = c.call(lambda: c.huginn().get(f"/runs/{run_id}"))
    if r is None:
        return
    key = "triggered" if r["status"] == "triggered" else r["status"]
    with st.container(border=True):
        top = st.columns([4, 1], vertical_alignment="center")
        top[0].markdown(f"{c.badge(key)} **{r['alert_name']}** · run {r['id']} · {c.local(r['started_at'])} "
                        f"· {c.SOURCE.get(r['source'], r['source']).lower()}")
        if r["triggered"] and not r.get("acknowledged_at"):
            if top[1].button("Acknowledge", key=f"ack{r['id']}", icon=":material/done:", width="stretch"):
                c.call(lambda: c.huginn().post("/runs/acknowledge", {"run_ids": [r["id"]]}))
                st.rerun()
        elif r.get("acknowledged_at"):
            top[1].caption(f"Acknowledged {c.ago(r['acknowledged_at'])}"
                           + (f" by {r['acknowledged_by']}" if r.get("acknowledged_by") else ""))
        st.caption(r.get("error") or r["message"])
        steps = pd.DataFrame([{"Step": s["name"], "Kind": c.KIND.get(s["kind"], (s["kind"],))[0],
                               "Rows": s["rows"], "Took": f"{s['duration']:.2f}s",
                               "Request": s["request"], "Error": s["error"] or ""} for s in r["steps"]])
        if not steps.empty:
            st.dataframe(steps, hide_index=True, width="stretch",
                         column_config={"Request": st.column_config.TextColumn(width="large")})
        snap = c.table(r.get("snapshot"))
        if not snap.empty:
            st.markdown("**What the condition judged**")
            st.dataframe(snap, hide_index=True, width="stretch", height=min(38 + 35 * len(snap), 300))
            total = (r.get("snapshot") or {}).get("total", 0)
            if total > len(snap):
                st.caption(f"First {len(snap)} of {total} rows were kept.")
        for d in r.get("deliveries", []):
            icon = ":material/check:" if d["ok"] else ":material/close:"
            colour = "green" if d["ok"] else "red"
            st.markdown(f":{colour}[{icon}] {d['channel']} → {d['target']}"
                        + (f" · :red[{d['error']}]" if d["error"] else ""))


def _events() -> None:
    alerts = _alerts()
    names = {a["id"]: a["name"] for a in alerts}
    alert_id = st.selectbox("Alert", [None] + list(names), key="e_alert",
                            format_func=lambda i: "All alerts" if i is None else names[i])
    events = c.call(lambda: c.huginn().get("/events", alert_id=alert_id, limit=500))
    if not events:
        st.caption("No changes recorded.")
        return
    df = pd.DataFrame([{"When": c.local(e["at"]), "Who": e["actor"], "What": e["action"],
                        "Alert": e["alert_name"], "Detail": e["detail"]} for e in events])
    st.dataframe(df, hide_index=True, width="stretch", height=520,
                 column_config={"Detail": st.column_config.TextColumn(width="large")})


def _cleanup() -> None:
    stats = c.call(lambda: c.huginn().get("/history/stats", days=1))
    if stats is None:
        return
    oldest = c.local(stats["oldest_run"], "%Y-%m-%d") if stats["oldest_run"] else "none"
    st.caption(f"Muninn holds **{stats['history_runs']:,}** run(s); the oldest is from {oldest}.")
    alerts = _alerts()
    names = {a["id"]: a["name"] for a in alerts}

    st.markdown("#### Clean up now")
    how = st.radio("Remove", ["older", "keep", "alert"], horizontal=True, key="cl_how",
                   format_func={"older": "Runs older than…", "keep": "All but the newest runs of each alert",
                                "alert": "Everything for one alert"}.get)
    body: dict = {}
    a, b = st.columns(2)
    if how == "older":
        body["older_than_days"] = a.number_input("Days", min_value=0.0, value=30.0, step=1.0, key="cl_days",
                                                 help="Changes older than this go too")
        scope = b.selectbox("For", [None] + list(names), key="cl_scope1",
                            format_func=lambda i: "All alerts" if i is None else names[i])
        body["alert_id"] = scope
    elif how == "keep":
        body["keep_last"] = int(a.number_input("Keep this many per alert", min_value=0, value=500, step=50, key="cl_keep"))
        body["alert_id"] = b.selectbox("For", [None] + list(names), key="cl_scope2",
                                       format_func=lambda i: "All alerts" if i is None else names[i])
    else:
        others = _orphans(alerts)
        options = list(names) + list(others)
        if not options:
            st.caption("Nothing to choose from.")
            return
        body["alert_id"] = a.selectbox("Alert", options, key="cl_scope3",
                                       format_func=lambda i: names.get(i) or f"{others[i]} (deleted)")
        body["everything"] = True
    body["keep_unacknowledged"] = st.checkbox("Keep firings nobody has acknowledged yet", value=True, key="cl_unack")

    preview = c.call(lambda: c.huginn().post("/history/purge", body | {"dry_run": True}), quiet=True)
    if preview is None:
        return
    n, e = preview["runs"], preview["events"]
    if not n and not e:
        st.caption("Nothing matches, so nothing would be removed.")
    else:
        st.markdown(f"This removes **{n:,} run(s)**" + (f" and **{e:,} change record(s)**" if e else "")
                    + ". It cannot be undone.")
        if st.button(f"Remove {n:,} run(s)", type="primary", icon=":material/delete_sweep:", key="cl_go"):
            done = c.call(lambda: c.huginn().post("/history/purge", body))
            if done:
                st.toast(f"Removed {done['runs']:,} run(s)", icon=":material/delete_sweep:")
                st.rerun()

    st.markdown("#### Clean up automatically")
    settings = c.call(lambda: c.huginn().get("/settings"), quiet=True) or {}
    ret = settings.get("retention") or {}
    a, b = st.columns(2)
    days = a.number_input("Remove runs older than (days, 0 = never)", min_value=0,
                          value=int(ret.get("days") or 0), key="rt_days")
    keep = b.number_input("Keep at most this many per alert (0 = no limit)", min_value=0, step=100,
                          value=int(ret.get("keep_last") or 0), key="rt_keep")
    ku = st.checkbox("Never remove unacknowledged firings", value=bool(ret.get("keep_unacknowledged", True)), key="rt_ku")
    st.caption("Huginn applies this once an hour.")
    if st.button("Save cleanup policy", icon=":material/save:", key="rt_save"):
        c.call(lambda: c.huginn().put("/settings", {"retention": {"days": days or None,
                                                                   "keep_last": keep or None,
                                                                   "keep_unacknowledged": ku}}))
        st.toast("Cleanup policy saved", icon=":material/check:")


def _orphans(alerts: list[dict]) -> dict[int, str]:
    """Alerts deleted with their history kept: still cleanable by id."""
    live = {a["id"] for a in alerts}
    res = c.call(lambda: c.huginn().get("/runs", limit=2000), quiet=True) or {"runs": []}
    return {r["alert_id"]: r["alert_name"] for r in res["runs"] if r["alert_id"] not in live}
