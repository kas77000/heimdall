"""Alerts: every definition, to open, copy, switch on and off, or delete."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from heimdall.bifrost import common as c
from heimdall.core import hours
from heimdall.core.models import ActiveHours
from heimdall.bifrost.views.overview import go


def render() -> None:
    head = st.columns([4, 1], vertical_alignment="bottom")
    with head[0]:
        c.page_header("Alerts")
    if head[1].button("New alert", icon=":material/add:", type="primary", width="stretch"):
        go("builder", builder_request={"id": None})

    alerts = c.call(lambda: c.huginn().get("/alerts"))
    if alerts is None:
        return
    if not alerts:
        st.info("No alerts yet. **New alert** opens the Builder.", icon=":material/lightbulb:")
        return

    groups = sorted({a.get("group") or "" for a in alerts} - {""}, key=str.lower)
    tags = sorted({t for a in alerts for t in a.get("tags", [])})
    f = st.columns([2, 2, 2, 1])
    q = f[0].text_input("Search", placeholder="Name, description or owner",
                        label_visibility="collapsed").strip().lower()
    picked_groups = f[1].multiselect("Groups", groups + [UNGROUPED], placeholder="Any group",
                                     label_visibility="collapsed", key="alerts_groups")
    picked_tags = f[2].multiselect("Tags", tags, placeholder="Any tag", label_visibility="collapsed")
    only_on = f[3].toggle("Enabled only")
    shown = [a for a in alerts
             if (not q or q in (a["name"] + a.get("description", "") + a.get("owner", "")).lower())
             and (not picked_groups or (a.get("group") or UNGROUPED) in picked_groups)
             and (not picked_tags or set(picked_tags) & set(a.get("tags", [])))
             and (not only_on or a["enabled"])]
    shown.sort(key=lambda a: (not a.get("group"), (a.get("group") or "").lower(), a["name"].lower()))

    if groups:
        _manage_groups(alerts, groups)

    df = pd.DataFrame([{
        "On": a["enabled"],
        "Group": a.get("group") or "",
        "Alert": a["name"],
        "Steps": " → ".join(f"{s['name']}" for s in a["steps"]),
        "Checks": c.every(a["interval_secs"]),
        "Hours": hours.describe(ActiveHours(**a["active_hours"])) if a.get("active_hours") else "Any time",
        "Owner": a.get("owner", ""),
        "Tags": ", ".join(a.get("tags", [])),
        "Changed": c.local(a.get("updated_at"), "%Y-%m-%d %H:%M"),
    } for a in shown])
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="multi-row", key="alerts_table",
                         column_config={"On": st.column_config.CheckboxColumn(width="small"),
                                        "Steps": st.column_config.TextColumn(width="large")})
    rows = [shown[i] for i in (event.selection.rows if event and event.selection else [])]
    if not rows:
        st.caption(f"{len(shown)} of {len(alerts)} alert(s). Select one or more to act on them.")
        return

    st.markdown(f"**{len(rows)} selected**")
    bar = st.columns(7)
    one = rows[0] if len(rows) == 1 else None
    if bar[0].button("Edit", icon=":material/edit:", disabled=one is None, width="stretch"):
        go("builder", builder_request={"id": one["id"]})
    if bar[1].button("Duplicate", icon=":material/content_copy:", disabled=one is None, width="stretch",
                     help="Open a copy in the Builder; nothing is saved until you save it"):
        copy = dict(one, id=None, enabled=False, name=one["name"] + " (copy)")
        go("builder", builder_request={"id": None, "draft": copy})
    if bar[2].button("Switch on", icon=":material/toggle_on:", width="stretch"):
        for a in rows:
            c.call(lambda a=a: c.huginn().post(f"/alerts/{a['id']}/enable"))
        st.rerun()
    if bar[3].button("Switch off", icon=":material/toggle_off:", width="stretch"):
        for a in rows:
            c.call(lambda a=a: c.huginn().post(f"/alerts/{a['id']}/disable"))
        st.rerun()
    bundle = c.call(lambda: c.huginn().get("/export", alert_ids=",".join(str(a["id"]) for a in rows)),
                    quiet=True)
    bar[4].download_button("Export", data=json.dumps(bundle or {}, indent=2),
                           file_name="heimdall-alerts.json", mime="application/json",
                           icon=":material/download:", width="stretch",
                           help="Alerts with their schedules, connections and scripts. Imported alerts arrive switched off.")
    with bar[5].popover("Group", icon=":material/folder:", width="stretch",
                        help="Move the selected alerts to a group"):
        _move(rows, groups)
    with bar[6].popover("Delete", icon=":material/delete:", width="stretch"):
        st.markdown(f"Delete **{', '.join(a['name'] for a in rows)}**? Their schedules go with them.")
        keep = st.checkbox("Keep their history", value=True,
                           help="Past runs stay readable in History until cleaned up")
        if st.button("Delete for good", type="primary", icon=":material/delete_forever:"):
            for a in rows:
                c.call(lambda a=a: c.huginn().delete(f"/alerts/{a['id']}", keep_history=keep))
            st.rerun()


UNGROUPED = "Ungrouped"
NEW_GROUP = "➕ New group…"


def _move(rows: list[dict], groups: list[str]) -> None:
    st.markdown(f"Move **{', '.join(a['name'] for a in rows)}** to")
    pick = st.selectbox("Group", ["(no group)", *groups, NEW_GROUP], key="move_group")
    target = "" if pick == "(no group)" else pick
    if pick == NEW_GROUP:
        target = " ".join(st.text_input("New group name", key="move_group_new").split())
    if st.button("Move", type="primary", icon=":material/drive_file_move:",
                 disabled=pick == NEW_GROUP and not target):
        for a in rows:
            body = {k: v for k, v in a.items() if k not in ("created_at", "updated_at")} | {"group": target}
            c.call(lambda a=a, body=body: c.huginn().put(f"/alerts/{a['id']}", body))
        st.rerun()


def _manage_groups(alerts: list[dict], groups: list[str]) -> None:
    with st.expander(f"Manage groups ({len(groups)})", icon=":material/folder_managed:"):
        for g in groups:
            members = [a for a in alerts if a.get("group") == g]
            on = sum(1 for a in members if a["enabled"])
            r = st.columns([3, 2, 1, 1, 1], vertical_alignment="center")
            r[0].markdown(f":material/folder: **{g}** · {len(members)} alert(s), {on} on")
            new = r[1].text_input("New name", key=f"grp_name_{g}", placeholder="New name",
                                  label_visibility="collapsed",
                                  help="Renaming onto an existing group merges the two")
            if r[2].button("Rename", key=f"grp_ren_{g}", width="stretch", disabled=not new.strip()):
                c.call(lambda g=g, new=new: c.huginn().post("/groups/rename", {"old": g, "new": new}))
                st.rerun()
            if r[3].button("Dissolve", key=f"grp_dis_{g}", width="stretch",
                           help="Its alerts stay, without a group"):
                c.call(lambda g=g: c.huginn().post("/groups/rename", {"old": g, "new": ""}))
                st.rerun()
            all_on = on == len(members)
            if r[4].button("All off" if all_on else "All on", key=f"grp_sw_{g}", width="stretch",
                           icon=":material/toggle_off:" if all_on else ":material/toggle_on:",
                           help="Switch every alert in this group off" if all_on
                           else "Switch every alert in this group on"):
                c.call(lambda g=g, all_on=all_on: c.huginn().post("/groups/switch",
                                                                  {"group": g, "enabled": not all_on}))
                st.rerun()
