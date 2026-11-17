"""Steps that keep their result for the day: daily price limits fetched once."""
from datetime import datetime, timezone

import pandas as pd

from heimdall.core.bloomberg import FakeBloomberg
from heimdall.core.chain import StepCache, run_steps, validate
from heimdall.core.evaluate import cache_day
from heimdall.core.models import ActiveHours, Alert, BloombergStep, ComputeStep, KdbStep


class Counting(FakeBloomberg):
    def __init__(self, blank: set[str] = frozenset()):
        self.asked: list[list[str]] = []
        self.blank = set(blank)

    def fetch(self, operation, securities, fields, overrides=None):
        self.asked.append(list(securities))
        df = super().fetch(operation, securities, fields, overrides)
        for s in self.blank:                    # a limit not published yet
            df.loc[df["security"] == s, fields] = None
        return df


class Kdb:
    def __init__(self, syms):
        self.syms, self.queries = syms, []

    def query(self, q):
        self.queries.append(q)
        return pd.DataFrame({"sym": self.syms})


class Res:
    def __init__(self, syms, bbg):
        self._kdb, self._bbg = Kdb(syms), bbg

    def kdb(self, name):
        return self._kdb

    def bloomberg(self, name):
        return self._bbg

    def script(self, name):
        raise LookupError(name)


def _alert(keep_orders=""):
    return Alert(id=1, name="JP limits", steps=[
        KdbStep(name="orders", connection="k", mode="raw", raw_q="select sym from target", keep_for=keep_orders),
        BloombergStep(name="limits", connection="b", operation="bdp", securities="{{orders.sym}} JT Equity",
                      fields=["MIN_LIMIT", "MAX_LIMIT"], keep_for="day"),
        BloombergStep(name="prices", connection="b", operation="snap", securities="{{orders.sym}} JT Equity",
                      fields=["BID", "ASK"])])


def test_limits_are_asked_once_a_day_and_only_for_new_securities():
    bbg, cache = Counting(), StepCache()
    res = Res(["7203", "9984"], bbg)
    first = run_steps(_alert(), res, cache=cache, day="2026-11-17")
    assert [o.error for o in first] == [None, None, None]
    assert bbg.asked == [["7203 JT Equity", "9984 JT Equity"], ["7203 JT Equity", "9984 JT Equity"]]
    res._kdb.syms = ["7203", "9984", "6758"]                 # a new order came in
    second = run_steps(_alert(), res, cache=cache, day="2026-11-17")
    limits = second[1].df
    assert list(limits["security"]) == ["7203 JT Equity", "9984 JT Equity", "6758 JT Equity"]
    assert list(limits["sym"]) == ["7203", "9984", "6758"]   # joins back to the orders
    assert limits["MAX_LIMIT"].notna().all()
    assert bbg.asked[2] == ["6758 JT Equity"]                # limits: only the new one
    assert len(bbg.asked[3]) == 3                            # prices: every time
    assert "2 kept from earlier today, 1 asked" in second[1].request
    run_steps(_alert(), res, cache=cache, day="2026-11-18")  # the next day asks again
    assert len(bbg.asked[4]) == 3


def test_a_limit_not_published_yet_is_asked_again():
    bbg, cache = Counting(blank={"9984 JT Equity"}), StepCache()
    res = Res(["7203", "9984"], bbg)
    out = run_steps(_alert(), res, cache=cache, day="d")
    assert out[1].df["MAX_LIMIT"].isna().sum() == 1          # shown as missing, not dropped
    bbg.blank.clear()
    run_steps(_alert(), res, cache=cache, day="d")
    assert bbg.asked[2] == ["9984 JT Equity"]


def test_without_a_cache_or_with_the_step_changed_everything_is_asked():
    bbg, cache = Counting(), StepCache()
    res = Res(["7203"], bbg)
    run_steps(_alert(), res)                                 # a test run in Bifrost: no cache
    run_steps(_alert(), res)
    assert len(bbg.asked) == 4
    run_steps(_alert(), res, cache=cache, day="d")
    changed = _alert()
    changed.steps[1].fields = ["MIN_LIMIT", "MAX_LIMIT", "PX_LAST"]
    run_steps(changed, res, cache=cache, day="d")
    assert bbg.asked[-2] == ["7203 JT Equity"]               # limits asked again: other fields


def test_a_kdb_step_keeps_the_same_query_for_the_day():
    bbg, cache = Counting(), StepCache()
    res = Res(["7203"], bbg)
    run_steps(_alert(keep_orders="day"), res, cache=cache, day="d")
    out = run_steps(_alert(keep_orders="day"), res, cache=cache, day="d")
    assert len(res._kdb.queries) == 1 and "kept from earlier today" in out[0].request


def test_only_kdb_and_bloomberg_steps_keep_and_the_day_follows_active_hours():
    a = _alert()
    a.steps.append(ComputeStep(name="c", source="limits", keep_for="day"))
    assert any("only kdb and Bloomberg" in p for p in validate(a, {}, {}))
    now = datetime(2026, 11, 17, 16, 30, tzinfo=timezone.utc)          # 01:30 the 18th in Tokyo
    tokyo = Alert(name="x", active_hours=ActiveHours(enabled=True, tz="Asia/Tokyo"))
    assert cache_day(tokyo, now) == "2026-11-18"
