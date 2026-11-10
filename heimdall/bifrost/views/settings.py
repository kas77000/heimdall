"""Settings: connections, the script registry, email, and moving alerts between installs."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from heimdall.bifrost import common as c

OUTPUTS = {"json": "JSON (records, an object or a value)", "csv": "CSV with a header line",
           "value": "One value", "exitcode": "Only the exit code"}


def render() -> None:
    c.page_header("Settings")
    view = st.segmented_control("Section", ["Connections", "Scripts", "Email", "Import / export"],
                                default="Connections", key="set_view", label_visibility="collapsed")
    {"Connections": _connections, "Scripts": _scripts, "Email": _email,
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
            if c.call(lambda: c.huginn().put("/settings", {"smtp": {"host": host.strip(), "port": int(port),
                                                                    "sender": sender.strip()}})):
                st.toast("Email settings saved", icon=":material/check:")


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
