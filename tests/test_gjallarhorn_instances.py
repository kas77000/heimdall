"""Several Heimdalls on one share: a colleague's PC, a server, yours, at the same time or not.

Every database numbers its runs from 1, so pop-ups carry the database's own tag:
run 4 of one Heimdall is never taken for run 4 of another, on the share, on a
trader's PC, or when acknowledgements are recorded."""
import json
from datetime import datetime, timezone

import pytest

from heimdall import gjallarhorn
from heimdall.muninn.store import Muninn
from tests.test_gjallarhorn import POWERSHELL, _alert, _app, _fired


def _heimdall(share, path=":memory:"):
    store = Muninn(path)
    store.set_settings({"gjallarhorn": {"root": str(share)}})
    return store, gjallarhorn.Settings.from_store(store.settings())


def test_each_database_makes_its_own_tag_once(tmp_path):
    a = Muninn(tmp_path / "a.db")
    tag = a.get_setting("instance")
    a.close()
    again = Muninn(tmp_path / "a.db")
    assert again.get_setting("instance") == tag and len(tag) == 8       # kept, not made anew
    assert Muninn(tmp_path / "b.db").get_setting("instance") != tag
    again.set_settings({"gjallarhorn": {"root": "x"}})                   # saving settings keeps it
    assert again.get_setting("instance") == tag


def test_the_same_run_number_from_two_heimdalls_is_two_pop_ups(tmp_path):
    share = tmp_path / "Gjallarhorn"
    (_, mine), (_, theirs) = _heimdall(share), _heimdall(share)
    now = datetime.now(timezone.utc)
    p1 = gjallarhorn.publish(mine, gjallarhorn.payload(_alert(), _fired(), "trigger", 4, now, mine))
    p2 = gjallarhorn.publish(theirs, gjallarhorn.payload(_alert(), _fired(), "trigger", 4, now, theirs))
    assert p1 != p2 and p1.exists() and p2.exists()                      # neither overwrites the other
    assert json.loads(p2.read_text(encoding="utf-8"))["instance"] == theirs.instance


def test_each_heimdall_records_only_its_own_acknowledgements(tmp_path):
    share = tmp_path / "Gjallarhorn"
    (_, mine), (_, theirs) = _heimdall(share), _heimdall(share)
    gjallarhorn.acknowledge(mine, gjallarhorn.pop_up_id(mine, 4), "jdoe")
    gjallarhorn.acknowledge(theirs, gjallarhorn.pop_up_id(theirs, 7), "asmith")
    gjallarhorn.acknowledge(mine, "4", "old")                             # written before tags: whose? nobody's
    assert [(a.run_id, a.user) for a in gjallarhorn.acks(mine)] == [(4, "jdoe")]
    assert [(a.run_id, a.user) for a in gjallarhorn.acks(theirs)] == [(7, "asmith")]


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell only")
def test_a_trader_gets_every_heimdalls_pop_ups_and_an_old_ack_does_not_hide_them(tmp_path):
    share = tmp_path / "Gjallarhorn"
    (_, mine), (_, theirs) = _heimdall(share), _heimdall(share)
    gjallarhorn.deploy_app(mine)
    now = datetime.now(timezone.utc)
    gjallarhorn.publish(mine, gjallarhorn.payload(_alert(), _fired(), "trigger", 4, now, mine))
    state = tmp_path / "state"
    _app(share, state, "-AckId", gjallarhorn.pop_up_id(mine, 4))         # my run 4, acknowledged
    gjallarhorn.publish(theirs, gjallarhorn.payload(_alert(), _fired(), "trigger", 4, now, theirs))
    events = _app(share, state, "-Once")
    assert [e["id"] for e in events if e["event"] == "popup"] == [gjallarhorn.pop_up_id(theirs, 4)]
    assert events[-1]["ids"] == [gjallarhorn.pop_up_id(theirs, 4)]       # theirs waits; mine is done
