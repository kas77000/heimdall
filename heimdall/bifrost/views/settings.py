"""Settings: connections, the script registry, email, Gjallarhorn, and moving alerts between installs."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from heimdall import gjallarhorn
from heimdall.bifrost import common as c

OUTPUTS = {"json": "JSON (records, an object or a value)", "csv": "CSV with a header line",
           "value": "One value", "exitcode": "Only the exit code"}


def render() -> None:
    c.page_header("Settings")
    view = st.segmented_control("Section", ["Connections", "Scripts", "Email", "Gjallarhorn", "Import / export"],
                                default="Connections", key="set_view", label_visibility="collapsed")
    {"Connections": _connections, "Scripts": _scripts, "Email": _email, "Gjallarhorn": _gjallarhorn,
     "Import / export": _bundles}.get(view or "Connections", _connections)()


# -- connections ----------------------------------------------------------------

def _connections() -> None:
    conns = c.call(lambda: c.huginn().get("/connections"))
    if conns is None:
        return
    st.caption("Where steps get their data. Huginn connects from its own machine, so these are "
               "addresses as Huginn sees them. A host of `demo` is a built-in fake.")
    if conns:
        st.dataframe(pd.DataFrame([{"Name": x["name"], "Kind": KINDS[x["kind"]],
                                    "Address": _address(x), "Application": x.get("app_name") or "",
                                    "Description": x.get("description", "")} for x in conns]),
                     hide_index=True, width="stretch")
    else:
        st.info("No connections yet. Add a KDB or a Bloomberg B-PIPE connection below.",
                icon=":material/link:")

    by_name = {x["name"]: x for x in conns}
    if "conn_pick_next" in st.session_state:          # set by Save / Delete on the last run
        st.session_state["conn_pick"] = st.session_state.pop("conn_pick_next")
    if st.session_state.get("conn_pick") not in (NEW, *by_name):
        st.session_state["conn_pick"] = NEW
    pick = st.selectbox("Connection", [NEW, *by_name], key="conn_pick",
                        format_func=lambda n: n if n == NEW else f"Edit {n} ({KINDS[by_name[n]['kind']]})")
    current = by_name.get(pick)

    if current:
        kind = current["kind"]
    else:
        kind = st.segmented_control("Kind", list(KINDS), default="kdb", key="conn_new_kind",
                                    format_func=KINDS.get) or "kdb"
    x = current or {"name": "", "kind": kind, "host": "", "port": DEFAULT_PORT[kind],
                    "app_name": "", "description": ""}
    key = f"conn_{pick}_{kind}"           # fresh fields for each connection and kind

    with st.form(key, border=True):
        st.markdown(f"**{'Edit ' + current['name'] if current else 'New ' + KINDS[kind] + ' connection'}**")
        name = st.text_input("Name", value=x["name"], placeholder="kdb_prod" if kind == "kdb" else "bpipe")
        a, b = st.columns([2, 1])
        if kind == "kdb":
            host = a.text_input("KDB host", value=x["host"], placeholder="kdb-prod-01")
            port = b.number_input("KDB port", min_value=0, max_value=65535, value=int(x["port"] or 0))
            app_name = ""
        else:
            host = a.text_input("B-PIPE host", value=x["host"], placeholder="bpipe-01.myfirm.com")
            port = b.number_input("B-PIPE port", min_value=0, max_value=65535,
                                  value=int(x["port"] or DEFAULT_PORT["bloomberg"]))
            app_name = st.text_input("Application name", value=x.get("app_name", ""),
                                     placeholder="MYFIRM:HEIMDALL",
                                     help="The application name B-PIPE authorises Heimdall as.")
        desc = st.text_input("Description", value=x.get("description", ""))
        bar = st.columns([1, 1, 3])
        test = bar[0].form_submit_button("Test", icon=":material/network_check:", width="stretch",
                                         help="Try these values from the Huginn machine, without saving")
        save = bar[1].form_submit_button("Save", type="primary", icon=":material/save:", width="stretch")

    body = {"name": name.strip(), "kind": kind, "host": host.strip(), "port": int(port),
            "app_name": app_name.strip(), "description": desc}
    if test:
        if not body["name"]:
            st.error("Give the connection a name first.")
        else:
            with st.spinner(f"Reaching {body['host'] or 'nowhere'}:{body['port']}…"):
                r = c.call(lambda: c.huginn().post("/connections/test", body))
            if r:
                (st.success if r["ok"] else st.error)(r["detail"], icon=":material/network_check:")
    if save:
        if c.call(lambda: c.huginn().put(f"/connections/{current['name'] if current else body['name']}", body)):
            c.kdb_tables.clear(); c.kdb_columns.clear()
            st.session_state["conn_pick_next"] = body["name"]
            st.toast(f"Saved {body['name']}", icon=":material/check:")
            st.rerun()

    if current and st.button(f"Delete {current['name']}", icon=":material/delete:"):
        if c.call(lambda: c.huginn().delete(f"/connections/{current['name']}")):
            st.session_state["conn_pick_next"] = NEW
            st.rerun()


NEW = "New connection"
KINDS = {"kdb": "KDB", "bloomberg": "Bloomberg B-PIPE"}
DEFAULT_PORT = {"kdb": 5000, "bloomberg": 8194}


def _address(x: dict) -> str:
    return "demo (built-in fake)" if x["host"].strip().lower() == "demo" else f"{x['host']}:{x['port']}"


# -- scripts --------------------------------------------------------------------

def _scripts() -> None:
    scripts = c.call(lambda: c.huginn().get("/scripts"))
    if scripts is None:
        return
    st.caption("Alerts can only run scripts registered here. Each argument stays one argument "
               "(there is no shell); `{name}` in an argument is filled from the step.")
    current = None
    if scripts:
        df = pd.DataFrame([{"Name": s["name"], "Command": " ".join(s["argv"]), "Prints": s["output"],
                            "Timeout": f"{s['timeout_secs']}s", "Description": s.get("description", "")}
                           for s in scripts])
        event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                             selection_mode="single-row", key="script_table",
                             column_config={"Command": st.column_config.TextColumn(width="large")})
        rows = event.selection.rows if event and event.selection else []
        current = scripts[rows[0]] if rows else None
    if current:
        bar = st.columns([1, 1, 4])
        if bar[0].button("Try it", icon=":material/play_arrow:", width="stretch",
                         help="Run it with its default parameters, on the Huginn machine"):
            trial = {"name": "try " + current["name"], "interval_secs": 0,
                     "steps": [{"kind": "script", "name": "trial", "script": current["name"], "args": {}}]}
            with st.spinner("Running…"):
                r = c.call(lambda: c.huginn().post("/alerts/preview", trial))
            if r:
                s = r["steps"][0]
                st.code(s["request"], language="bash", wrap_lines=True)
                if s["error"]:
                    st.error(s["error"])
                else:
                    st.dataframe(c.table(s["table"]), hide_index=True, width="stretch")
        if bar[1].button("Remove", icon=":material/delete:", width="stretch"):
            if c.call(lambda: c.huginn().delete(f"/scripts/{current['name']}")):
                st.rerun()

    st.markdown("#### " + (f"Edit {current['name']}" if current else "Register a script"))
    x = current or {"name": "", "argv": [], "params": [], "workdir": "", "timeout_secs": 60,
                    "output": "json", "fail_on_nonzero": True, "description": ""}
    with st.form(f"script_{x['name'] or '_new'}", border=True):
        a, b = st.columns([1, 2])
        name = a.text_input("Name", value=x["name"], help="Letters, digits and _")
        desc = b.text_input("Description", value=x.get("description", ""))
        argv = st.text_area("Command, one argument per line", value="\n".join(x["argv"]), height=110,
                            placeholder="python\nC:\\scripts\\check_fix_sessions.py\n--venue\n{venue}")
        st.markdown("**Parameters**")
        params = st.data_editor(
            pd.DataFrame(x["params"] or [], columns=["name", "required", "default", "description"]),
            num_rows="dynamic", hide_index=True, width="stretch", key=f"params_{x['name'] or '_new'}",
            column_config={"name": st.column_config.TextColumn("Name", required=True),
                           "required": st.column_config.CheckboxColumn("Required", default=True),
                           "default": st.column_config.TextColumn("Default"),
                           "description": st.column_config.TextColumn("What it is", width="large")})
        a, b, h = st.columns(3)
        output = a.selectbox("It prints", list(OUTPUTS), index=list(OUTPUTS).index(x["output"]), format_func=OUTPUTS.get)
        timeout = b.number_input("Timeout (seconds)", min_value=1, max_value=3600, value=int(x["timeout_secs"]))
        workdir = h.text_input("Working folder", value=x.get("workdir", ""), placeholder="optional")
        nonzero = st.checkbox("A non-zero exit code is a failure", value=x.get("fail_on_nonzero", True))
        if st.form_submit_button("Save script", type="primary", icon=":material/save:"):
            plist = [{"name": str(r["name"]).strip(), "required": bool(r.get("required", True)),
                      "default": "" if pd.isna(r.get("default")) else str(r.get("default") or ""),
                      "description": "" if pd.isna(r.get("description")) else str(r.get("description") or "")}
                     for _, r in params.iterrows() if str(r.get("name") or "").strip() and not pd.isna(r.get("name"))]
            body = {"name": name, "argv": [l for l in argv.splitlines() if l.strip()], "params": plist,
                    "workdir": workdir, "timeout_secs": int(timeout), "output": output,
                    "fail_on_nonzero": nonzero, "description": desc}
            if c.call(lambda: c.huginn().put(f"/scripts/{current['name'] if current else name}", body)):
                st.toast(f"Saved {name}", icon=":material/check:")
                st.rerun()


# -- email ----------------------------------------------------------------------

def _email() -> None:
    settings = c.call(lambda: c.huginn().get("/settings"))
    if settings is None:
        return
    s = settings.get("smtp") or {}
    st.caption("How Huginn sends alert emails: through this mail server, from this address.")
    with st.form("smtp", border=True):
        a, b = st.columns([3, 1])
        host = a.text_input("SMTP server", value=s.get("host", ""), placeholder="smtp.company.com")
        port = b.number_input("SMTP port", min_value=1, max_value=65535, value=int(s.get("port") or 25))
        sender = st.text_input("Sender address", value=s.get("sender", ""), placeholder="heimdall@company.com",
                               help="The address alert emails come from")
        if st.form_submit_button("Save", type="primary", icon=":material/save:"):
            if c.call(lambda: c.huginn().put("/settings", {"smtp": s | {"host": host.strip(), "port": int(port),
                                                                        "sender": sender.strip()}})):
                st.toast("Email settings saved", icon=":material/check:")
    _prefixes(s)


def _prefixes(s: dict) -> None:
    """Subject prefixes: "[Heimdall] Price outside limits: TRIGGERED", or one of your own."""
    from heimdall.core.models import clean_prefix
    from heimdall.core.notify import prefixes
    current = prefixes({"smtp": s})
    with st.form("prefixes", border=True):
        st.markdown("**Subject prefixes**")
        st.caption("What starts each alert email's subject, in brackets: `[APAC QUANT TEAM] Price outside "
                   "limits: TRIGGERED`. Each alert picks one (Edit → Notify), or takes the default.")
        text = st.text_area("Prefixes, one per line", value="\n".join(current), height=120,
                            placeholder="Heimdall\nAPAC QUANT TEAM\nCOVERAGE")
        typed = [p for p in dict.fromkeys(clean_prefix(x) for x in text.splitlines()) if p]
        default = st.selectbox("Default", typed or ["Heimdall"],
                               index=(typed.index(current[0]) if current[0] in typed else 0),
                               help="For alerts that do not pick one. Save the list first to choose a new one.")
        if st.form_submit_button("Save prefixes", icon=":material/save:"):
            chosen = default if default in typed else (typed[0] if typed else "Heimdall")
            if c.call(lambda: c.huginn().put("/settings", {"smtp": s | {"prefixes": typed or ["Heimdall"],
                                                                        "prefix": chosen}})):
                st.toast("Prefixes saved", icon=":material/check:")
                st.rerun()


# -- gjallarhorn ------------------------------------------------------------------

def _gjallarhorn() -> None:
    settings = c.call(lambda: c.huginn().get("/settings"))
    if settings is None:
        return
    g = settings.get("gjallarhorn") or {}
    st.caption("Pop-ups on traders' screens. Huginn writes each alert to a folder on the shared drive; "
               "the Gjallarhorn app on each trader's PC (Windows PowerShell, nothing to install) shows it and "
               "writes acknowledgements back. Huginn's account needs to read and write this folder.")
    with st.form("gjallarhorn", border=True):
        a, b = st.columns([3, 1])
        root = a.text_input("Shared folder", value=g.get("root", ""),
                            placeholder=r"\\fileserver\share\Heimdall\Gjallarhorn")
        ttl = b.number_input("Alerts stay for (hours)", min_value=1, max_value=gjallarhorn.MAX_TTL_HOURS,
                             value=gjallarhorn.Settings.from_store(settings).ttl_hours,
                             help="A pop-up leaves every trader's list this long after it fired, acknowledged "
                                  "or not, and Huginn then removes it from the folder. Its acknowledgements "
                                  "are removed after it, never before. Up to a week.")
        if st.form_submit_button("Save", type="primary", icon=":material/save:"):
            keep = {k: v for k, v in g.items() if k != "keep_days"}
            if c.call(lambda: c.huginn().put("/settings", {"gjallarhorn": keep | {"root": root.strip(),
                                                                                 "ttl_hours": int(ttl)}})):
                st.toast("Gjallarhorn settings saved", icon=":material/check:")
                st.rerun()
    status = c.call(lambda: c.huginn().get("/gjallarhorn"))
    if not status or not status.get("configured"):
        st.info("Set the shared folder first.", icon=":material/folder_shared:")
        return
    if not status.get("reachable"):
        st.warning(f"Huginn cannot reach {status['root']}. Check the path and that Huginn's account can write there.",
                   icon=":material/folder_off:")
    else:
        st.caption(f":material/check_circle: Reachable · {status['today']} alert(s) written today · app "
                   + ("published" if status.get("app_published") else "**not published yet**"))
    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("**1. Publish the app to the share**")
        st.caption("Copies Gjallarhorn into the folder's `app` subfolder. Publishing again updates every trader "
                   "at their next logon (or when they press Restart).")
        if st.button("Publish the app", icon=":material/upload:", key="gj_deploy"):
            r = c.call(lambda: c.huginn().post("/gjallarhorn/deploy", {}))
            if r:
                st.toast(f"Published {len(r['copied'])} file(s)", icon=":material/check:")
    with b.container(border=True):
        st.markdown("**2. Send a test pop-up**")
        _test_pop_up()
    st.markdown("**3. On each trader's PC**, once, as that trader (no admin rights): double-click "
                "`install-gjallarhorn.cmd` in the folder's `app` subfolder (it asks for the folder), or run:")
    st.code(status.get("install", ""), language="bat")
    st.caption("Gjallarhorn then starts at every logon, and the trader's Windows user name shows up below within "
               "a few minutes. Next to it: `start-gjallarhorn.cmd`, `stop-gjallarhorn.cmd` (until the next logon) "
               "and `uninstall-gjallarhorn.cmd` (stops it starting at logon).")
    _groups()
    _sounds(g)


def _alerts_using(alerts: list[dict]) -> dict[str, list[str]]:
    """Name in an alert's pop-up audience (lower case) -> the alerts using it."""
    used: dict[str, list[str]] = {}
    for a in alerts:
        for who in ((a.get("channels") or {}).get("gjallarhorn") or {}).get("audience", []):
            used.setdefault(str(who).lower(), []).append(a["name"])
    return used


def _groups() -> None:
    """Groups of traders, by Windows user name: an alert's pop-ups go to the groups it picks."""
    import uuid
    saved = c.call(lambda: c.huginn().get("/gjallarhorn/groups")) or {}
    seen = c.call(lambda: c.huginn().get("/gjallarhorn/users"), quiet=True) or []
    used = _alerts_using(c.call(lambda: c.huginn().get("/alerts"), quiet=True) or [])
    if "gj_groups" not in st.session_state:
        st.session_state.gj_groups = [{"key": uuid.uuid4().hex[:8], "name": k, "members": list(v)}
                                      for k, v in saved.items()]
    draft: list[dict] = st.session_state.gj_groups
    with st.container(border=True):
        st.markdown("**Groups of traders**")
        st.caption("Put traders in groups by Windows user name; an alert's pop-ups go to the groups it picks "
                   "(Edit → Notify → Traders' pop-ups). A change applies to the next pop-ups, not to those "
                   "already on screens.")
        known = {u["user"] for u in seen} | {m for grp in draft for m in grp["members"]}
        options = sorted(known, key=str.lower)
        for grp in list(draft):
            k = grp["key"]
            a, b, x = st.columns([2, 5, 0.6], vertical_alignment="bottom")
            grp["name"] = a.text_input("Group", value=grp["name"], key=f"gj_g_name_{k}", placeholder="Equities")
            grp["members"] = b.multiselect("Traders (Windows user names)", options, default=grp["members"],
                                           accept_new_options=True, key=f"gj_g_mem_{k}",
                                           placeholder="Pick or type a user name")
            if x.button("", icon=":material/delete:", key=f"gj_g_del_{k}", help="Remove this group"):
                draft.remove(grp)
                st.rerun()
            users_of = used.get(grp["name"].strip().lower(), [])
            if users_of:
                st.caption(f"Used by {len(users_of)} alert(s): " + ", ".join(sorted(users_of)))
        a, b, x = st.columns([1, 1, 2])
        if a.button("Add a group", icon=":material/group_add:", key="gj_g_add"):
            draft.append({"key": uuid.uuid4().hex[:8], "name": "", "members": []})
            st.rerun()
        if x.button("Undo changes", icon=":material/undo:", key="gj_g_undo"):
            del st.session_state.gj_groups
            st.rerun()
        if b.button("Save groups", icon=":material/save:", type="primary", key="gj_g_save"):
            names = [grp["name"].strip() for grp in draft]
            dupes = sorted({n for n in names if n and [m.lower() for m in names].count(n.lower()) > 1})
            if any(not n for n in names):
                st.error("Every group needs a name.")
            elif dupes:
                st.error("Two groups have the same name: " + ", ".join(dupes))
            else:
                body = {grp["name"].strip(): grp["members"] for grp in draft}
                if c.call(lambda: c.huginn().put("/gjallarhorn/groups", body)) is not None:
                    gone = [n for n in saved if n.lower() not in {m.lower() for m in body} and used.get(n.lower())]
                    for n in gone:
                        st.warning(f"Group {n} is gone but still picked by: {', '.join(used[n.lower()])}. "
                                   "Their pop-ups now reach nobody through it.", icon=":material/warning:")
                    del st.session_state.gj_groups
                    st.toast("Groups saved", icon=":material/check:")
                    if not gone:
                        st.rerun()
        _traders(seen)


def _traders(seen: list[dict]) -> None:
    st.markdown("Traders running Gjallarhorn")
    if not seen:
        st.caption("None seen yet: Gjallarhorn writes the trader's user name to the folder's `users` subfolder "
                   "when it starts (after the app is published again with this version), then every few minutes.")
        return
    import pandas as pd
    st.dataframe(pd.DataFrame([{
        "Status": "🟢 Running" if u["running"] else "⚪ Not seen lately",
        "User": u["user"], "PC": u["machine"],
        "Groups": ", ".join(u.get("groups") or []) or "—",
        "Last seen": pd.to_datetime(u["seen"]).tz_convert(None).strftime("%Y-%m-%d %H:%M") + " UTC",
    } for u in seen]), hide_index=True, use_container_width=True)


TEST_SEVERITIES = {"info": "Info", "warning": "Warning", "critical": "Critical"}


def _test_pop_up() -> None:
    """A test pop-up: its severity (and so its sound), and everyone or chosen desks / traders."""
    groups = c.call(lambda: c.huginn().get("/gjallarhorn/groups"), quiet=True) or {}
    seen = c.call(lambda: c.huginn().get("/gjallarhorn/users"), quiet=True) or []
    used = {who for a in (c.call(lambda: c.huginn().get("/alerts"), quiet=True) or [])
            for who in ((a.get("channels") or {}).get("gjallarhorn") or {}).get("audience", [])}
    sev = st.segmented_control("Severity", list(TEST_SEVERITIES), format_func=TEST_SEVERITIES.get,
                               default="warning", key="gj_test_sev") or "warning"
    to = st.radio("Send to", ["everyone", "chosen"], horizontal=True, key="gj_test_scope",
                  format_func={"everyone": "Every trader", "chosen": "Some groups or traders"}.get)
    who: list[str] = []
    if to == "chosen":
        who = st.multiselect("Groups or Windows user names", c.audience_options(groups, seen, used),
                             format_func=lambda n: c.audience_label(n, groups), accept_new_options=True,
                             key="gj_test_to", placeholder="Pick a group or a trader",
                             help="Your groups (Groups of traders, below) and the traders running Gjallarhorn")
    info = c.call(lambda: c.huginn().get("/gjallarhorn/sounds"), quiet=True) or {}
    sound = (info.get("defaults") or {}).get(sev, f"horn:{sev}")
    if sound.startswith(("horn:", "file:")):
        data = c.call(lambda: c.huginn().get_bytes("/gjallarhorn/sound", sound=sound), quiet=True)
        if data:
            st.audio(data, format="audio/wav")
    elif sound == "none":
        st.caption("This severity is silent (Sounds, below).")
    ready = to == "everyone" or bool(who)
    target = "every trader" if to == "everyone" else ", ".join(who) or "nobody yet"
    if st.button(f"Send a {TEST_SEVERITIES[sev].lower()} test to {target}", icon=":material/notifications_active:",
                 key="gj_test", disabled=not ready, type="primary"):
        if c.call(lambda: c.huginn().post("/gjallarhorn/test", {"audience": who, "severity": sev})):
            st.toast(f"Test pop-up sent to {target}", icon=":material/check:")


def _sounds(g: dict) -> None:
    """The horn each severity sounds by default, and sounds of your own."""
    from heimdall.bifrost.views.builder import SOUNDS
    info = c.call(lambda: c.huginn().get("/gjallarhorn/sounds")) or {}
    files = info.get("files", [])
    choices = {k: v for k, v in SOUNDS.items() if k} | {f["sound"]: f"Uploaded: {f['name']}" for f in files}
    defaults = info.get("defaults", {})
    with st.container(border=True):
        st.markdown("**Sounds**")
        st.caption("What each severity sounds like on traders' screens, unless an alert picks its own sound. "
                   "Gjallarhorn's horns grow more urgent with the severity.")
        picked = {}
        cols = st.columns(3)
        for col, sev in zip(cols, ("info", "warning", "critical")):
            cur = defaults.get(sev, f"horn:{sev}")
            picked[sev] = col.selectbox(sev.capitalize(), list(choices), format_func=choices.get,
                                        index=list(choices).index(cur) if cur in choices else 0, key=f"gj_snd_{sev}")
            if picked[sev].startswith(("horn:", "file:")):
                data = c.call(lambda s=picked[sev]: c.huginn().get_bytes("/gjallarhorn/sound", sound=s), quiet=True)
                if data:
                    col.audio(data, format="audio/wav")
        if st.button("Save sounds", icon=":material/volume_up:", key="gj_snd_save"):
            if c.call(lambda: c.huginn().put("/settings", {"gjallarhorn": g | {"sounds": picked}})):
                st.toast("Sounds saved", icon=":material/check:")
        st.markdown("Your own sounds")
        up = st.file_uploader("Add a .wav", type=["wav"], key="gj_snd_up",
                              help="WAV only: it plays on every Windows PC without anything installed. Up to 2 MB.")
        if up is not None and st.button("Upload", icon=":material/upload:", key="gj_snd_upload"):
            import base64
            r = c.call(lambda: c.huginn().post("/gjallarhorn/sounds",
                                               {"name": up.name, "data": base64.b64encode(up.getvalue()).decode()}))
            if r:
                st.toast(f"Added {r['sound'][5:]}", icon=":material/check:")
                st.rerun()
        for f in files:
            r = st.columns([3, 4, 1], vertical_alignment="center")
            r[0].markdown(f":material/music_note: {f['name']}")
            data = c.call(lambda f=f: c.huginn().get_bytes("/gjallarhorn/sound", sound=f["sound"]), quiet=True)
            if data:
                r[1].audio(data, format="audio/wav")
            if r[2].button("", icon=":material/delete:", key=f"gj_snd_del_{f['name']}", help="Remove this sound"):
                c.call(lambda f=f: c.huginn().delete(f"/gjallarhorn/sounds/{f['name']}"))
                st.rerun()


# -- import / export --------------------------------------------------------------

PARTS = {"connections": "Connections", "alerts": "Alerts", "schedules": "Schedules", "scripts": "Scripts"}


def _bundles() -> None:
    a, b = st.columns(2)
    with a:
        st.markdown("#### Export")
        what = st.radio("What to export", ["everything", *PARTS], key="export_what", horizontal=True,
                        format_func=lambda p: "Everything" if p == "everything" else PARTS[p])
        st.caption({"everything": "Every connection, script, alert and schedule.",
                    "connections": "Every KDB and B-PIPE connection.",
                    "alerts": "Every alert definition, without schedules, connections or scripts.",
                    "schedules": "Every schedule, each naming the alert it belongs to.",
                    "scripts": "Every registered script."}[what])
        parts = None if what == "everything" else what
        bundle = c.call(lambda: c.huginn().get("/export", parts=parts), quiet=True)
        st.download_button(f"Download {'everything' if what == 'everything' else PARTS[what].lower()}",
                           data=json.dumps(bundle or {}, indent=2),
                           file_name=f"heimdall-{what}.json", mime="application/json",
                           icon=":material/download:", disabled=bundle is None)
    with b:
        st.markdown("#### Import")
        st.caption("Imported alerts arrive switched off, so someone looks before they run. "
                   "Schedules attach to the alert with the same name.")
        chosen = st.multiselect("What to import", list(PARTS), default=list(PARTS), key="import_what",
                                format_func=PARTS.get)
        replace = st.checkbox("Replace what has the same name", value=False,
                              help="For schedules: the file's schedules replace that alert's schedules")
        up = st.file_uploader("File exported from Heimdall", type=["json"], key="import_file")
        if up is None:
            return
        try:
            data = json.loads(up.getvalue().decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("the file does not hold a Heimdall export")
        except (UnicodeDecodeError, ValueError) as exc:
            st.error(f"Not a Heimdall export: {exc}")
            return
        holds = {p: len(data.get(p) or []) for p in PARTS}
        st.caption("This file holds " + ", ".join(f"{n} {PARTS[p].lower()}" for p, n in holds.items()) + ".")
        if not chosen:
            st.warning("Choose at least one thing to import.")
            return
        if not any(holds[p] for p in chosen):
            st.warning("The file has none of what you chose to import.")
            return
        if st.button("Import " + ", ".join(PARTS[p].lower() for p in chosen), type="primary",
                     icon=":material/upload:"):
            rep = c.call(lambda: c.huginn().post("/import", data, replace=replace, parts=",".join(chosen)))
            if rep:
                st.success(f"Imported {len(rep['alerts'])} alert(s), {len(rep['connections'])} connection(s), "
                           f"{len(rep['scripts'])} script(s), {rep['schedules']} schedule(s).")
                if rep["skipped"]:
                    st.caption("Skipped: " + "; ".join(rep["skipped"]))
