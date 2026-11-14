"""Gjallarhorn: alerts as pop-ups on traders' screens, through a folder on the share."""
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from heimdall import gjallarhorn
from heimdall.core.evaluate import Evaluation
from heimdall.core.models import Alert, Channels, Condition, Gjallarhorn, KdbStep
from heimdall.demo import seed

NOW = datetime(2026, 11, 11, 9, 30, tzinfo=timezone.utc)
ROWS = pd.DataFrame({"sym": ["AAPL", "MSFT"], "last": [231.4, None], "desk": ["Équités", "x" * 400]})


def _alert(severity="warning", sound="", **g) -> Alert:
    return Alert(id=7, name="Price outside limits", group="Equities", description="Watched closely.",
                 severity=severity, sound=sound,
                 steps=[KdbStep(name="o", connection="demo_kdb", table="orders")],
                 condition=Condition(type="has_rows"),
                 channels=Channels(gjallarhorn=Gjallarhorn(enabled=True, **g)))


def _fired(table=ROWS) -> Evaluation:
    return Evaluation("triggered", True, True, "trigger", "fired", judged_step="o", judged=table)


@pytest.fixture
def share(tmp_path, store):
    root = tmp_path / "Gjallarhorn"
    store.set_settings({"gjallarhorn": {"root": str(root), "keep_days": 7}})
    return root


# -- what goes on the share ------------------------------------------------------------

def test_the_file_carries_everything_a_trader_sees_and_no_bifrost_link():
    data = gjallarhorn.payload(_alert(severity="critical", audience=["Equities", " equities ", "jdoe"]),
                               _fired(), "trigger", 42, NOW)
    assert data["id"] == "42" and data["severity"] == "critical" and data["needs_ack"]
    assert data["audience"] == ["Equities", "jdoe"]                     # cleaned, no duplicates
    assert data["title"] == "Price outside limits" and data["group"] == "Equities"
    assert data["lines"][0] == "Watched closely." and "returns any rows" in data["lines"][1]
    assert data["rows"]["columns"] == ["sym", "last", "desk"] and data["rows"]["total"] == 2
    assert data["rows"]["data"][1][1] is None and data["rows"]["data"][0][2] == "Équités"
    assert len(data["rows"]["data"][1][2]) == gjallarhorn.MAX_CELL
    assert "http" not in json.dumps(data)


def test_rows_are_capped_but_the_count_is_kept():
    big = pd.DataFrame({"n": range(gjallarhorn.MAX_ROWS + 30)})
    data = gjallarhorn.payload(_alert(), _fired(big), "trigger", 1, NOW)
    assert len(data["rows"]["data"]) == gjallarhorn.MAX_ROWS and data["rows"]["total"] == gjallarhorn.MAX_ROWS + 30


def test_a_recovery_is_information_with_nothing_to_acknowledge():
    data = gjallarhorn.payload(_alert(), Evaluation("ok", False, False, "recovery", "ok"), "recovery", 3, NOW)
    assert data["severity"] == "info" and not data["needs_ack"] and data["title"].startswith("Cleared")


def test_publish_writes_by_day_and_leaves_no_temporary_file(tmp_path):
    s = gjallarhorn.Settings(root=str(tmp_path))
    path = gjallarhorn.publish(s, gjallarhorn.payload(_alert(), _fired(), "trigger", 5, NOW))
    assert path == tmp_path / "alerts" / "2026-11-11" / "5.json"
    assert [p.name for p in path.parent.iterdir()] == ["5.json"]
    assert json.loads(path.read_text(encoding="utf-8"))["id"] == "5"
    with pytest.raises(RuntimeError, match="shared folder"):
        gjallarhorn.publish(gjallarhorn.Settings(), {"id": "1", "fired_at": "2026-11-11"})


def _age(path: Path, at: datetime) -> None:
    for p in [path, *path.rglob("*")]:
        os.utime(p, (at.timestamp(), at.timestamp()))


def test_clean_removes_expired_alerts_then_their_acknowledgements(tmp_path):
    s = gjallarhorn.Settings(root=str(tmp_path), ttl_hours=48)
    old = gjallarhorn.publish(s, gjallarhorn.payload(_alert(), _fired(), "trigger", 1, NOW - timedelta(days=5), s))
    new = gjallarhorn.publish(s, gjallarhorn.payload(_alert(), _fired(), "trigger", 2, NOW, s))
    ack = gjallarhorn.acknowledge(s, "1", "jdoe")
    _age(ack.parent, NOW - timedelta(days=5))
    assert gjallarhorn.clean(s, NOW) == 2
    assert not old.exists() and not old.parent.exists() and new.exists() and not ack.parent.exists()


def test_an_acknowledgement_is_never_removed_before_its_alert(tmp_path):
    # The bug traders met: an alert acknowledged 32 hours earlier came back to be
    # acknowledged again, its acknowledgement cleaned while the alert was still listed.
    s = gjallarhorn.Settings(root=str(tmp_path), ttl_hours=72)
    fired = NOW - timedelta(hours=33)
    alert = gjallarhorn.publish(s, gjallarhorn.payload(_alert(), _fired(), "trigger", 1, fired, s))
    ack = gjallarhorn.acknowledge(s, "1", "jdoe", now=fired + timedelta(hours=1))
    _age(ack.parent, NOW - timedelta(hours=32))
    gjallarhorn.clean(s, NOW)
    assert alert.exists() and ack.exists()
    # once the alert's time is over both go
    gjallarhorn.clean(s, fired + timedelta(hours=73))
    assert not alert.exists() and not ack.parent.exists()
    # an acknowledgement written less than a day ago waits, for Huginn to record it
    late = gjallarhorn.acknowledge(s, "9", "jdoe", now=NOW)
    gjallarhorn.clean(s, datetime.now(timezone.utc))
    assert late.exists()


def test_clean_follows_each_files_own_expiry_and_old_files_by_their_firing(tmp_path):
    s = gjallarhorn.Settings(root=str(tmp_path), ttl_hours=24)
    short = gjallarhorn.payload(_alert(), _fired(), "trigger", 1, NOW - timedelta(hours=3), s)
    short["expires_at"] = (NOW - timedelta(hours=1)).isoformat()          # written with a shorter time to live
    legacy = gjallarhorn.payload(_alert(), _fired(), "trigger", 2, NOW - timedelta(hours=25), s)
    del legacy["expires_at"]                                              # from before expires_at
    fresh = gjallarhorn.payload(_alert(), _fired(), "trigger", 3, NOW - timedelta(hours=23), s)
    paths = [gjallarhorn.publish(s, d) for d in (short, legacy, fresh)]
    assert gjallarhorn.clean(s, NOW) == 2
    assert [p.exists() for p in paths] == [False, False, True]


def test_the_time_to_live_comes_from_the_settings():
    ttl = lambda g: gjallarhorn.Settings.from_store({"gjallarhorn": g}).ttl_hours  # noqa: E731
    assert ttl({}) == gjallarhorn.TTL_HOURS == 24
    assert ttl({"ttl_hours": 6}) == 6 and ttl({"ttl_hours": 9999}) == gjallarhorn.MAX_TTL_HOURS
    assert ttl({"keep_days": 2}) == 48 and ttl({"keep_days": 30}) == gjallarhorn.MAX_TTL_HOURS   # older settings
    assert ttl({"ttl_hours": "x"}) == 24
    data = gjallarhorn.payload(_alert(), _fired(), "trigger", 1, NOW, gjallarhorn.Settings(ttl_hours=6))
    assert data["expires_at"] == "2026-11-11T15:30:00+00:00"


def test_deploy_copies_the_ascii_app():
    for name in (n for n in gjallarhorn.APP_FILES if n.endswith((".ps1", ".cmd", ".txt"))):
        text = (gjallarhorn.APP_DIR / name).read_bytes()
        assert all(b < 128 for b in text), f"{name} must stay ASCII: Windows PowerShell reads it in the local code page"


# -- through Huginn --------------------------------------------------------------------

def _save(store, **g):
    seed(store)
    return store.save_alert(_alert(**g).model_copy(update={"id": None, "enabled": True, "interval_secs": 60}))


def test_a_firing_writes_a_pop_up_and_records_the_delivery(store, engine, share, sent):
    a = _save(store, severity="critical", audience=["Equities"])
    run = store.run(engine.run_now(a.id))
    (f,) = (share / "alerts").rglob("*.json")
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["id"] == str(run["id"]) and data["severity"] == "critical" and data["audience"] == ["Equities"]
    assert [(d["channel"], d["target"], d["ok"]) for d in run["deliveries"]] == [("gjallarhorn", "Equities", True)]
    assert sent["email"] == []                                         # no address: no mail


def test_an_unreachable_share_is_a_failed_delivery_not_a_failed_run(store, engine, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    store.set_settings({"gjallarhorn": {"root": str(blocker / "sub")}})
    a = _save(store)
    run = store.run(engine.run_now(a.id))
    assert run["status"] == "triggered"
    (d,) = run["deliveries"]
    assert d["channel"] == "gjallarhorn" and not d["ok"] and d["error"]


def test_a_recovery_pop_up_does_not_mail_unless_asked(store, engine, share, sent):
    a = _save(store, on_recovery=True)
    a = store.save_alert(a.model_copy(update={"channels": a.channels.model_copy(update={"email_to": ["d@x"]})}))
    engine.run_now(a.id)
    assert len(sent["email"]) == 1
    quiet = a.model_copy(update={"steps": [KdbStep(name="o", connection="demo_kdb", mode="raw",
                                                   raw_q="select from orders where sym in `NONE")]})
    store.save_alert(quiet)
    run = store.run(engine.run_now(a.id))
    assert run["notified"] == "recovery" and len(sent["email"]) == 1        # pop-up only
    kinds = sorted(json.loads(p.read_text(encoding="utf-8"))["kind"] for p in (share / "alerts").rglob("*.json"))
    assert kinds == ["recovery", "trigger"]


def test_acknowledgements_on_the_share_are_recorded_on_the_run(store, engine, share):
    a = _save(store)
    run_id = engine.run_now(a.id)
    gjallarhorn.acknowledge(gjallarhorn.Settings(root=str(share)), str(run_id), "jdoe")
    engine._gjallarhorn_acks(datetime.now(timezone.utc))
    run = store.run(run_id)
    assert run["acknowledged_by"] == "jdoe (Gjallarhorn)" and run["acknowledged_at"]
    assert store.events(alert_id=a.id)[0]["action"] == "acknowledged in Gjallarhorn"
    engine._last_acks = None
    engine._gjallarhorn_acks(datetime.now(timezone.utc))                # read again: recorded once
    assert sum(e["action"] == "acknowledged in Gjallarhorn" for e in store.events(alert_id=a.id)) == 1


def test_acknowledging_in_bifrost_closes_the_pop_ups(store, engine, share, huginn):
    a = _save(store)
    run_id = engine.run_now(a.id)
    huginn.post("/runs/acknowledge", {"run_ids": [run_id]})
    ack = share / "acks" / str(run_id)
    (f,) = ack.glob("*.json")
    assert json.loads(f.read_text(encoding="utf-8"))["note"] == "acknowledged in Bifrost"


def test_the_api_tests_and_publishes(store, engine, share, huginn):
    assert huginn.get("/gjallarhorn")["reachable"] is False                # not created yet
    data = huginn.post("/gjallarhorn/test", {"audience": ["Rates"], "severity": "critical"})
    assert data["kind"] == "test" and data["audience"] == ["Rates"]
    r = huginn.post("/gjallarhorn/deploy", {})
    assert set(r["copied"]) == set(gjallarhorn.APP_FILES) and "install-gjallarhorn.cmd" in r["install"]
    status = huginn.get("/gjallarhorn")
    assert status["reachable"] and status["app_published"] and status["today"] == 1
    assert huginn.get("/settings")["gjallarhorn"]["root"] == str(share)


# -- the trader app, without its window (Windows only) ---------------------------------

POWERSHELL = shutil.which("powershell")


def _app(share: Path, state: Path, *args: str, user: str = "") -> list[dict]:
    """One run of the app without its window; ``user`` stands for another trader's Windows logon."""
    env = dict(os.environ, USERNAME=user) if user else None
    out = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(share / "app" / "gjallarhorn.ps1"), "-DryRun", "-StateDir", str(state), *args],
                         capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell only")
def test_the_trader_app_filters_pops_once_and_acknowledges(store, engine, share, tmp_path):
    s = gjallarhorn.Settings(root=str(share))
    gjallarhorn.deploy_app(s)
    me = os.environ.get("USERNAME", "")
    now = datetime.now(timezone.utc)
    for i, aud in enumerate([[], ["Equities"], ["Rates"], [me]], start=1):
        gjallarhorn.publish(s, gjallarhorn.payload(_alert(audience=aud), _fired(), "trigger", i, now))
    state = tmp_path / "state"

    first = _app(share, state, "-Once", "-Desk", "Equities, FX")
    assert sorted(e["id"] for e in first if e["event"] == "popup") == ["1", "2", "4"]   # not Rates
    assert first[-1] == {"count": 3, "ids": first[-1]["ids"], "event": "pending"}
    again = _app(share, state, "-Once", "-Desk", "Equities")
    assert [e["event"] for e in again] == ["pending"]                   # seen: no second pop-up

    assert _app(share, state, "-AckId", "2", "-Desk", "Equities")[0]["event"] == "acked"
    (ack,) = (share / "acks" / "2").glob("*.json")
    assert json.loads(ack.read_text(encoding="utf-8-sig"))["user"] == me
    assert sorted(_app(share, state, "-Once", "-Desk", "Equities")[-1]["ids"]) == ["1", "4"]


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell only")
def test_the_trader_app_does_not_flood_a_new_pc_with_old_alerts(share, tmp_path):
    s = gjallarhorn.Settings(root=str(share))
    gjallarhorn.deploy_app(s)
    now = datetime.now(timezone.utc)
    gjallarhorn.publish(s, gjallarhorn.payload(_alert(), _fired(), "trigger", 1, now - timedelta(hours=3)))
    gjallarhorn.publish(s, gjallarhorn.payload(_alert(), _fired(), "trigger", 2, now))
    events = _app(share, tmp_path / "state", "-Once")
    assert [e["id"] for e in events if e["event"] == "popup"] == ["2"]
    assert sorted(events[-1]["ids"]) == ["1", "2"]                      # both still to acknowledge


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell only")
def test_the_trader_app_parses_and_stays_ascii():
    for name in ("gjallarhorn.ps1", "install-gjallarhorn.ps1"):
        path = gjallarhorn.APP_DIR / name
        cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{path}', [ref]$null, [ref]$e); $e.Count")
        out = subprocess.run([POWERSHELL, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == "0", out.stdout + out.stderr


# -- severity and sound ----------------------------------------------------------------

def test_severity_moved_from_the_pop_up_to_the_alert():
    old = {"name": "x", "channels": {"gjallarhorn": {"enabled": True, "severity": "critical"}}}
    assert Alert.model_validate(old).severity == "critical"
    assert Alert(name="y").severity == "warning" and Alert(name="y").sound == ""
    with pytest.raises(ValueError):
        Alert(name="z", sound="file:../../evil.exe")


def test_the_sound_follows_the_severity_unless_the_alert_or_settings_say_otherwise():
    s = gjallarhorn.Settings(root="x", sounds={"warning": "system:beep"})
    assert gjallarhorn.payload(_alert(severity="critical"), _fired(), "trigger", 1, NOW, s)["sound"] == "horn:critical"
    assert gjallarhorn.payload(_alert(severity="warning"), _fired(), "trigger", 1, NOW, s)["sound"] == "system:beep"
    assert gjallarhorn.payload(_alert(sound="none"), _fired(), "trigger", 1, NOW, s)["sound"] == "none"
    rec = gjallarhorn.payload(_alert(severity="critical"), Evaluation("ok", False, False, "recovery", "ok"),
                              "recovery", 2, NOW, s)
    assert rec["severity"] == "info" and rec["sound"] == "horn:info"


def test_a_firing_mail_says_its_severity():
    from heimdall.core.notify import compose
    msg = compose(_alert(severity="critical"), _fired(), "trigger")
    assert msg.subject == "[CRITICAL] [Heimdall] Price outside limits: TRIGGERED" and "CRITICAL" in msg.html
    assert compose(_alert(), Evaluation("ok", False, False, "recovery", "ok"), "recovery").subject.startswith("[Heimdall]")


def test_the_horns_are_real_wav_files_growing_with_severity():
    import wave
    lengths = []
    for level in ("info", "warning", "critical"):
        with wave.open(str(gjallarhorn.APP_DIR / "sounds" / f"horn-{level}.wav")) as w:
            assert w.getnchannels() == 1 and w.getframerate() == 22050
            lengths.append(w.getnframes())
    assert lengths == sorted(lengths)


WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28


def test_sounds_are_uploaded_listed_previewed_and_removed(store, engine, share, huginn):
    import base64
    from heimdall.client import HuginnError
    r = huginn.post("/gjallarhorn/sounds", {"name": "desk bell.wav", "data": base64.b64encode(WAV).decode()})
    assert r["sound"] == "file:desk bell.wav" and (share / "sounds" / "desk bell.wav").read_bytes() == WAV
    listed = huginn.get("/gjallarhorn/sounds")
    assert listed["files"][0]["sound"] == "file:desk bell.wav" and listed["defaults"]["critical"] == "horn:critical"
    assert huginn.get_bytes("/gjallarhorn/sound", sound="file:desk bell.wav") == WAV
    assert huginn.get_bytes("/gjallarhorn/sound", sound="horn:warning")[:4] == b"RIFF"
    with pytest.raises(HuginnError):
        huginn.post("/gjallarhorn/sounds", {"name": "x.mp3", "data": base64.b64encode(b"ID3...").decode()})
    assert huginn.delete("/gjallarhorn/sounds/desk bell.wav")["deleted"]
    assert huginn.get("/gjallarhorn/sounds")["files"] == []


def test_deploy_ships_the_horns(store, engine, share, huginn):
    huginn.post("/gjallarhorn/deploy", {})
    assert (share / "app" / "sounds" / "horn-critical.wav").is_file()


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell only")
def test_the_trader_app_draws_its_horn_icon(tmp_path):
    ico = tmp_path / "gj.ico"
    out = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(gjallarhorn.APP_DIR / "gjallarhorn.ps1"), "-WriteIcon", str(ico)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    data = ico.read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00" and int.from_bytes(data[4:6], "little") == 6
    assert b"\x89PNG" in data


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell only")
def test_the_trader_app_knows_each_alerts_sound(share, tmp_path):
    s = gjallarhorn.Settings(root=str(share))
    gjallarhorn.deploy_app(s)
    now = datetime.now(timezone.utc)
    gjallarhorn.publish(s, gjallarhorn.payload(_alert(severity="critical"), _fired(), "trigger", 1, now, s))
    data = gjallarhorn.payload(_alert(), _fired(), "trigger", 2, now, s)
    del data["sound"]                                                   # a file from before sounds
    gjallarhorn.publish(s, data)
    sounds = {e["id"]: e["sound"] for e in _app(share, tmp_path / "state", "-Once") if e["event"] == "popup"}
    assert sounds == {"1": "horn:critical", "2": "horn:warning"}
