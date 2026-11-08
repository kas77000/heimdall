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
               "addresses as Huginn sees them. A KDB host of `demo` or a Bloomberg URL of `demo` "
               "is a built-in fake.")
    if conns:
        df = pd.DataFrame([{"Name": x["name"], "Kind": "KDB" if x["kind"] == "kdb" else "Bloomberg",
                            "Address": f"{x['host']}:{x['port']}" if x["kind"] == "kdb" else x["url"],
                            "Token from": x.get("token_env") or "", "Description": x.get("description", "")}
                           for x in conns])
        event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                             selection_mode="single-row", key="conn_table")
        rows = event.selection.rows if event and event.selection else []
        current = conns[rows[0]] if rows else None
    else:
        current = None
        st.info("No connections yet.", icon=":material/link:")

    if current:
        bar = st.columns([1, 1, 4])
        if bar[0].button("Test", icon=":material/network_check:", width="stretch"):
            with st.spinner(f"Reaching {current['name']}…"):
                r = c.call(lambda: c.huginn().post(f"/connections/{current['name']}/test"))
            if r:
                (st.success if r["ok"] else st.error)(f"{current['name']}: {r['detail']}")
        if bar[1].button("Delete", icon=":material/delete:", width="stretch"):
            if c.call(lambda: c.huginn().delete(f"/connections/{current['name']}")):
                st.rerun()

    st.markdown("#### " + (f"Edit {current['name']}" if current else "Add a connection"))
    key = current["name"] if current else "_new"
    x = current or {"name": "", "kind": "kdb", "host": "", "port": 5000, "url": "", "token_env": "", "description": ""}
    with st.form(f"conn_{key}", border=True):
        a, b = st.columns([2, 1])
        name = a.text_input("Name", value=x["name"])
        kind = b.segmented_control("Kind", ["kdb", "bloomberg"], default=x["kind"],
                                   format_func={"kdb": "KDB", "bloomberg": "Bloomberg (BbgRelay)"}.get)
        a, b = st.columns([2, 1])
        host = a.text_input("KDB host", value=x["host"], placeholder="kdb-prod-01")
        port = b.number_input("KDB port", min_value=0, max_value=65535, value=int(x["port"] or 0))
        a, b = st.columns([2, 1])
        url = a.text_input("BbgRelay URL", value=x["url"], placeholder="ws://bbgbox:8484")
        token_env = b.text_input("Token environment variable", value=x["token_env"],
                                 placeholder="BBGRELAY_TOKEN", help="Read on the Huginn machine; the token itself is never stored")
        desc = st.text_input("Description", value=x.get("description", ""))
        if st.form_submit_button("Save connection", type="primary", icon=":material/save:"):
            body = {"name": name, "kind": kind or "kdb", "host": host, "port": int(port), "url": url,
                    "token_env": token_env, "description": desc}
            if c.call(lambda: c.huginn().put(f"/connections/{current['name'] if current else name}", body)):
                c.kdb_tables.clear(); c.kdb_columns.clear()
                st.toast(f"Saved {name}", icon=":material/check:")
                st.rerun()


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
    st.caption("How Huginn sends email. The password is read from an environment variable on the Huginn machine.")
    with st.form("smtp", border=True):
        a, b = st.columns([3, 1])
        host = a.text_input("SMTP server", value=s.get("host", ""), placeholder="smtp.company.com")
        port = b.number_input("Port", min_value=1, max_value=65535, value=int(s.get("port") or 25))
        sender = st.text_input("From", value=s.get("sender", ""), placeholder="heimdall@company.com")
        a, b = st.columns(2)
        username = a.text_input("User (if the server needs one)", value=s.get("username", ""))
        pw_env = b.text_input("Password environment variable", value=s.get("password_env", ""),
                              placeholder="HEIMDALL_SMTP_PASSWORD")
        tls = st.checkbox("Use STARTTLS", value=bool(s.get("starttls")))
        if st.form_submit_button("Save", type="primary", icon=":material/save:"):
            c.call(lambda: c.huginn().put("/settings", {"smtp": {"host": host, "port": int(port), "sender": sender,
                                                                 "username": username, "password_env": pw_env,
                                                                 "starttls": tls}}))
            st.toast("Email settings saved", icon=":material/check:")


# -- import / export --------------------------------------------------------------

def _bundles() -> None:
    a, b = st.columns(2)
    with a:
        st.markdown("#### Export")
        st.caption("Every alert with its schedules, connections and scripts.")
        bundle = c.call(lambda: c.huginn().get("/export"), quiet=True)
        st.download_button("Download everything", data=json.dumps(bundle or {}, indent=2),
                           file_name="heimdall-bundle.json", mime="application/json",
                           icon=":material/download:", disabled=bundle is None)
    with b:
        st.markdown("#### Import")
        st.caption("Imported alerts arrive switched off, so someone looks before they run.")
        up = st.file_uploader("Bundle", type=["json"], label_visibility="collapsed")
        replace = st.checkbox("Replace what has the same name", value=False)
        if up is not None and st.button("Import", type="primary", icon=":material/upload:"):
            try:
                data = json.loads(up.getvalue().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                st.error(f"Not a Heimdall bundle: {exc}")
                return
            rep = c.call(lambda: c.huginn().post("/import", data, replace=replace))
            if rep:
                st.success(f"Imported {len(rep['alerts'])} alert(s), {len(rep['connections'])} connection(s), "
                           f"{len(rep['scripts'])} script(s), {rep['schedules']} schedule(s).")
                if rep["skipped"]:
                    st.caption("Skipped (already here): " + ", ".join(rep["skipped"]))
