"""The shipped alert examples/alerts/jp-lcr-limit-up-down.json, run against fakes."""
import json
from pathlib import Path

import pandas as pd

from heimdall.core.chain import StepCache, run_steps, validate
from heimdall.core.fingerprint import result_fingerprint
from heimdall.core.models import Alert, Connection

FILE = Path(__file__).parents[1] / "examples" / "alerts" / "jp-lcr-limit-up-down.json"
ORDERS = pd.DataFrame({"sym": ["7203.JP", "9984.JP", "6758.JP"], "code": ["7203", "9984", "6758"],
                       "side": ["buy", "sell", "buy"], "size": [1000, 500, 300], "trader": ["jdoe"] * 3,
                       "algo": ["VWAP"] * 3, "lcr": ["LCR=0.05"] * 3, "leave": [800.0, 500.0, 300.0],
                       "id_target": [1, 2, 3]})
LIMITS = {"7203": (2500.0, 3500.0), "9984": (8000.0, 10000.0), "6758": (3000.0, 4000.0)}


class Bpipe:
    """7203 bid pinned at its limit up, 9984 offered at its limit down, 6758 trading normally."""

    def __init__(self):
        self.asked, self.tick = [], 0.0

    def fetch(self, operation, securities, fields, overrides=None):
        self.asked.append((operation, list(securities)))
        rows = []
        for sec in securities:
            lo, hi = LIMITS[sec.split()[0]]
            quote = {"7203": (hi, None, hi), "9984": (None, lo, lo + self.tick),
                     "6758": (3500.0, 3510.0, 3505.0 + self.tick)}[sec.split()[0]]
            data = {"MIN_LIMIT": lo, "MAX_LIMIT": hi, "BID": quote[0], "ASK": quote[1], "LAST_PRICE": quote[2]}
            rows.append({"security": sec, **{f: data[f] for f in fields}})
        return pd.DataFrame(rows, columns=["security", *fields])


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
    assert validate(_alert(), conns, {}) == []
    q = _alert().steps[0].raw_q
    assert q.count("[") == q.count("]") and q.count("{") == q.count("}") and q.count("(") == q.count(")")
    assert 'fixmsg like "*LCR=0.*"' in q and "state=`activated" in q


def test_it_finds_the_stocks_at_their_limits_and_asks_limits_once():
    res, cache = Res(ORDERS), StepCache()
    out = run_steps(_alert(), res, cache=cache, day="2026-11-17")
    assert [o.error for o in out] == [None] * 5
    hit = out[-1].df.sort_values("sym")
    assert list(hit["sym"]) == ["7203.JP", "9984.JP"] and list(hit["limit"]) == ["UP", "DOWN"]
    assert "BID" not in hit.columns and "leave" not in hit.columns     # nothing that moves every tick
    first = result_fingerprint(out[-1].df)
    res.bbg.tick = 1.0                                                  # prices move, same stocks pinned
    again = run_steps(_alert(), res, cache=cache, day="2026-11-17")
    assert result_fingerprint(again[-1].df) == first                   # on_change: no second mail
    assert [op for op, _ in res.bbg.asked] == ["bdp", "snap", "snap"]  # limits once, prices each time
    res.orders = pd.concat([ORDERS, ORDERS.iloc[[2]].assign(sym="6501.JP", code="6501", id_target=4)])
    LIMITS["6501"] = (1000.0, 2000.0)
    run_steps(_alert(), res, cache=cache, day="2026-11-17")
    assert res.bbg.asked[3] == ("bdp", ["6501 JT Equity"])              # a new stock: only its limits


def test_it_imports_switched_off_with_its_cache_setting(store, engine, huginn):
    report = huginn.post("/import", json.loads(FILE.read_text(encoding="utf-8")))
    assert report["alerts"] == ["JP LCR orders at limit up/down"]
    a = store.alert_by_name("JP LCR orders at limit up/down")
    assert not a.enabled and a.steps[1].keep_for == "day" and a.steps[2].keep_for == ""


def test_no_orders_is_a_quiet_check_not_an_error():
    out = run_steps(_alert(), Res(ORDERS.iloc[0:0]), cache=StepCache(), day="d")
    assert [o.error for o in out] == [None] * 5 and out[-1].df.empty
