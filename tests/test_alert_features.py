"""Active hours, the all-clear mail and alert groups."""
from datetime import datetime, timedelta, timezone

import pytest

from heimdall.core import hours
from heimdall.core.models import (ActiveHours, Alert, AllClear, Channels, Condition, KdbStep, Rearm,
                                  Window)
from heimdall.core.notify import compose_all_clear, fill
from heimdall.demo import seed

UTC = timezone.utc


def at(text: str) -> datetime:
    """A UTC moment; London is on GMT in January, so these read as London time too."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


OFFICE = ActiveHours(enabled=True, days=[0, 1, 2, 3, 4], windows=[Window(start="09:00", end="17:00")],
                     tz="Europe/London")


# -- active hours ----------------------------------------------------------------------

def test_a_day_window_includes_its_start_and_not_its_end():
    # 2026-01-05 is a Monday
    assert not hours.is_active(OFFICE, at("2026-01-05 08:59"))
    assert hours.is_active(OFFICE, at("2026-01-05 09:00"))
    assert hours.is_active(OFFICE, at("2026-01-05 16:59"))
    assert not hours.is_active(OFFICE, at("2026-01-05 17:00"))
    assert not hours.is_active(OFFICE, at("2026-01-10 12:00"))          # Saturday


def test_an_overnight_window_belongs_to_the_day_it_starts():
    late = ActiveHours(enabled=True, days=[4], windows=[Window(start="22:00", end="02:00")], tz="UTC")
    assert hours.is_active(late, at("2026-01-09 23:30"))                 # Friday night
    assert hours.is_active(late, at("2026-01-10 01:59"))                 # into Saturday
    assert not hours.is_active(late, at("2026-01-10 02:00"))
    assert not hours.is_active(late, at("2026-01-08 23:30"))             # Thursday night
    assert not hours.is_active(late, at("2026-01-09 01:00"))             # Friday morning: Thursday's


def test_time_zone_and_daylight_saving():
    tokyo = ActiveHours(enabled=True, days=[], windows=[Window(start="09:00", end="15:00")], tz="Asia/Tokyo")
    assert hours.is_active(tokyo, at("2026-01-05 00:30"))                # 09:30 in Tokyo
    assert not hours.is_active(tokyo, at("2026-01-05 07:00"))            # 16:00 in Tokyo
    # London in July is on BST (UTC+1): 08:30 UTC is 09:30 local
    assert hours.is_active(OFFICE, at("2026-07-06 08:30"))
    assert not hours.is_active(OFFICE, at("2026-07-06 16:30"))


def test_hours_that_are_off_or_empty_never_hold_an_alert_back():
    assert hours.is_active(ActiveHours(), at("2026-01-10 03:00"))
    assert hours.is_active(ActiveHours(enabled=True, windows=[]), at("2026-01-10 03:00"))


def test_next_open_and_describe():
    assert hours.next_open(OFFICE, at("2026-01-05 18:00")) == at("2026-01-06 09:00")
    assert hours.next_open(OFFICE, at("2026-01-09 18:00")) == at("2026-01-12 09:00")   # over the weekend
    assert hours.next_open(ActiveHours(), at("2026-01-09 18:00")) is None
    assert hours.describe(OFFICE) == "09:00-17:00 Mon-Fri (Europe/London)"
    two = ActiveHours(enabled=True, days=[0, 2, 3], tz="UTC",
                      windows=[Window(start="08:00", end="12:00"), Window(start="13:00", end="18:00")])
    assert hours.describe(two) == "08:00-12:00, 13:00-18:00 Mon, Wed-Thu (UTC)"
    assert hours.describe(ActiveHours()) == "Any time"


def test_problems_with_active_hours():
    assert hours.problems(ActiveHours(enabled=True, windows=[])) != []
    assert "starts and ends" in hours.problems(
        ActiveHours(enabled=True, windows=[Window(start="10:00", end="10:00")]))[0]
    assert "unknown time zone" in hours.problems(ActiveHours(enabled=True, tz="Mars/Base"))[0]
    with pytest.raises(ValueError):
        Window(start="25:00")
    assert Window(start="9:30").start == "09:30"


# -- the engine ------------------------------------------------------------------------

def _alert(store, **kw):
    seed(store)
    base = dict(name="orders exist", interval_secs=60, enabled=True,
                steps=[KdbStep(name="o", connection="demo_kdb", table="orders")],
                condition=Condition(type="has_rows"), rearm=Rearm(mode="transition"),
                channels=Channels(email_to=["desk@example.com"]))
    base.update(kw)
    return store.save_alert(Alert(**base))


def _quiet(**kw):
    """A condition the mock never meets: no orders for a made-up symbol."""
    return dict(steps=[KdbStep(name="o", connection="demo_kdb", mode="raw",
                               raw_q="select from orders where sym in `NONE")], **kw)


def test_outside_active_hours_regular_checks_stop_and_the_alert_is_disarmed(store, engine, sent):
    a = _alert(store, active_hours=OFFICE)
    engine.clock = lambda: at("2026-01-05 10:00")
    assert a.id in [x.id for x in engine.due(engine.clock())]
    engine.run_now(a.id)
    assert store.state(a.id)["last_triggered"] == 1 and len(sent["email"]) == 1

    engine.clock = lambda: at("2026-01-05 18:00")
    engine.paused = True                       # only the hours are watched, nothing runs
    engine.tick()
    assert a.id not in [x.id for x in engine.due(engine.clock())]
    state = store.state(a.id)
    assert state["off_hours"] == 1 and state["last_triggered"] == 0
    assert engine.next_due(a, state) == at("2026-01-06 09:00")
    changes = store.events(alert_id=a.id)
    assert changes[0]["action"] == "outside active hours" and "Tue 06 Jan 09:00" in changes[0]["detail"]
    engine.tick()
    assert len(store.events(alert_id=a.id)) == len(changes)              # said once, not every tick

    engine.clock = lambda: at("2026-01-06 09:00")
    engine.tick()
    assert store.state(a.id)["off_hours"] == 0
    engine.run_now(a.id)                        # still firing, but this is a new episode: it notifies
    assert len(sent["email"]) == 2


def test_runs_by_hand_still_run_outside_active_hours(store, engine):
    a = _alert(store, active_hours=OFFICE)
    engine.clock = lambda: at("2026-01-10 12:00")
    assert store.run(engine.run_now(a.id))["status"] == "triggered"


def test_all_clear_every_check(store, engine, sent):
    a = _alert(store, **_quiet(channels=Channels(email_to=["desk@example.com"],
                                                 all_clear=AllClear(enabled=True, mode="every_check"))))
    engine.clock = lambda: at("2026-01-05 10:00")
    run = store.run(engine.run_now(a.id))
    assert run["status"] == "ok" and run["notified"] == "all_clear"
    assert [d["kind"] for d in run["deliveries"]] == ["all_clear"]
    subject = sent["email"][0][1].subject
    assert subject == "[Heimdall] orders exist: ALL CLEAR"
    assert "did not fire at 2026-01-05 10:00" in sent["email"][0][1].text


def test_all_clear_once_a_period_and_not_after_a_firing(store, engine, sent):
    a = _alert(store, interval_secs=600, **_quiet(channels=Channels(
        email_to=["desk@example.com"], all_clear=AllClear(enabled=True, interval_secs=3600))))
    t0 = at("2026-01-05 09:00")
    for minutes in (0, 10, 20, 30, 40, 50):
        engine.clock = lambda m=minutes: t0 + timedelta(minutes=m)
        engine.run_now(a.id)
    assert sent["email"] == []
    engine.clock = lambda: t0 + timedelta(minutes=60)
    engine.run_now(a.id)
    assert len(sent["email"]) == 1
    text = sent["email"][0][1].text
    assert "between 2026-01-05 09:00 and 2026-01-05 10:00" in text and "Clean checks: 7" in text

    # a period in which it fires sends no all-clear
    store.save_alert(store.alert(a.id).model_copy(update={"steps": [
        KdbStep(name="o", connection="demo_kdb", table="orders")]}))
    store.update_state(a.id, clear_since=(t0 + timedelta(minutes=60)).isoformat(), clear_checks=0, clear_dirty=0)
    engine.clock = lambda: t0 + timedelta(minutes=70)
    engine.run_now(a.id)                                          # fires: the period is dirty
    sent["email"].clear()
    store.save_alert(store.alert(a.id).model_copy(update={"steps": _quiet()["steps"]}))
    engine.clock = lambda: t0 + timedelta(minutes=125)
    engine.run_now(a.id)                                          # the period ends, but it was dirty
    assert not [m for _, m in sent["email"] if "ALL CLEAR" in m.subject]


def test_all_clear_needs_recipients_and_is_quiet_when_off(store, engine, sent):
    a = _alert(store, **_quiet(channels=Channels(all_clear=AllClear(enabled=True, mode="every_check"))))
    assert store.run(engine.run_now(a.id))["notified"] is None
    b = _alert(store, name="b", **_quiet())
    assert store.run(engine.run_now(b.id))["notified"] is None and sent["email"] == []


def test_all_clear_subject_and_body_placeholders():
    a = Alert(name="Feeds", group="Market data",
              channels=Channels(all_clear=AllClear(enabled=True, subject="{alert}\n ok · {group}",
                                                   body="{checks} checks to {until}, {unknown} kept")))
    msg = compose_all_clear(a, checks=4, since=at("2026-01-05 09:00"), until=at("2026-01-05 10:00"), rows=0)
    assert msg.subject == "Feeds ok · Market data"
    assert msg.text.startswith("4 checks to 2026-01-05 10:00, {unknown} kept")
    assert fill("{x} {alert}", {"alert": "A"}) == "{x} A"


def test_changing_the_all_clear_restarts_its_period(huginn, store):
    a = _alert(store, **_quiet(channels=Channels(email_to=["d@x"], all_clear=AllClear(enabled=True))))
    store.update_state(a.id, clear_since="2026-01-05T09:00:00+00:00", clear_checks=3)
    body = huginn.get(f"/alerts/{a.id}")
    body["channels"]["all_clear"]["interval_secs"] = 7200
    huginn.put(f"/alerts/{a.id}", body)
    assert store.state(a.id)["clear_since"] is None and store.state(a.id)["clear_checks"] == 0


# -- groups ----------------------------------------------------------------------------

def test_groups_rename_merge_dissolve_and_switch(huginn, store):
    for name, group in (("a", "Equities"), ("b", "Equities"), ("c", "Futures"), ("d", "")):
        _alert(store, name=name, group=group, enabled=False)
    assert [(g["name"], g["alerts"]) for g in huginn.get("/groups")] == [("Equities", 2), ("Futures", 1)]

    assert huginn.post("/groups/switch", {"group": "Equities", "enabled": True})["changed"] == 2
    assert {a.name for a in store.alerts() if a.enabled and a.name in "abcd"} == {"a", "b"}

    huginn.post("/groups/rename", {"old": "Futures", "new": "  Equities "})          # merge
    assert [(g["name"], g["alerts"]) for g in huginn.get("/groups")] == [("Equities", 3)]
    huginn.post("/groups/rename", {"old": "Equities", "new": ""})                   # dissolve
    assert huginn.get("/groups") == [] and all(a.group == "" for a in store.alerts())

    status = {a["name"]: a for a in huginn.get("/status")["alerts"]}
    assert status["a"]["group"] == "" and status["a"]["active_hours"] == "Any time"
