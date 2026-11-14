"""Several traders on one share, each with their own Gjallarhorn (Windows only).

Each trader is a Windows user name and a state folder of their own, as two people
logged on to two PCs (or to the same PC) are."""
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from heimdall import gjallarhorn
from tests.test_gjallarhorn import POWERSHELL, _age, _alert, _app, _fired

pytestmark = pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell only")
TRADERS = ("alice", "bob", "carol")


@pytest.fixture
def desk(tmp_path):
    share = tmp_path / "Gjallarhorn"
    s = gjallarhorn.Settings(root=str(share), ttl_hours=72, groups={"Equities": ["alice", "bob"]})
    gjallarhorn.deploy_app(s)
    now = datetime.now(timezone.utc)
    pub = lambda i, aud, at, st=s: gjallarhorn.publish(  # noqa: E731
        st, gjallarhorn.payload(_alert(audience=aud), _fired(), "trigger", i, at, st))
    pub(1, ["Equities"], now - timedelta(hours=33))
    pub(2, [], now)                                                     # every trader
    pub(3, ["carol"], now)
    pub(4, [], now - timedelta(hours=30), gjallarhorn.Settings(root=str(share), ttl_hours=24))   # its time is over
    run = lambda user, *a: _app(share, tmp_path / f"state-{user}", *a, user=user)  # noqa: E731
    return s, share, run, now


def _pending(events):
    return sorted(events[-1]["ids"]) if events[-1]["event"] == "pending" else events


def test_each_trader_gets_their_own_alerts_and_none_past_their_time(desk):
    s, share, run, now = desk
    seen = {u: run(u, "-Once") for u in TRADERS}
    assert {u: _pending(e) for u, e in seen.items()} == {"alice": ["1", "2"], "bob": ["1", "2"], "carol": ["2", "3"]}
    assert all(e[-1]["count"] == len(e[-1]["ids"]) for e in seen.values())
    # alert 1 is 33 hours old: listed, but not popped on a first start
    assert sorted(e["id"] for e in seen["alice"] if e["event"] == "popup") == ["2"]
    assert {p.stem for p in (share / "users").glob("*.json")} == set(TRADERS)


def test_one_acknowledgement_clears_the_alert_for_every_trader(desk):
    s, share, run, now = desk
    for u in TRADERS:
        run(u, "-Once")
    assert run("alice", "-AckId", "1")[0] == {"event": "acked", "id": "1", "user": "alice"}
    gjallarhorn.acknowledge(s, "3", "jdoe", note="acknowledged in Bifrost")       # from Bifrost
    assert {u: _pending(run(u, "-Once")) for u in TRADERS} == {"alice": ["2"], "bob": ["2"], "carol": ["2"]}
    assert [json.loads(f.read_text(encoding="utf-8-sig"))["user"] for f in (share / "acks" / "1").glob("*.json")] == ["alice"]


def test_two_traders_acknowledging_at_once_both_count_and_the_first_is_recorded(desk):
    s, share, run, now = desk
    for u in TRADERS:
        run(u, "-Once")
    procs = [subprocess.Popen([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(share / "app" / "gjallarhorn.ps1"), "-DryRun", "-AckId", "2",
                               "-StateDir", str(share.parent / f"state-{u}")],
                              env=dict(os.environ, USERNAME=u), stdout=subprocess.PIPE, text=True)
             for u in ("bob", "carol")]
    outs = [json.loads(p.communicate(timeout=120)[0].splitlines()[0]) for p in procs]
    assert [o["event"] for o in outs] == ["acked", "acked"]
    assert sorted(f.stem for f in (share / "acks" / "2").glob("*.json")) == ["bob", "carol"]
    assert not list((share / "acks" / "2").glob(".*"))                  # no temporary file left
    (first,) = [a for a in gjallarhorn.acks(s) if a.run_id == 2]          # one per run, for Huginn
    assert first.user in ("bob", "carol")
    assert _pending(run("alice", "-Once")) == ["1"]


def test_an_alert_acknowledged_32_hours_ago_stays_acknowledged_after_cleaning(desk):
    s, share, run, now = desk
    run("alice", "-Once")
    run("alice", "-AckId", "1")
    _age(share / "acks" / "1", now - timedelta(hours=32))
    gjallarhorn.clean(s, now)
    assert list((share / "alerts").rglob("1.json")) and (share / "acks" / "1").is_dir()
    assert not list((share / "alerts").rglob("4.json"))                 # past its time: cleaned
    assert {u: _pending(run(u, "-Once")) for u in ("alice", "bob")} == {"alice": ["2"], "bob": ["2"]}


def test_an_expired_alert_leaves_the_list_even_before_huginn_cleans_it(desk):
    s, share, run, now = desk
    assert "4" not in _pending(run("bob", "-Once"))
    (f,) = (share / "alerts").rglob("2.json")
    data = json.loads(f.read_text(encoding="utf-8"))
    data["expires_at"] = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    f.write_text(json.dumps(data), encoding="utf-8")
    assert _pending(run("bob", "-Once")) == ["1"]
