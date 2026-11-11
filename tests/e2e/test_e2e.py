"""End to end: the real processes, talking over real sockets.

Started here, each on a free port, against a throwaway database:

* Huginn          ``python -m heimdall huginn``   (API + engine, with an API token)
* Sleipnir        ``python -m heimdall sleipnir`` (ticking every second)
* Bifrost         ``python -m heimdall bifrost``  (Streamlit server)
* an SMTP server, in this process, that records what arrives

KDB and Bloomberg are the built-in fakes (host ``demo``): a real kdb+ needs a
licence and a server, and B-PIPE needs Bloomberg's network and an application name.

Run just these with ``pytest -m e2e``; skip them with ``pytest -m "not e2e"``.
They take a little over a minute, because a cron schedule fires on the minute.
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import pytest

from heimdall.proc import popen_group_kwargs, stop_tree

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "e2e-secret-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check, timeout: float, what: str, every: float = 0.5):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = check()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what} (last: {last!r})")


# -- sinks ---------------------------------------------------------------------

class SmtpSink(socketserver.ThreadingTCPServer):
    """Just enough SMTP to accept mail and keep it."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port):
        self.messages: list[dict] = []
        super().__init__(("127.0.0.1", port), _SmtpHandler)


class _SmtpHandler(socketserver.StreamRequestHandler):
    def handle(self):
        msg = {"rcpt": [], "data": ""}
        self.wfile.write(b"220 sink\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").strip()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250 sink\r\n")
            elif up.startswith("MAIL FROM"):
                msg["from"] = cmd
                self.wfile.write(b"250 ok\r\n")
            elif up.startswith("RCPT TO"):
                msg["rcpt"].append(cmd.split(":", 1)[1].strip(" <>"))
                self.wfile.write(b"250 ok\r\n")
            elif up == "DATA":
                self.wfile.write(b"354 go\r\n")
                lines = []
                while True:
                    d = self.rfile.readline()
                    if d in (b".\r\n", b".\n", b""):
                        break
                    lines.append(d.decode("utf-8", "replace"))
                msg["data"] = "".join(lines)
                self.server.messages.append(dict(msg))
                msg = {"rcpt": [], "data": ""}
                self.wfile.write(b"250 queued\r\n")
            elif up == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 ok\r\n")


# -- the stack -------------------------------------------------------------------

class Stack:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.ports = {k: free_port() for k in ("huginn", "bifrost", "smtp")}
        self.url = f"http://127.0.0.1:{self.ports['huginn']}"
        self.procs: dict[str, subprocess.Popen] = {}
        self.config = tmp / "heimdall.toml"
        self.config.write_text(textwrap.dedent(f"""
            [muninn]
            path = "{(tmp / 'e2e.db').as_posix()}"
            [huginn]
            host = "127.0.0.1"
            port = {self.ports['huginn']}
            token_env = "HEIMDALL_E2E_TOKEN"
            workers = 4
            tick_secs = 0.5
            [sleipnir]
            url = "{self.url}"
            tick_secs = 1
            [bifrost]
            url = "http://localhost:{self.ports['bifrost']}"
        """), encoding="utf-8")
        self.env = dict(os.environ, HEIMDALL_CONFIG=str(self.config), HEIMDALL_E2E_TOKEN=TOKEN,
                        PYTHONUNBUFFERED="1")
        self.http = httpx.Client(base_url=self.url, timeout=60,
                                 headers={"X-Heimdall-Token": TOKEN, "X-Heimdall-User": "e2e"})

    def start(self, name: str, args: list[str], cwd: Path = ROOT, python: str = sys.executable) -> None:
        log = open(self.tmp / f"{name}.log", "w", encoding="utf-8")
        self.procs[name] = subprocess.Popen([python, *args], cwd=cwd, env=self.env,
                                            stdout=log, stderr=subprocess.STDOUT,
                                            **popen_group_kwargs())

    def stop(self, name: str) -> None:
        p = self.procs.pop(name, None)
        if p:
            stop_tree(p)

    def log(self, name: str) -> str:
        path = self.tmp / f"{name}.log"
        return path.read_text(encoding="utf-8", errors="replace")[-3000:] if path.exists() else ""

    def start_huginn(self) -> None:
        self.start("huginn", ["-m", "heimdall", "huginn"])
        wait_until(lambda: httpx.get(self.url + "/health", timeout=2).status_code == 200, 40,
                   "Huginn to answer")

    # API helpers that fail loudly with the server's explanation
    def call(self, method: str, path: str, json=None, **params):
        r = self.http.request(method, path, json=json, params=params or None)
        assert r.status_code < 400, f"{method} {path} -> {r.status_code}: {r.text}"
        return r.json()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("heimdall-e2e")
    s = Stack(tmp)
    smtp = SmtpSink(s.ports["smtp"])
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    s.smtp = smtp
    try:
        s.start_huginn()
        s.start("sleipnir", ["-m", "heimdall", "sleipnir"])
        s.start("bifrost", ["-m", "heimdall", "bifrost", "--port", str(s.ports["bifrost"])])
        yield s
    finally:
        for name in list(s.procs):
            s.stop(name)
        smtp.shutdown()
        s.http.close()


# -- the journey ---------------------------------------------------------------

def test_01_token_is_required(stack):
    assert httpx.get(stack.url + "/health").status_code == 200
    assert httpx.get(stack.url + "/alerts").status_code == 401
    assert httpx.get(stack.url + "/alerts", headers={"X-Heimdall-Token": "wrong"}).status_code == 401


def test_02_connections_scripts_and_email(stack):
    stack.call("PUT", "/connections/oms", json={"name": "oms", "kind": "kdb", "host": "demo"})
    stack.call("PUT", "/connections/bbg", json={"name": "bbg", "kind": "bloomberg", "host": "demo"})
    assert stack.call("POST", "/connections/bbg/test")["ok"]
    assert stack.call("POST", "/connections/oms/test")["ok"]

    script = stack.tmp / "tag_orders.py"
    script.write_text("import json, sys\n"
                      "rows = json.load(sys.stdin)\n"
                      "for r in rows:\n"
                      "    r['desk'] = sys.argv[1]\n"
                      "print(json.dumps(rows))\n", encoding="utf-8")
    stack.call("PUT", "/scripts/tag_orders", json={
        "name": "tag_orders", "argv": [sys.executable, str(script), "{desk}"],
        "params": [{"name": "desk", "required": True}], "output": "json", "timeout_secs": 30})
    stack.call("PUT", "/settings", json={"smtp": {"host": "127.0.0.1", "port": stack.ports["smtp"],
                                                  "sender": "heimdall@test"}})


ALERT = {
    "name": "E2E orders priced through Bloomberg",
    "description": "Working orders, their Bloomberg price, and the desk that owns them.",
    "enabled": True,
    "interval_secs": 5,
    "steps": [
        {"kind": "kdb", "name": "orders", "connection": "oms", "table": "orders",
         "filters": [{"column": "state", "op": "=", "value": "working", "value_type": "symbol"}]},
        {"kind": "bloomberg", "name": "px", "connection": "bbg", "operation": "bdp",
         "securities": "{{orders.sym}} US Equity", "fields": ["PX_LAST"]},
        {"kind": "compute", "name": "priced", "source": "orders", "join_step": "px",
         "left_on": ["sym"], "right_on": ["sym"],
         "derive": [{"column": "away_pct", "expr": "abs(limitPrice - PX_LAST) / PX_LAST * 100"}]},
        {"kind": "script", "name": "tagged", "script": "tag_orders", "args": {"desk": "Program"},
         "stdin_step": "priced"},
    ],
    "condition": {"type": "has_rows"},
    "rearm": {"mode": "transition"},
    "channels": {"email_to": ["desk@example.com"], "notify_on_recovery": True},
}


def test_03_preview_runs_every_kind_of_step(stack):
    alert = json.loads(json.dumps(ALERT))
    p = stack.call("POST", "/alerts/preview", json=alert)
    assert p["problems"] == []
    assert [s["error"] for s in p["steps"]] == [None, None, None, None], p["steps"]
    rows = {s["name"]: s["rows"] for s in p["steps"]}
    assert rows["orders"] >= 1 and rows["px"] == rows["orders"]
    tagged = p["steps"][-1]["table"]
    assert {"sym", "PX_LAST", "away_pct", "desk", "exit_code"} <= set(tagged["columns"])
    assert p["status"] == "triggered"
    assert stack.call("GET", "/runs")["total"] == 0          # a preview records nothing


def test_04_engine_runs_it_and_notifies_once(stack):
    alert = json.loads(json.dumps(ALERT))
    created = stack.call("POST", "/alerts", json=alert)
    stack.alert_id = created["id"]

    runs = wait_until(lambda: (r := stack.call("GET", "/runs", alert_id=created["id"]))["total"] >= 2 and r,
                      40, "two interval runs")
    first = runs["runs"][-1]
    assert first["status"] == "triggered" and first["triggered"] and first["notified"] == "trigger"
    assert first["source"] == "interval"
    later = runs["runs"][0]
    assert later["status"] == "triggered" and later["notified"] is None and not later["triggered"]

    detail = stack.call("GET", f"/runs/{first['id']}")
    assert [(d["channel"], d["ok"]) for d in detail["deliveries"]] == [("email", True)]
    assert [s["name"] for s in detail["steps"]] == ["orders", "px", "priced", "tagged"]
    assert detail["snapshot"]["total"] >= 1

    mail = wait_until(lambda: stack.smtp.messages, 10, "an email")
    assert mail[0]["rcpt"] == ["desk@example.com"]
    assert "E2E orders priced through Bloomberg" in mail[0]["data"]
    assert "Subject: [WARNING] [Heimdall] E2E orders priced through Bloomberg: TRIGGERED" in mail[0]["data"]
    assert len(stack.smtp.messages) == 1                       # transition: once


def test_05_status_ack_and_events(stack):
    status = stack.call("GET", "/status")
    row = next(a for a in status["alerts"] if a["id"] == stack.alert_id)
    assert row["last_status"] == "triggered" and row["unacknowledged"] == 1
    assert status["engine"]["running"] and not status["engine"]["paused"]
    assert stack.call("POST", "/runs/acknowledge", json={"alert_id": stack.alert_id})["acknowledged"] == 1
    actions = [e["action"] for e in stack.call("GET", "/events", alert_id=stack.alert_id)]
    assert "created" in actions


def test_06_errors_and_recovery_are_notified(stack):
    """A script reads a file the test controls, so the alert fires, fails and clears on cue."""
    state = stack.tmp / "flag.json"
    script = stack.tmp / "flag.py"
    script.write_text("import sys, pathlib\n"
                      "text = pathlib.Path(sys.argv[1]).read_text()\n"
                      "if text == 'broken': sys.exit('feed handler unreachable')\n"
                      "print(text)\n", encoding="utf-8")
    stack.call("PUT", "/scripts/flag", json={"name": "flag", "argv": [sys.executable, str(script), str(state)],
                                             "output": "json", "timeout_secs": 30})
    alert = stack.call("POST", "/alerts", json={
        "name": "E2E flag", "enabled": False, "interval_secs": 0,
        "steps": [{"kind": "script", "name": "flag", "script": "flag"}],
        "condition": {"type": "any_row", "column": "lag", "op": ">", "value": 60},
        "channels": {"email_to": ["oncall@example.com"],
                     "notify_on_error": True, "error_after": 2, "notify_on_recovery": True}})
    run = lambda: stack.call("POST", f"/alerts/{alert['id']}/run")   # noqa: E731
    def subjects():
        return [line.split(": ", 1)[1] for m in stack.smtp.messages for line in m["data"].splitlines()
                if line.startswith("Subject: ") and "E2E flag" in line]

    state.write_text('[{"feed": "TSE", "lag": 95}]')
    assert run()["notified"] == "trigger"
    state.write_text('[{"feed": "TSE", "lag": 3}]')
    assert run()["notified"] == "recovery"
    state.write_text("broken")
    first, second, third = run(), run(), run()
    assert [r["status"] for r in (first, second, third)] == ["error", "error", "error"]
    assert [r["notified"] for r in (first, second, third)] == [None, "error", None]
    assert "feed handler unreachable" in second["error"]
    assert subjects() == ["[WARNING] [Heimdall] E2E flag: TRIGGERED", "[Heimdall] E2E flag: RECOVERED",
                          "[Heimdall] E2E flag: ERROR"]


def test_07_sleipnir_runs_a_schedule_on_the_minute(stack):
    sched_alert = stack.call("POST", "/alerts", json={
        "name": "E2E scheduled", "enabled": False, "interval_secs": 0,
        "steps": [{"kind": "kdb", "name": "fills", "connection": "oms", "table": "fills"}],
        "condition": {"type": "row_count", "op": ">=", "value": 1}})
    stack.call("POST", "/schedules", json={"alert_id": sched_alert["id"], "action": "run",
                                           "cron": "* * * * *", "tz": "Europe/London"})
    stack.call("POST", "/schedules", json={"alert_id": sched_alert["id"], "action": "enable",
                                           "cron": "* * * * *", "tz": "Asia/Tokyo"})
    runs = wait_until(lambda: (r := stack.call("GET", "/runs", alert_id=sched_alert["id"]))["total"] and r,
                      90, "Sleipnir to run the alert", every=1)
    assert runs["runs"][0]["source"] == "schedule"
    assert wait_until(lambda: stack.call("GET", f"/alerts/{sched_alert['id']}")["enabled"], 15,
                      "Sleipnir to enable the alert")
    actions = [e["action"] for e in stack.call("GET", "/events", alert_id=sched_alert["id"])]
    assert "schedule applied" in actions and "enabled" in actions
    assert stack.call("GET", "/status")["engine"]["sleipnir_seen"]


def test_08_history_cleanup(stack):
    total = stack.call("GET", "/runs")["total"]
    dry = stack.call("POST", "/history/purge", json={"keep_last": 1, "dry_run": True})
    assert 0 < dry["runs"] < total
    stack.call("POST", "/engine/pause")
    done = stack.call("POST", "/history/purge", json={"keep_last": 1})
    assert done["runs"] >= dry["runs"]
    per_alert = {r["alert_id"] for r in stack.call("GET", "/runs")["runs"]}
    assert stack.call("GET", "/runs")["total"] == len(per_alert)
    assert "history cleaned" in [e["action"] for e in stack.call("GET", "/events")]
    stack.call("POST", "/engine/resume")


def test_09_muninn_survives_a_restart(stack):
    before = {a["name"] for a in stack.call("GET", "/alerts")}
    runs_before = stack.call("GET", "/runs")["total"]
    stack.stop("huginn")
    stack.start_huginn()
    assert {a["name"] for a in stack.call("GET", "/alerts")} == before
    assert stack.call("GET", "/runs")["total"] >= runs_before
    assert stack.call("GET", "/connections")


def test_10_bifrost_serves_and_its_pages_render_against_huginn(stack, monkeypatch):
    port = stack.ports["bifrost"]
    wait_until(lambda: httpx.get(f"http://127.0.0.1:{port}/_stcore/health", timeout=2).text == "ok", 60,
               "Bifrost to start\n" + stack.log("bifrost"))
    assert "<title>Streamlit</title>" in httpx.get(f"http://127.0.0.1:{port}/").text or \
        httpx.get(f"http://127.0.0.1:{port}/").status_code == 200

    from streamlit.testing.v1 import AppTest
    from heimdall.bifrost import common
    monkeypatch.setenv("HEIMDALL_CONFIG", str(stack.config))
    monkeypatch.setenv("HEIMDALL_E2E_TOKEN", TOKEN)
    common._client.clear()
    script = ROOT / "tests" / "e2e" / "_page.py"
    for view in ("overview", "alerts", "history", "schedules", "settings", "builder"):
        at = AppTest.from_file(str(script), default_timeout=60)
        at.session_state["view"] = view
        at.run()
        assert not at.exception, (view, at.exception)
        assert not [e.value for e in at.error], (view, [e.value for e in at.error])
    at = AppTest.from_file(str(script), default_timeout=60)
    at.session_state["view"] = "builder"
    at.session_state["builder_request"] = {"id": stack.alert_id}
    at.run()
    next(b for b in at.button if b.label == "Test run").click().run()
    assert not at.exception
    assert any("Test run" in m.value for m in at.markdown)


def test_11_stopping_a_part_stops_everything_it_started(stack):
    """`heimdall bifrost` runs Streamlit as a child: stopping it must free the port."""
    port = stack.ports["bifrost"]
    stack.stop("bifrost")

    def port_free():
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", port)) != 0
    assert wait_until(port_free, 15, "Bifrost's port to be released")
