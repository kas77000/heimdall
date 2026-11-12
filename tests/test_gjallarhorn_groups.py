"""Gjallarhorn groups: who gets an alert's pop-ups is set in Heimdall, by Windows user name."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from heimdall import gjallarhorn
from tests.test_gjallarhorn import POWERSHELL, _alert, _app, _fired, _save, share  # noqa: F401

NOW = datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc)


def test_groups_are_cleaned():
    assert gjallarhorn.clean_groups({" Equities ": ["jdoe", "JDOE", " asmith ", ""], "": ["x"],
                                     "equities": ["dup"], "Rates": "bwayne, ckent;dprince"}) == {
        "Equities": ["jdoe", "asmith"], "Rates": ["bwayne", "ckent", "dprince"]}
    assert gjallarhorn.clean_groups(None) == {}


def test_a_group_opens_up_into_its_members():
    s = gjallarhorn.Settings(root="x", groups={"Equities": ["jdoe", "asmith"], "Empty": []})
    assert gjallarhorn.recipients(s, ["equities", "bwayne", "JDOE"]) == ["equities", "jdoe", "asmith", "bwayne"]
    # an empty group still names someone: never "every trader" by accident
    assert gjallarhorn.recipients(s, ["Empty"]) == ["Empty"]
    assert gjallarhorn.recipients(s, []) == []
    data = gjallarhorn.payload(_alert(audience=["Equities"]), _fired(), "trigger", 1, NOW, s)
    assert data["audience"] == ["Equities"] and data["recipients"] == ["Equities", "jdoe", "asmith"]


def test_a_test_pop_up_for_a_group(tmp_path):
    s = gjallarhorn.Settings(root=str(tmp_path), groups={"Rates": ["bwayne"]})
    data = gjallarhorn.publish_test(s, ["Rates"], now=NOW)
    assert data["recipients"] == ["Rates", "bwayne"]


def test_users_running_gjallarhorn_are_listed_and_old_ones_cleaned(tmp_path):
    s = gjallarhorn.Settings(root=str(tmp_path))
    d = tmp_path / "users"
    d.mkdir()
    (d / "jdoe.json").write_text(json.dumps({"user": "jdoe", "machine": "PC1", "desks": []}), encoding="utf-8")
    (d / "old.json").write_text(json.dumps({"user": "old"}), encoding="utf-8")
    long_ago = time.time() - (gjallarhorn.USER_KEEP_DAYS + 1) * 86400
    os.utime(d / "old.json", (long_ago, long_ago))
    seen = gjallarhorn.users(s)
    assert [u["user"] for u in seen] == ["jdoe", "old"]
    assert seen[0]["running"] and not seen[1]["running"] and seen[0]["machine"] == "PC1"
    gjallarhorn.clean(s, datetime.now(timezone.utc))
    assert [u["user"] for u in gjallarhorn.users(s)] == ["jdoe"]


def test_the_api_keeps_groups_apart_from_the_other_settings(store, engine, huginn, tmp_path):
    root = tmp_path / "gj"
    huginn.put("/settings", {"gjallarhorn": {"root": str(root)}})
    assert huginn.put("/gjallarhorn/groups", {"Equities": ["jdoe", "jdoe", "asmith"], "Rates": []}) == {
        "Equities": ["jdoe", "asmith"], "Rates": []}
    # a form saving the folder with what it read before the groups changed does not undo them
    huginn.put("/settings", {"gjallarhorn": {"root": str(root), "keep_days": 3, "groups": {}}})
    assert huginn.get("/gjallarhorn/groups") == {"Equities": ["jdoe", "asmith"], "Rates": []}
    assert huginn.get("/settings")["gjallarhorn"]["keep_days"] == 3
    (root / "users").mkdir(parents=True)
    (root / "users" / "JDoe.json").write_text(json.dumps({"user": "JDoe", "machine": "PC1"}), encoding="utf-8")
    (u,) = huginn.get("/gjallarhorn/users")
    assert u["user"] == "JDoe" and u["groups"] == ["Equities"] and u["running"]
    sent = huginn.post("/gjallarhorn/test", {"audience": ["Equities"], "severity": "info"})
    assert sent["recipients"] == ["Equities", "jdoe", "asmith"]


def test_a_firing_for_a_group_reaches_its_members(store, engine, share, sent):
    store.set_settings({"gjallarhorn": {"root": str(share), "groups": {"Equities": ["jdoe"]}}})
    a = _save(store, audience=["Equities"])
    run = store.run(engine.run_now(a.id))
    (f,) = (share / "alerts").rglob("*.json")
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["recipients"] == ["Equities", "jdoe"]
    assert [(d["channel"], d["target"]) for d in run["deliveries"]] == [("gjallarhorn", "Equities")]


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell only")
def test_the_trader_app_follows_groups_and_says_it_runs(share, tmp_path):
    me = os.environ.get("USERNAME", "")
    s = gjallarhorn.Settings(root=str(share), groups={"Mine": [me], "Others": ["nobody-here"]})
    gjallarhorn.deploy_app(s)
    now = datetime.now(timezone.utc)
    for i, aud in enumerate([["Mine"], ["Others"], ["Empty"]], start=1):
        gjallarhorn.publish(s, gjallarhorn.payload(_alert(audience=aud), _fired(), "trigger", i, now, s))
    events = _app(share, tmp_path / "state", "-Once")                 # no -Desk: groups do it
    assert [e["id"] for e in events if e["event"] == "popup"] == ["1"]
    (u,) = (share / "users").glob("*.json")
    assert json.loads(u.read_text(encoding="utf-8-sig"))["user"] == me
