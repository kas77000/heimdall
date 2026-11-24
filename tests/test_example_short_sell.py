"""The shipped alert examples/alerts/short-sells-without-331-dynamic-code-a.json, run against fakes."""
import json
from pathlib import Path

import pandas as pd

from heimdall.core.chain import StepCache, run_steps, validate
from heimdall.core.models import Alert, Connection

FILE = Path(__file__).parents[1] / "examples" / "alerts" / "short-sells-without-331-dynamic-code-a.json"
# What OMS RT gives back once the q has filtered (sellshort, no 331=1) and built bbg.
ORDERS = pd.DataFrame({"sym": ["7203.JP", "0700.HK", "2330.TW", "6758.JP"],
                       "bbg": ["7203 JT Equity", "0700 HK Equity", "2330 TT Equity", "6758 JT Equity"],
                       "side": ["sellshort"] * 4, "size": [1000, 500, 300, 200], "trader": ["jdoe"] * 4,
                       "algo": ["VWAP"] * 4, "id_target": [1, 2, 3, 4]})
CODES = {"7203 JT Equity": "A", "0700 HK Equity": "N", "2330 TT Equity": "A", "6758 JT Equity": None}


class Bpipe:
    def __init__(self):
        self.asked = []

    def fetch(self, operation, securities, fields, overrides=None):
        self.asked.append((operation, list(securities), list(fields)))
        return pd.DataFrame([{"security": s, "DYNAMIC_SHORT_SALE_CODE_RT": CODES.get(s)} for s in securities],
                            columns=["security", *fields])


class Res:
    def __init__(self, orders):
        self.orders, self.bbg, self.queries = orders, Bpipe(), []

    def kdb(self, name):
        assert name == "OMS RT"
        return self

    def query(self, q):
        self.queries.append(q)
        return self.orders.copy()

    def bloomberg(self, name):
        assert name == "bpipe"
        return self.bbg


def _alert() -> Alert:
    (a,) = json.loads(FILE.read_text(encoding="utf-8"))["alerts"]
    return Alert.model_validate(a)


def test_the_file_is_a_valid_alert_for_the_work_connections():
    conns = {"OMS RT": Connection(name="OMS RT", kind="kdb", host="h", port=1),
             "bpipe": Connection(name="bpipe", kind="bloomberg", host="h", port=8194)}
    a = _alert()
    assert validate(a, conns, {}) == []
    q = a.steps[0].raw_q
    assert q.count("[") == q.count("]") and q.count("{") == q.count("}") and q.count("(") == q.count(")")
    assert "side=`sellshort" in q and 'not fixmsg like "*331=1*"' in q and "m:`TW`JP!`TT`JT;" in q
    assert a.steps[1].fields == ["DYNAMIC_SHORT_SALE_CODE_RT"] and a.steps[1].keep_for == ""


def test_it_lists_the_orders_whose_code_is_a():
    res = Res(ORDERS)
    out = run_steps(_alert(), res, cache=StepCache(), day="d")
    assert [o.error for o in out] == [None] * 3
    hit = out[-1].df
    assert sorted(hit["sym"]) == ["2330.TW", "7203.JP"]
    assert set(hit["DYNAMIC_SHORT_SALE_CODE_RT"]) == {"A"}
    assert res.bbg.asked == [("bdp", list(ORDERS["bbg"]), ["DYNAMIC_SHORT_SALE_CODE_RT"])]
    run_steps(_alert(), res, cache=StepCache(), day="d")
    assert len(res.bbg.asked) == 2                                     # asked again at every check


def test_no_short_sells_is_a_quiet_check():
    out = run_steps(_alert(), Res(ORDERS.iloc[0:0]), cache=StepCache(), day="d")
    assert [o.error for o in out] == [None] * 3 and out[-1].df.empty


def test_it_imports_switched_off(store, engine, huginn):
    report = huginn.post("/import", json.loads(FILE.read_text(encoding="utf-8")))
    assert report["alerts"] == ["Short sells without 331=1 where Bloomberg says A"]
    a = store.alert_by_name("Short sells without 331=1 where Bloomberg says A")
    assert not a.enabled and a.interval_secs == 300 and a.rearm.mode == "rows_change"
