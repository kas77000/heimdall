"""Builder: an alert as a chain of steps, a condition, and who to tell.

The draft lives in session state as the alert's JSON. Every widget key carries
the draft's revision and the step's uid, so loading another alert, or moving a
step, never lets one widget's state leak into another's.
"""
from __future__ import annotations

import uuid
from datetime import time
from typing import Any

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from heimdall.bifrost import common as c
from heimdall.core import cron, hours
from heimdall.core.conditions import describe
from heimdall.core.kdb import build_q
from heimdall.core.models import ALL_CLEAR_INTERVALS, ActiveHours, Alert, Condition, KdbStep
from heimdall.core.notify import ALL_CLEAR_FIELDS

DRAFT, REV, LOADED, PREVIEW = "builder_draft", "builder_rev", "builder_loaded", "builder_preview"

BBG_FIELDS = ["PX_LAST", "LAST_PRICE", "BID", "ASK", "VOLUME", "LIMIT_UP", "LIMIT_DOWN",
              "PX_CLOSE_1D", "PX_OPEN", "NAME", "CRNCY", "EQY_SH_OUT", "CUR_MKT_CAP"]
OPS = ["=", "<>", "<", "<=", ">", ">="]
FILTER_OPS = OPS + ["in", "like"]
VALUE_TYPES = ["symbol", "number", "string", "date", "time", "expression"]
INTERVALS = {"15s": 15, "30s": 30, "1 min": 60, "5 min": 300, "15 min": 900, "1 hour": 3600,
             "Schedule only": 0}
CONDITIONS = {"has_rows": "Returns any rows", "no_rows": "Returns no rows",
              "row_count": "Number of rows is…", "any_row": "Any row where…",
              "all_rows": "Every row where…", "aggregate": "A total (max, sum…) is…"}
REARM = {"transition": "When it starts firing", "on_change": "When the result changes",
         "cooldown": "At most every…", "every_run": "Every run while it fires"}
NO_GROUP, NEW_GROUP = "(no group)", "➕ New group…"
ZONES = ["Europe/London", "Europe/Paris", "America/New_York", "Asia/Tokyo", "Asia/Hong_Kong",
         "Asia/Singapore", "Australia/Sydney", "UTC"]
KIND_HELP = {
    "kdb": "Query a KDB server, guided or in raw q.",
    "bloomberg": "Ask Bloomberg B-PIPE for reference (bdp) or live (snap) values.",
    "script": "Run a script registered in Settings and read what it prints.",
    "compute": "Join, derive and filter tables from earlier steps. No query is sent.",
}


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _new_step(kind: str, existing: list[dict]) -> dict:
    names = {s["name"] for s in existing}
    n = 1
    while f"{kind}{n}" in names:
        n += 1
    step: dict[str, Any] = {"kind": kind, "name": f"{kind}{n}", "_uid": _uid(), "note": ""}
    prev = existing[-1]["name"] if existing else ""
    if kind == "kdb":
        step |= {"connection": "", "mode": "guided", "table": "", "filters": [], "columns": [], "raw_q": ""}
    elif kind == "bloomberg":
        step |= {"connection": "", "operation": "bdp", "securities": "", "fields": [], "overrides": {}}
    elif kind == "script":
        step |= {"script": "", "args": {}, "stdin_step": ""}
    else:
        step |= {"source": prev, "join_step": "", "join_how": "inner", "left_on": [], "right_on": [],
                 "derive": [], "where": "", "keep": []}
    return step


def _blank() -> dict:
    return Alert(name="New alert", enabled=False, interval_secs=60).model_dump() | {"steps": []}


def _load(alert_id, draft=None) -> None:
    if draft is None:
        if alert_id is None:
            draft = _blank()
        else:
            draft = c.call(lambda: c.huginn().get(f"/alerts/{alert_id}"))
            if draft is None:
                draft = _blank()
    draft = {k: v for k, v in draft.items() if k not in ("created_at", "updated_at")}
    for s in draft["steps"]:
        s["_uid"] = s.get("_uid") or _uid()
        for f in s.get("filters", []):
            f["_uid"] = f.get("_uid") or _uid()
        for d in s.get("derive", []):
            d["_uid"] = d.get("_uid") or _uid()
    st.session_state[DRAFT] = draft
    st.session_state[REV] = st.session_state.get(REV, 0) + 1
    st.session_state[LOADED] = alert_id
    st.session_state.pop(PREVIEW, None)


def _clean(draft: dict) -> dict:
    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if not k.startswith("_")}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return strip(draft)


def K(*parts) -> str:
    return "b" + str(st.session_state[REV]) + "_" + "_".join(str(p) for p in parts)


# -- known columns, from the last test run ----------------------------------------

def _columns_of(name: str) -> list[str]:
    prev = st.session_state.get(PREVIEW) or {}
    for s in prev.get("steps", []):
        if s["name"] == name:
            return s.get("columns") or []
    return []


def _ref_hint(earlier: list[dict]) -> None:
    if not earlier:
        st.caption("This is the first step, so there is nothing earlier to refer to.")
        return
    parts = []
    for s in earlier:
        cols = _columns_of(s["name"])
        if cols:
            shown = ", ".join(f"`{{{{{s['name']}.{col}}}}}`" for col in cols[:6])
            parts.append(f"**{s['name']}**: {shown}" + (" …" if len(cols) > 6 else ""))
        else:
            parts.append(f"**{s['name']}**: `{{{{{s['name']}.column}}}}`")
    st.caption("Refer to earlier results with " + " · ".join(parts)
               + (". Run a test to list their columns." if not st.session_state.get(PREVIEW) else ""))


# -- page ---------------------------------------------------------------------

def render() -> None:
    req = st.session_state.pop("builder_request", None)
    if req is None and "alert" in st.query_params:
        try:
            req = {"id": int(st.query_params["alert"])}
        except ValueError:
            pass
        del st.query_params["alert"]
    if req is not None:
        _load(req.get("id"), req.get("draft"))
    elif DRAFT not in st.session_state or REV not in st.session_state:
        _load(None)

    conns = c.call(lambda: c.huginn().get("/connections"))
    if conns is None:
        return
    scripts = c.call(lambda: c.huginn().get("/scripts")) or []
    draft = st.session_state[DRAFT]

    head = st.columns([4, 1, 1], vertical_alignment="bottom")
    with head[0]:
        c.page_header(("Edit · " + draft["name"]) if draft.get("id") else "New alert",
                      "Steps run in order; each can use what the earlier ones returned.")
    if head[1].button("New", icon=":material/add:", width="stretch",
                      help="Start a fresh alert (unsaved changes here are lost)"):
        _load(None)
        st.rerun()
    test_clicked = head[2].button("Test run", icon=":material/science:", width="stretch",
                                  help="Run every step now. Nothing is recorded or sent.")

    tabs = st.segmented_control("Section", ["Steps", "Trigger", "Notify", "Active hours", "Details"],
                                default="Steps", key=K("section"), label_visibility="collapsed")
    st.write("")
    if tabs == "Details":
        _details(draft)
    elif tabs == "Active hours":
        _hours(draft)
    elif tabs == "Trigger":
        _trigger(draft)
    elif tabs == "Notify":
        _notify(draft)
    else:
        _steps(draft, conns, scripts)

    if test_clicked:
        _run_preview(draft)
    _preview_panel(draft)
    st.divider()
    _save_bar(draft)


# -- details --------------------------------------------------------------------

def _details(d: dict) -> None:
    a, b = st.columns([2, 1])
    d["name"] = a.text_input("Name", key=c.seed(K("name"), d["name"]))
    d["owner"] = b.text_input("Owner", key=c.seed(K("owner"), d.get("owner", "")),
                              placeholder="Who answers when it fires")
    d["description"] = st.text_area("What it means when this fires", height=80,
                                    key=c.seed(K("desc"), d.get("description", "")),
                                    help="Goes at the top of every notification")
    _group(d)
    tags = st.multiselect("Tags", options=sorted(set(d.get("tags", []))), accept_new_options=True,
                          key=c.seed(K("tags"), d.get("tags", [])), placeholder="Type to add a tag")
    d["tags"] = [t.strip() for t in tags if t.strip()]
    a, b = st.columns([2, 1])
    labels = list(INTERVALS)
    current = next((k for k, v in INTERVALS.items() if v == d["interval_secs"]), "Custom")
    pick = a.segmented_control("How often Huginn checks it", labels + ["Custom"],
                               key=c.seed(K("ivl"), current))
    if pick == "Custom":
        d["interval_secs"] = int(a.number_input("Seconds between checks", min_value=5, step=5,
                                                key=c.seed(K("ivl_n"), max(5, d["interval_secs"] or 60))))
    elif pick:
        d["interval_secs"] = INTERVALS[pick]
    if d["interval_secs"] == 0:
        a.caption("Runs only when a schedule or a person starts it.")
    d["enabled"] = b.toggle("Enabled", key=c.seed(K("enabled"), d.get("enabled", False)),
                            help="Off: kept, but Huginn does not run it on its interval")


def _group(d: dict) -> None:
    existing = [g["name"] for g in (c.call(lambda: c.huginn().get("/groups"), quiet=True) or [])]
    current = d.get("group", "")
    options = [NO_GROUP, *sorted(set(existing) | ({current} if current else set()), key=str.lower), NEW_GROUP]
    a, b = st.columns([2, 1])
    pick = a.selectbox("Group", options, key=c.seed(K("group"), current or NO_GROUP),
                       help="Alerts in a group are shown together in Overview and Alerts")
    if pick == NEW_GROUP:
        d["group"] = " ".join(b.text_input("New group name", key=K("group_new"),
                                           placeholder="e.g. Equities").split())
    else:
        d["group"] = "" if pick == NO_GROUP else pick


# -- active hours -----------------------------------------------------------------

def _hours(d: dict) -> None:
    h = d.setdefault("active_hours", ActiveHours().model_dump())
    st.caption("When this alert is allowed to run on its interval. Outside these hours Huginn skips it; "
               "runs started by hand or by a schedule still happen.")
    h["enabled"] = st.toggle("Only run during set hours", key=c.seed(K("h_on"), h.get("enabled", False)))
    if not h["enabled"]:
        st.caption("Runs whenever it is switched on.")
        return
    a, b = st.columns([1, 2])
    zones = ZONES if h.get("tz") in ZONES else [h.get("tz") or "UTC", *ZONES]
    h["tz"] = a.selectbox("Time zone", zones, key=c.seed(K("h_tz"), h.get("tz") or "Europe/London"))
    picked = b.pills("Days", hours.DAY_NAMES, selection_mode="multi",
                     key=c.seed(K("h_days"), [hours.DAY_NAMES[i] for i in h.get("days", [])]),
                     help="None picked = every day")
    h["days"] = [hours.DAY_NAMES.index(x) for x in picked or []]

    windows = h.setdefault("windows", [])
    for w in windows:
        w.setdefault("_uid", _uid())
    for i, w in enumerate(list(windows), start=1):
        r = st.columns([2, 2, 1], vertical_alignment="bottom")
        start = r[0].time_input(f"From #{i}", step=60, key=c.seed(K("h_from", w["_uid"]), _clock(w["start"])))
        end = r[1].time_input(f"To #{i}", step=60, key=c.seed(K("h_to", w["_uid"]), _clock(w["end"])))
        w["start"], w["end"] = start.strftime("%H:%M"), end.strftime("%H:%M")
        if r[2].button("Remove", key=K("h_del", w["_uid"]), icon=":material/delete:", width="stretch"):
            windows.remove(w)
            st.rerun()
        if w["end"] < w["start"]:
            st.caption(f"Window {i} crosses midnight: it runs from {w['start']} into the next day until {w['end']}.")
    if len(windows) < hours.MAX_WINDOWS and st.button("Add window", icon=":material/add:"):
        windows.append({"start": "09:00", "end": "17:00", "_uid": _uid()})
        st.rerun()

    try:
        model = ActiveHours(**_clean(h))
    except ValidationError as exc:
        st.warning(exc.errors()[0]["msg"].removeprefix("Value error, "), icon=":material/warning:")
        return
    problems = hours.problems(model)
    for p in problems:
        st.warning(p.removeprefix("Active hours: "), icon=":material/warning:")
    if not problems:
        st.markdown(f":violet-badge[:material/schedule: Runs {hours.describe(model)}]")
        if d.get("interval_secs", 0) == 0:
            st.caption("This alert only runs when scheduled or started by hand, so these hours change nothing yet.")


def _clock(hhmm: str) -> time:
    h, m = (hhmm or "09:00").split(":")
    return time(int(h), int(m))


# -- steps --------------------------------------------------------------------

def _steps(d: dict, conns: list[dict], scripts: list[dict]) -> None:
    steps = d["steps"]
    if not steps:
        st.info("**Start with a step.** A KDB query, a Bloomberg request, a registered script, "
                "or a computation over earlier steps. Later steps can use what earlier ones returned.",
                icon=":material/conversion_path:")
    for i, s in enumerate(list(steps)):
        _step_card(d, i, s, conns, scripts)

    add = st.columns([3, 1], vertical_alignment="bottom")
    kind = add[0].segmented_control("Add a step", list(c.KIND), format_func=lambda k: c.KIND[k][0],
                                    key=K("addkind"), default="kdb")
    if add[1].button("Add step", icon=":material/add:", width="stretch", disabled=not kind):
        steps.append(_new_step(kind, steps))
        st.rerun()
    if kind:
        st.caption(KIND_HELP[kind])


def _step_card(d, i, s, conns, scripts) -> None:
    steps = d["steps"]
    uid = s["_uid"]
    earlier = steps[:i]
    with st.container(border=True):
        top = st.columns([9, 0.6, 0.6, 0.6], vertical_alignment="center", gap="small")
        label, icon = c.KIND[s["kind"]]
        top[0].markdown(f"**{i + 1}** &nbsp; {icon} **{label}** · `{s['name']}`")
        top = [None] + top
        if top[2].button("", key=K(uid, "up"), icon=":material/arrow_upward:", disabled=i == 0,
                         help="Move up", width="stretch"):
            steps[i - 1], steps[i] = steps[i], steps[i - 1]
            st.rerun()
        if top[3].button("", key=K(uid, "down"), icon=":material/arrow_downward:",
                         disabled=i == len(steps) - 1, help="Move down", width="stretch"):
            steps[i + 1], steps[i] = steps[i], steps[i + 1]
            st.rerun()
        if top[4].button("", key=K(uid, "del"), icon=":material/close:", help="Remove this step",
                         width="stretch"):
            steps.pop(i)
            st.rerun()

        a, b = st.columns([1, 3])
        s["name"] = a.text_input("Step name", key=c.seed(K(uid, "name"), s["name"]),
                                 help="Later steps refer to this step by this name")
        s["note"] = b.text_input("Note", key=c.seed(K(uid, "note"), s.get("note", "")),
                                 placeholder="Optional: what this step is for")
        {"kdb": _kdb, "bloomberg": _bbg, "script": _script, "compute": _compute}[s["kind"]](
            s, uid, earlier, conns, scripts)


def _conn_select(s, uid, conns, kind) -> str:
    names = [x["name"] for x in conns if x["kind"] == kind]
    if not names:
        st.warning(f"No {kind.upper() if kind == 'kdb' else 'Bloomberg'} connection yet. "
                   "Add one in Settings → Connections.", icon=":material/link_off:")
        return s.get("connection", "")
    current = s.get("connection") if s.get("connection") in names else names[0]
    return st.selectbox("Connection", names, key=c.seed(K(uid, "conn"), current))


def _kdb(s, uid, earlier, conns, scripts) -> None:
    a, b = st.columns([2, 1])
    with a:
        s["connection"] = _conn_select(s, uid, conns, "kdb")
    s["mode"] = b.segmented_control("Mode", ["guided", "raw"], format_func=lambda m: {"guided": "Guided", "raw": "Raw q"}[m],
                                    key=c.seed(K(uid, "mode"), s.get("mode", "guided"))) or "guided"
    _ref_hint(earlier)
    if s["mode"] == "raw":
        s["raw_q"] = st.text_area("q", height=110, key=c.seed(K(uid, "raw"), s.get("raw_q", "")),
                                  placeholder="select sym, last from quote where sym in {{orders.sym}}")
        return
    tables, cols = [], []
    if s["connection"]:
        tables = c.call(lambda: c.kdb_tables(s["connection"]), quiet=True) or []
    a, b = st.columns([1, 2])
    if tables:
        cur = s.get("table") if s.get("table") in tables else tables[0]
        s["table"] = a.selectbox("Table", tables, key=c.seed(K(uid, "table"), cur))
    else:
        s["table"] = a.text_input("Table", key=c.seed(K(uid, "table_t"), s.get("table", "")),
                                  help="Could not list tables from this connection; type the name")
    if s["connection"] and s["table"]:
        cols = c.call(lambda: c.kdb_columns(s["connection"], s["table"]), quiet=True) or []
    picked = b.multiselect("Columns", cols or s.get("columns", []), accept_new_options=True,
                           key=c.seed(K(uid, "cols"), [x for x in s.get("columns", [])]),
                           placeholder="All columns")
    s["columns"] = picked
    st.markdown("**Where**")
    for j, f in enumerate(list(s["filters"])):
        fu = f["_uid"]
        r = st.columns([2, 1, 3, 1.4, 0.8, 0.6], vertical_alignment="bottom")
        colopts = cols or ([f["column"]] if f.get("column") else [])
        if cols:
            cur = f["column"] if f.get("column") in cols else cols[0]
            f["column"] = r[0].selectbox("Column", cols, key=c.seed(K(uid, fu, "c"), cur),
                                         label_visibility="collapsed")
        else:
            f["column"] = r[0].text_input("Column", key=c.seed(K(uid, fu, "c"), f.get("column", "")),
                                          label_visibility="collapsed", placeholder="column")
        f["op"] = r[1].selectbox("Op", FILTER_OPS, key=c.seed(K(uid, fu, "o"), f.get("op", "=")),
                                 label_visibility="collapsed")
        val = f.get("value", "")
        if isinstance(val, list):
            val = ", ".join(map(str, val))
        f["value"] = r[2].text_input("Value", key=c.seed(K(uid, fu, "v"), str(val)),
                                     label_visibility="collapsed",
                                     placeholder="a, b, c" if f["op"] == "in" else "value or {{step.column}}")
        f["value_type"] = r[3].selectbox("Type", VALUE_TYPES, key=c.seed(K(uid, fu, "t"), f.get("value_type", "symbol")),
                                         label_visibility="collapsed")
        f["negated"] = r[4].checkbox("not", key=c.seed(K(uid, fu, "n"), f.get("negated", False)))
        if r[5].button("", key=K(uid, fu, "x"), icon=":material/close:", help="Remove"):
            s["filters"].pop(j)
            st.rerun()
    if st.button("Add condition", key=K(uid, "addf"), icon=":material/add:"):
        s["filters"].append({"_uid": _uid(), "column": cols[0] if cols else "", "op": "=",
                             "value": "", "value_type": "symbol", "negated": False})
        st.rerun()
    try:
        st.code(build_q(KdbStep(**_clean(s))), language="sql")
    except (ValidationError, ValueError):
        pass


def _bbg(s, uid, earlier, conns, scripts) -> None:
    a, b = st.columns([2, 1])
    with a:
        s["connection"] = _conn_select(s, uid, conns, "bloomberg")
    s["operation"] = b.segmented_control(
        "Request", ["bdp", "snap"], key=c.seed(K(uid, "op"), s.get("operation", "bdp")),
        format_func=lambda o: {"bdp": "bdp · reference", "snap": "snap · live"}[o]) or "bdp"
    _ref_hint(earlier)
    s["securities"] = st.text_area(
        "Securities, one per line", height=90, key=c.seed(K(uid, "secs"), s.get("securities", "")),
        placeholder="{{orders.sym}} JP Equity\nNKY Index",
        help="A line with {{step.column}} is repeated for each value, and the result keeps that "
             "column next to `security` so you can join back on it.")
    fields = st.multiselect("Fields", sorted(set(BBG_FIELDS) | set(s.get("fields", []))),
                            accept_new_options=True, key=c.seed(K(uid, "flds"), s.get("fields", [])),
                            placeholder="Pick or type Bloomberg field mnemonics")
    s["fields"] = [f.strip().upper() for f in fields if f.strip()]
    text = "\n".join(f"{k}={v}" for k, v in s.get("overrides", {}).items())
    raw = st.text_input("Overrides", key=c.seed(K(uid, "ovr"), text.replace("\n", "; ")),
                        placeholder="FIELD=VALUE; FIELD=VALUE (optional)")
    s["overrides"] = {p.split("=", 1)[0].strip(): p.split("=", 1)[1].strip()
                      for p in raw.split(";") if "=" in p}


def _script(s, uid, earlier, conns, scripts) -> None:
    names = [x["name"] for x in scripts]
    if not names:
        st.warning("No script is registered yet. An administrator adds them in Settings → Scripts; "
                   "alerts can only run registered scripts.", icon=":material/terminal:")
        return
    cur = s.get("script") if s.get("script") in names else names[0]
    s["script"] = st.selectbox("Script", names, key=c.seed(K(uid, "script"), cur))
    sc = next(x for x in scripts if x["name"] == s["script"])
    if sc.get("description"):
        st.caption(sc["description"] + f" · prints {sc['output']}")
    _ref_hint(earlier)
    args = {}
    for p in sc.get("params", []):
        v = st.text_input(p["name"] + (" *" if p["required"] and not p["default"] else ""),
                          key=c.seed(K(uid, "arg", s["script"], p["name"]), s.get("args", {}).get(p["name"], "")),
                          placeholder=p.get("default") or "", help=p.get("description") or None)
        if v.strip():
            args[p["name"]] = v
    s["args"] = args
    opts = [""] + [e["name"] for e in earlier]
    cur = s.get("stdin_step") if s.get("stdin_step") in opts else ""
    s["stdin_step"] = st.selectbox("Send a table on stdin (JSON records)", opts,
                                   format_func=lambda x: x or "Nothing", key=c.seed(K(uid, "stdin"), cur))
    st.code(" ".join(sc["argv"]), language="bash")


def _compute(s, uid, earlier, conns, scripts) -> None:
    names = [e["name"] for e in earlier]
    if not names:
        st.warning("A compute step works on earlier results; add a step above it first.",
                   icon=":material/function:")
        return
    a, b, h = st.columns([1, 1, 1])
    cur = s.get("source") if s.get("source") in names else names[-1]
    s["source"] = a.selectbox("Start from", names, key=c.seed(K(uid, "src"), cur))
    opts = [""] + names
    cur = s.get("join_step") if s.get("join_step") in opts else ""
    s["join_step"] = b.selectbox("Join with", opts, format_func=lambda x: x or "No join",
                                 key=c.seed(K(uid, "join"), cur))
    if s["join_step"]:
        s["join_how"] = h.selectbox("Keep", ["inner", "left", "outer"],
                                    format_func=lambda x: {"inner": "Rows in both", "left": "All rows of the first",
                                                           "outer": "All rows of either"}[x],
                                    key=c.seed(K(uid, "how"), s.get("join_how", "inner")))
        l, r = st.columns(2)
        s["left_on"] = l.multiselect(f"Match columns of {s['source']}", sorted(set(_columns_of(s["source"]) + s.get("left_on", []))),
                                     accept_new_options=True, key=c.seed(K(uid, "lon"), s.get("left_on", [])))
        s["right_on"] = r.multiselect(f"…with columns of {s['join_step']}", sorted(set(_columns_of(s["join_step"]) + s.get("right_on", []))),
                                      accept_new_options=True, key=c.seed(K(uid, "ron"), s.get("right_on", [])),
                                      help="Same order as the left; leave empty when the names are the same")
    st.markdown("**New columns**")
    for j, dv in enumerate(list(s["derive"])):
        du = dv["_uid"]
        r = st.columns([1, 3, 0.4], vertical_alignment="bottom")
        dv["column"] = r[0].text_input("Name", key=c.seed(K(uid, du, "n"), dv.get("column", "")),
                                       label_visibility="collapsed", placeholder="name")
        dv["expr"] = r[1].text_input("Expression", key=c.seed(K(uid, du, "e"), dv.get("expr", "")),
                                     label_visibility="collapsed", placeholder="abs(last - LIMIT_UP) / LIMIT_UP * 100")
        if r[2].button("", key=K(uid, du, "x"), icon=":material/close:", help="Remove"):
            s["derive"].pop(j)
            st.rerun()
    if st.button("Add column", key=K(uid, "addd"), icon=":material/add:"):
        s["derive"].append({"_uid": _uid(), "column": "", "expr": ""})
        st.rerun()
    s["where"] = st.text_input("Keep rows where", key=c.seed(K(uid, "where"), s.get("where", "")),
                               placeholder="last >= LIMIT_UP or last <= LIMIT_DOWN",
                               help="Column names, numbers, text in quotes, + - * / < > == !=, and, or, not, "
                                    "x if cond else y, and abs, min, max, round, isnull, notnull.")
    known = sorted(set(_columns_of(s["name"]) + s.get("keep", [])))
    s["keep"] = st.multiselect("Columns to keep", known, accept_new_options=True,
                               key=c.seed(K(uid, "keep"), s.get("keep", [])), placeholder="All columns")


# -- trigger ------------------------------------------------------------------

def _trigger(d: dict) -> None:
    cond, steps = d["condition"], d["steps"]
    if not steps:
        st.info("Add a step first: the condition judges a step's result.", icon=":material/rule:")
        return
    names = [s["name"] for s in steps]
    a, b = st.columns([1, 2])
    cur = cond.get("step") if cond.get("step") in names else ""
    cond["step"] = a.selectbox("Judge the result of", [""] + names,
                               format_func=lambda x: x or f"The last step ({names[-1]})",
                               key=c.seed(K("c_step"), cur))
    cond["type"] = b.selectbox("Fire when it", list(CONDITIONS), format_func=CONDITIONS.get,
                               key=c.seed(K("c_type"), cond.get("type", "has_rows")))
    judged = cond["step"] or names[-1]
    cols = _columns_of(judged)
    t = cond["type"]
    if t in ("any_row", "all_rows", "aggregate", "row_count"):
        r = st.columns([1, 2, 1, 2])
        if t == "aggregate":
            cond["agg"] = r[0].selectbox("Total", ["max", "min", "avg", "sum", "count"],
                                         key=c.seed(K("c_agg"), cond.get("agg", "max")))
        if t != "row_count":
            if cols:
                cur = cond.get("column") if cond.get("column") in cols else cols[0]
                cond["column"] = r[1].selectbox("Column", cols, key=c.seed(K("c_col"), cur))
            else:
                cond["column"] = r[1].text_input("Column", key=c.seed(K("c_col_t"), cond.get("column", "")),
                                                 help="Run a test to pick from the real columns")
        cond["op"] = r[2].selectbox("Is", OPS, index=OPS.index(cond.get("op", ">")), key=K("c_op"))
        raw = r[3].text_input("Value", key=c.seed(K("c_val"), str(cond.get("value", 0))))
        try:
            cond["value"] = float(raw) if raw.strip() not in ("",) and _isnum(raw) else raw
        except ValueError:
            cond["value"] = raw
    try:
        st.markdown(f":violet-badge[:material/rule:] {describe(Condition(**cond), names[-1])}.")
    except ValidationError:
        pass

    st.markdown("#### When it keeps firing")
    rearm = d["rearm"]
    a, b = st.columns([2, 1])
    rearm["mode"] = a.radio("Notify", list(REARM), format_func=REARM.get, horizontal=True,
                            key=c.seed(K("r_mode"), rearm.get("mode", "transition")))
    if rearm["mode"] == "cooldown":
        mins = b.number_input("Minutes between notifications", min_value=1,
                              key=c.seed(K("r_cd"), max(1, rearm.get("cooldown_secs", 900) // 60)))
        rearm["cooldown_secs"] = int(mins) * 60
    st.caption({
        "transition": "One notification when the condition becomes true; nothing more until it clears and fires again.",
        "on_change": "A notification whenever the rows it fires on are different from the last time it fired.",
        "cooldown": "Notifies again while it keeps firing, but not more often than this.",
        "every_run": "Every check that fires sends a notification.",
    }[rearm["mode"]])


def _isnum(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


# -- notify -------------------------------------------------------------------

def _lines(text: str) -> list[str]:
    return [p.strip() for p in text.replace(",", "\n").replace(";", "\n").splitlines() if p.strip()]


def _notify(d: dict) -> None:
    ch = d["channels"]
    ch["email_to"] = _lines(st.text_area("Email to", height=90,
                                         key=c.seed(K("n_to"), "\n".join(ch.get("email_to", []))),
                                         placeholder="one address per line"))
    ch["subject"] = st.text_input("Subject", key=c.seed(K("n_subj"), ch.get("subject", "")),
                                  placeholder=f"[Heimdall] {d['name']}")
    a, b, h = st.columns(3)
    ch["notify_on_error"] = a.toggle("Tell me when it keeps failing", key=c.seed(K("n_err"), ch.get("notify_on_error", False)))
    if ch["notify_on_error"]:
        ch["error_after"] = int(b.number_input("After this many failures in a row", min_value=1, max_value=50,
                                               key=c.seed(K("n_errn"), ch.get("error_after", 3))))
    ch["notify_on_recovery"] = h.toggle("Tell me when it clears", key=c.seed(K("n_rec"), ch.get("notify_on_recovery", False)))
    _all_clear(d, ch)
    if not ch["email_to"]:
        st.caption("No one will be told. Firings still show in Overview and History.")
    elif st.button("Send a test notification", icon=":material/send:"):
        res = c.call(lambda: c.huginn().post("/alerts/test-notification", _clean(d)))
        for dl in (res or {}).get("deliveries", []):
            (st.success if dl["ok"] else st.error)(f"{dl['channel']} → {dl['target']}"
                                                   + ("" if dl["ok"] else f": {dl['error']}"))


def _all_clear(d: dict, ch: dict) -> None:
    ac = ch.setdefault("all_clear", {"enabled": False, "mode": "interval", "interval_secs": 3600,
                                     "subject": "", "body": ""})
    ac["enabled"] = st.toggle("Also mail when all clear", key=c.seed(K("ac_on"), ac.get("enabled", False)),
                              help="A mail to the same people saying nothing fired")
    if not ac["enabled"]:
        return
    with st.container(border=True):
        whens = {"With every check": None, **ALL_CLEAR_INTERVALS}
        current = ("With every check" if ac.get("mode") == "every_check" else
                   next((k for k, v in ALL_CLEAR_INTERVALS.items() if v == ac.get("interval_secs")), "1 hour"))
        when = st.selectbox("When", list(whens), key=c.seed(K("ac_when"), current),
                            help="A period with clean checks and none that fired or failed sends one mail")
        if whens[when] is None:
            ac["mode"] = "every_check"
        else:
            ac["mode"], ac["interval_secs"] = "interval", whens[when]
            if d.get("interval_secs") and ac["interval_secs"] <= d["interval_secs"]:
                st.markdown(":orange[The period is not longer than the check interval, "
                            "so this mails after every clean check.]")
        default_subject = (ch.get("subject") or f"[Heimdall] {d['name']}") + ": ALL CLEAR"
        ac["subject"] = st.text_input("Subject", key=c.seed(K("ac_subj"), ac.get("subject", "")),
                                      placeholder=default_subject)
        default_body = ("{alert} did not fire at {until}. Rows returned: {rows}." if ac["mode"] == "every_check"
                        else "{alert} did not fire between {since} and {until}. Clean checks: {checks}.")
        ac["body"] = st.text_area("Body", height=80, key=c.seed(K("ac_body"), ac.get("body", "")),
                                  placeholder=default_body)
        names = " ".join("`{" + f + "}`" for f in ALL_CLEAR_FIELDS)
        st.caption(f"You can use {names}. Times are in the alert's active-hours time zone.")
        if not ch.get("email_to"):
            st.caption(":orange[Add someone under Email to, or no all-clear mail is sent.]")


# -- preview ------------------------------------------------------------------

def _run_preview(d: dict) -> None:
    with st.spinner("Running every step…"):
        res = c.call(lambda: c.huginn().post("/alerts/preview", _clean(d)))
    if res is not None:
        st.session_state[PREVIEW] = res


def _preview_panel(d: dict) -> None:
    res = st.session_state.get(PREVIEW)
    if not res:
        return
    st.markdown("#### Test run")
    verdict = res["status"]
    msg = (res.get("error") or res["message"].split(": ", 1)[-1])
    st.markdown(f"{c.badge(verdict)} {msg} · {res['duration']:.2f}s")
    for s in res["steps"]:
        label, icon = c.KIND[s["kind"]]
        rows = "failed" if s["error"] else f"{s['rows']} row(s)"
        with st.expander(f"{icon} **{s['name']}** · {label} · {rows} · {s['duration']:.2f}s",
                         expanded=bool(s["error"]) or s["name"] == res.get("judged_step")):
            if s["request"]:
                st.code(s["request"], language="sql" if s["kind"] == "kdb" else None, wrap_lines=True)
            if s["error"]:
                st.error(s["error"], icon=":material/error:")
            else:
                df = c.table(s["table"])
                if df.empty:
                    st.caption("No rows.")
                else:
                    st.dataframe(df, hide_index=True, width="stretch", height=min(38 + 35 * len(df), 320))
                    if s["table"]["total"] > len(df):
                        st.caption(f"First {len(df)} of {s['table']['total']} rows.")
    if res.get("matching") and res["matching"]["total"]:
        st.caption(f"{res['matching']['total']} row(s) match the condition.")


# -- save ---------------------------------------------------------------------

def _save_bar(d: dict) -> None:
    body = _clean(d)
    problems: list[str] = []
    try:
        Alert(**body)
    except ValidationError as exc:
        problems = [e["msg"].removeprefix("Value error, ") for e in exc.errors()]
    if not problems:
        res = c.call(lambda: c.huginn().post("/alerts/validate", body), quiet=True)
        problems = (res or {}).get("problems", [])
    a, b = st.columns([4, 1], vertical_alignment="center")
    with a:
        if problems:
            st.markdown(":orange-badge[:material/warning: Not ready to save]")
            for p in problems:
                st.caption("· " + p)
        else:
            on = ":green-badge[on]" if d.get("enabled") else ":gray-badge[off]"
            st.caption(f"Ready. It will be saved switched {on} and checked "
                       f"{c.every(d['interval_secs']).lower()}.")
    if b.button("Save alert", type="primary", icon=":material/save:", width="stretch", disabled=bool(problems)):
        if d.get("id"):
            saved = c.call(lambda: c.huginn().put(f"/alerts/{d['id']}", body))
        else:
            saved = c.call(lambda: c.huginn().post("/alerts", body))
        if saved:
            preview = st.session_state.get(PREVIEW)
            _load(saved["id"], saved)
            if preview:
                st.session_state[PREVIEW] = preview
            st.toast(f"Saved {saved['name']}", icon=":material/check:")
            st.rerun()
    if d.get("id"):
        sched = c.call(lambda: c.huginn().get("/schedules", alert_id=d["id"]), quiet=True) or []
        if sched:
            st.caption("Schedules: " + " · ".join(f"{s['action']} {s['describe']}" for s in sched))
