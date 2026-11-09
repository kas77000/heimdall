from datetime import datetime, timedelta, timezone

import pytest

from heimdall.core.models import Alert, Channels, Condition, KdbStep, Rearm, Schedule
from heimdall.demo import seed
from heimdall.muninn.store import iso, utcnow
from heimdall.sleipnir.scheduler import Sleipnir, due_firings


def _alert(store, **kw):
    seed(store)
    base = dict(name="orders exist", interval_secs=10, enabled=True,
                steps=[KdbStep(name="o", connection="demo_kdb", table="orders")],
                condition=Condition(type="has_rows"), rearm=Rearm(mode="transition"),
                channels=Channels(email_to=["desk@example.com"]))
    base.update(kw)
    return store.save_alert(Alert(**base))


# -- engine ---------------------------------------------------------------------

def test_run_records_history_and_notifies_once_on_transition(store, engine, sent):
    a = _alert(store)
    r1 = store.run(engine.run_now(a.id))
    assert r1["status"] == "triggered" and r1["triggered"] and r1["notified"] == "trigger"
    assert r1["snapshot"]["total"] == 5 and r1["steps"][0]["rows"] == 5
    assert [(d["channel"], d["ok"]) for d in r1["deliveries"]] == [("email", True)]
    r2 = store.run(engine.run_now(a.id))
    assert r2["status"] == "triggered" and r2["notified"] is None     # transition: no repeat
    assert len(sent["email"]) == 1
    assert "Open in Bifrost: http://bifrost/history?run=" in sent["email"][0][1].text


def test_on_change_only_fires_for_new_results(store, engine, sent):
    a = _alert(store, rearm=Rearm(mode="on_change"))
    assert store.run(engine.run_now(a.id))["triggered"]
    r2 = store.run(engine.run_now(a.id))
    assert r2["status"] == "triggered" and not r2["triggered"]


def test_error_notifies_after_n_and_recovery(store, engine, sent):
    a = _alert(store, steps=[KdbStep(name="o", connection="demo_kdb", mode="raw", raw_q="bad q")],
               condition=Condition(type="has_rows"),
               channels=Channels(email_to=["x@y"], notify_on_error=True, error_after=2,
                                 notify_on_recovery=True))
    assert store.run(engine.run_now(a.id))["notified"] is None
    second = store.run(engine.run_now(a.id))
    assert second["status"] == "error" and second["notified"] == "error"
    assert store.run(engine.run_now(a.id))["notified"] is None          # only once
    fixed = a.model_copy(update={"steps": [KdbStep(name="o", connection="demo_kdb", mode="raw",
                                                   raw_q="select from orders where sym in `NONE")]})
    store.save_alert(fixed)
    rec = store.run(engine.run_now(a.id))
    assert rec["status"] == "ok" and rec["notified"] == "recovery"


def test_due_respects_interval_and_enabled(store, engine):
    a = _alert(store)
    now = utcnow()
    assert [x.id for x in engine.due(now) if x.id == a.id] == [a.id]
    engine.run_now(a.id)
    assert a.id not in [x.id for x in engine.due(now + timedelta(seconds=5))]
    assert a.id in [x.id for x in engine.due(now + timedelta(seconds=11))]
    store.set_enabled(a.id, False)
    assert a.id not in [x.id for x in engine.due(now + timedelta(seconds=60))]


def test_preview_records_nothing(store, engine):
    a = _alert(store)
    p = engine.preview(a)
    assert p["status"] == "triggered" and p["steps"][0]["table"]["total"] == 5
    assert store.count_runs() == 0


# -- history cleanup ------------------------------------------------------------

def test_purge_by_keep_last_age_alert_and_ack(store, engine):
    a = _alert(store)
    b = _alert(store, name="second")
    for _ in range(4):
        engine.run_now(a.id)
    engine.run_now(b.id)
    preview = store.purge(keep_last=1, dry_run=True)
    assert preview == {"runs": 3, "events": 0, "dry_run": True} and store.count_runs() == 5
    assert store.purge(keep_last=1)["runs"] == 3
    assert store.count_runs(alert_id=a.id) == 1
    assert store.purge(older_than_days=1)["runs"] == 0
    store._db.execute("UPDATE runs SET started_at = ? WHERE alert_id = ?",
                      (iso(utcnow() - timedelta(days=40)), b.id))
    assert store.purge(older_than_days=30, keep_unacknowledged=True)["runs"] == 0  # unacked firing kept
    assert store.purge(older_than_days=30)["runs"] == 1
    assert store.purge(everything=True, alert_id=a.id)["runs"] == 1
    with pytest.raises(ValueError):
        store.purge()


def test_acknowledge(store, engine):
    a = _alert(store, rearm=Rearm(mode="every_run"))
    ids = [engine.run_now(a.id) for _ in range(3)]
    assert store.count_runs(unacknowledged=True) == 3
    assert store.acknowledge(ids[:1], by="me") == 1
    assert store.acknowledge_all(a.id) == 2
    assert store.stats(utcnow() - timedelta(days=1))["unacknowledged"] == 0


# -- API ----------------------------------------------------------------------

def test_api_alert_lifecycle(huginn, store):
    seed(store)
    body = Alert(name="api alert", interval_secs=0,
                 steps=[KdbStep(name="o", connection="demo_kdb", table="orders")]).model_dump()
    created = huginn.post("/alerts", body)
    assert created["id"] and not created["enabled"]
    assert huginn.post(f"/alerts/{created['id']}/enable")["changed"]
    run = huginn.post(f"/alerts/{created['id']}/run")
    assert run["status"] == "triggered" and run["source"] == "manual"
    status = huginn.get("/status")
    row = next(x for x in status["alerts"] if x["id"] == created["id"])
    assert row["last_status"] == "triggered" and row["unacknowledged"] == 1
    assert huginn.post("/runs/acknowledge", {"alert_id": created["id"]})["acknowledged"] == 1
    events = [e["action"] for e in huginn.get("/events", alert_id=created["id"])]
    assert "created" in events and "enabled" in events


def test_api_rejects_invalid_alert(huginn, store):
    from heimdall.client import HuginnError
    seed(store)
    bad = Alert(name="bad", steps=[KdbStep(name="o", connection="nowhere", table="t")]).model_dump()
    with pytest.raises(HuginnError) as err:
        huginn.post("/alerts", bad)
    assert err.value.status == 422 and "no connection called 'nowhere'" in err.value.problems[0]


def test_api_preview_and_purge_and_export_import(huginn, store):
    seed(store)
    alerts = huginn.get("/alerts")
    limit = next(a for a in alerts if a["name"].startswith("Price outside"))
    p = huginn.post("/alerts/preview", limit)
    assert [s["error"] for s in p["steps"]] == [None] * 4 and p["problems"] == []
    huginn.post(f"/alerts/{limit['id']}/run")
    assert huginn.post("/history/purge", {"everything": True, "dry_run": True})["runs"] == 1
    bundle = huginn.get("/export")
    assert len(bundle["alerts"]) == 3 and len(bundle["schedules"]) == 3
    assert all(not a["enabled"] for a in bundle["alerts"])
    rep = huginn.post("/import", bundle)
    assert rep["alerts"] == [] and len(rep["skipped"]) >= 3
    rep = huginn.post("/import", bundle, replace=True)
    assert len(rep["alerts"]) == 3 and rep["schedules"] == 3
    assert len(huginn.get("/schedules")) == 3


def test_api_token(store, engine):
    from fastapi.testclient import TestClient
    from heimdall.huginn.api import create_app
    import os
    os.environ["HEIMDALL_TEST_TOKEN"] = "s3cret"
    engine.cfg.token_env = "HEIMDALL_TEST_TOKEN"
    c = TestClient(create_app(store, engine, engine.cfg))
    assert c.get("/health").status_code == 200
    assert c.get("/alerts").status_code == 401
    assert c.get("/alerts", headers={"X-Heimdall-Token": "s3cret"}).status_code == 200


def test_connection_in_use_cannot_be_deleted(huginn, store):
    from heimdall.client import HuginnError
    seed(store)
    with pytest.raises(HuginnError) as err:
        huginn.delete("/connections/demo_kdb")
    assert err.value.status == 409


# -- Sleipnir -----------------------------------------------------------------

UTC = timezone.utc


def test_due_firings_folds_missed_and_reports_lateness():
    sched = [{"id": 1, "alert_id": 9, "action": "enable", "cron": "0 * * * *", "tz": "UTC",
              "active": True, "last_fired_at": "2026-01-01T00:00:00+00:00"}]
    due = due_firings(sched, datetime(2026, 1, 1, 3, 10, tzinfo=UTC))
    assert len(due) == 1 and due[0].at == datetime(2026, 1, 1, 3, tzinfo=UTC)
    assert due[0].missed == 2 and due[0].late_secs == 600
    assert due_firings(sched, datetime(2026, 1, 1, 0, 30, tzinfo=UTC)) == []


def test_sleipnir_enables_runs_and_skips_late(huginn, store):
    a = _alert(store, enabled=False, interval_secs=0)
    s_on = store.save_schedule(Schedule(alert_id=a.id, action="enable", cron="0 8 * * *", tz="UTC"))
    s_run = store.save_schedule(Schedule(alert_id=a.id, action="run", cron="30 16 * * *", tz="UTC"))
    start = datetime(2026, 3, 2, 7, 0, tzinfo=UTC)
    for s in (s_on, s_run):
        store.mark_fired(s.id, start)
    clock = {"now": datetime(2026, 3, 2, 8, 0, 20, tzinfo=UTC)}
    sl = Sleipnir(huginn, clock=lambda: clock["now"], run_grace_secs=300)
    done = sl.tick()
    assert [(d["action"], d["outcome"]) for d in done] == [("enable", "applied")]
    assert store.alert(a.id).enabled
    assert sl.tick() == []                                   # not again
    clock["now"] = datetime(2026, 3, 2, 16, 31, tzinfo=UTC)
    done = sl.tick()
    assert [(d["action"], d["outcome"]) for d in done] == [("run", "applied")]
    assert store.runs(alert_id=a.id)[0]["source"] == "schedule"
    clock["now"] = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)   # next day's 16:30, 90 min late
    done = sl.tick()
    outcomes = {d["action"]: d["outcome"] for d in done}
    assert outcomes == {"enable": "unchanged", "run": "skipped"}
    actions = [e["action"] for e in store.events(a.id)]
    assert "schedule skipped" in actions and "schedule applied" in actions


def test_a_long_firing_asks_for_one_acknowledgement(store, engine):
    a = _alert(store, rearm=Rearm(mode="transition"))
    for _ in range(5):
        engine.run_now(a.id)
    assert store.count_runs(alert_id=a.id, status="triggered") == 5
    assert store.count_runs(alert_id=a.id, unacknowledged=True) == 1


def test_config_file_and_environment(tmp_path, monkeypatch):
    from heimdall import config
    f = tmp_path / "h.toml"
    f.write_text('[muninn]\npath = "x.db"\n[huginn]\nport = 9001\nworkers = 2\n'
                 '[sleipnir]\nurl = "http://h:9001"\ntick_secs = 3\n[bifrost]\nurl = "http://b"\n')
    cfg = config.load(f)
    assert (cfg.db_path, cfg.port, cfg.workers, cfg.huginn_url, cfg.sleipnir_tick_secs, cfg.bifrost_url) == \
        ("x.db", 9001, 2, "http://h:9001", 3.0, "http://b")
    monkeypatch.setenv("HEIMDALL_PORT", "9100")
    monkeypatch.setenv("HEIMDALL_TOKEN", "t")
    cfg = config.load(f)
    assert cfg.port == 9100 and cfg.token == "t"


def test_huginn_client_ignores_a_system_proxy(monkeypatch):
    """A company proxy (environment or Windows registry) must not catch calls to Huginn."""
    import http.server
    import threading

    from heimdall.client import HuginnClient

    class Huginn(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Proxy(Huginn):
        def do_GET(self):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), h) for h in (Huginn, Proxy)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    huginn, proxy = (f"http://127.0.0.1:{s.server_address[1]}" for s in servers)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, proxy)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    try:
        assert HuginnClient(huginn).get("/health") == {"ok": True}
    finally:
        for s in servers:
            s.shutdown()
