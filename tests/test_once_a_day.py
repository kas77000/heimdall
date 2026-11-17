"""An alert that fires at most once a day: after it notifies, no more checks that day."""
from datetime import datetime, timedelta, timezone

import pandas as pd

from heimdall.core.models import ActiveHours, Alert, Channels, Condition, KdbStep, Window
from heimdall.demo import seed
from tests.test_delta import table  # noqa: F401 - the fixture

# 01:00 UTC = 10:00 in Tokyo, a Tuesday
T0 = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)
ROWS = pd.DataFrame({"sym": ["7203.JP"], "limit": ["UP"]})


def _alert(store, **kw):
    seed(store)
    return store.save_alert(Alert(
        name="once", enabled=True, interval_secs=3600, once_a_day=True,
        active_hours=ActiveHours(enabled=True, tz="Asia/Tokyo", windows=[Window(start="09:00", end="15:30")]),
        steps=[KdbStep(name="o", connection="demo_kdb", table="orders")],
        condition=Condition(type="has_rows"), channels=Channels(email_to=["desk@example.com"]), **kw))


def _at(engine, hours):
    now = T0 + timedelta(hours=hours)
    engine.clock = lambda: now
    return now


def test_it_rests_for_the_day_once_it_has_fired(store, engine, sent, table):
    a = _alert(store)
    table["df"] = ROWS.iloc[0:0]
    now = _at(engine, 0)
    assert a.id in [x.id for x in engine.due(now)]
    assert store.run(engine.run_now(a.id))["notified"] is None              # 10:00: nothing yet
    now = _at(engine, 1)
    assert a.id in [x.id for x in engine.due(now)]                         # 11:00: checked again
    table["df"] = ROWS
    assert store.run(engine.run_now(a.id))["notified"] == "trigger"         # fires
    for h in (2, 3, 4):                                                     # 12:00 .. 14:00: no checks
        now = _at(engine, h)
        assert a.id not in [x.id for x in engine.due(now)]
    st = store.states()[a.id]
    assert engine.done_today(a, st, now)
    nxt = engine.next_due(a, st)
    assert nxt == datetime(2026, 11, 18, 9, 0, tzinfo=timezone(timedelta(hours=9)))  # Wednesday 09:00 Tokyo
    assert store.run(engine.run_now(a.id))["notified"] is None              # by hand: runs, no second mail
    assert len(sent["email"]) == 1


def test_the_next_day_it_notifies_again_even_if_it_never_cleared(store, engine, sent, table):
    a = _alert(store)
    table["df"] = ROWS
    _at(engine, 0)
    assert store.run(engine.run_now(a.id))["notified"] == "trigger"
    now = _at(engine, 24)                                                   # Wednesday 10:00 Tokyo
    assert a.id in [x.id for x in engine.due(now)]
    assert store.run(engine.run_now(a.id))["notified"] == "trigger"
    assert len(sent["email"]) == 2


def test_without_once_a_day_nothing_changes(store, engine, table):
    a = _alert(store)
    a = store.save_alert(a.model_copy(update={"once_a_day": False}))
    table["df"] = ROWS
    _at(engine, 0)
    engine.run_now(a.id)
    now = _at(engine, 1)
    assert a.id in [x.id for x in engine.due(now)]
