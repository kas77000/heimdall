import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

from heimdall.core import compute, conditions, cron, scripts
from heimdall.core.bloomberg import FakeBloomberg
from heimdall.core.chain import Pool, run_steps, validate
from heimdall.core.kdb import MockKdbClient, build_q
from heimdall.core.models import (Alert, BloombergStep, ComputeStep, Condition, Derive, Filter,
                                  KdbStep, Script, ScriptParam, ScriptStep)
from heimdall.demo import demo_alerts, demo_connections, demo_scripts

PY = sys.executable


def test_guided_query():
    s = KdbStep(name="a", table="orders", columns=["sym", "qty"],
                filters=[Filter(column="sym", op="in", value="AAPL, MSFT"),
                         Filter(column="qty", op=">", value="100", value_type="number"),
                         Filter(column="state", op="=", value="done", negated=True)])
    assert build_q(s) == "select sym, qty from orders where sym in `AAPL`MSFT, qty>100, not state=`done"


def test_mock_kdb_filters_by_sym():
    df = MockKdbClient().query("select sym, last from quote where sym in `AAPL`MSFT")
    assert list(df.columns) == ["sym", "last"] and set(df.sym) == {"AAPL", "MSFT"}


def test_fake_bloomberg_limits_are_ten_percent():
    df = FakeBloomberg().fetch("bdp", ["AAPL US Equity"], ["LIMIT_UP", "LIMIT_DOWN", "PX_LAST"])
    assert df.iloc[0].to_dict() == {"security": "AAPL US Equity", "LIMIT_UP": 110.0,
                                    "LIMIT_DOWN": 90.0, "PX_LAST": 100.0}


def _script(**kw):
    code = "import sys,json; print(json.dumps([{'x': 1, 'a': sys.argv[1]}]))"
    base = dict(name="t", argv=[PY, "-c", code, "{a}"], params=[ScriptParam(name="a")])
    base.update(kw)
    return Script(**base)


def test_script_json_and_single_argument():
    sc = _script()
    df = scripts.parse_output(sc, scripts.run(sc, {"a": "two words & more"}))
    assert df.to_dict("records") == [{"x": 1, "a": "two words & more", "exit_code": 0}]


def test_script_value_csv_exitcode():
    v = Script(name="v", argv=[PY, "-c", "print(42)"], output="value")
    assert scripts.parse_output(v, scripts.run(v, {})).value[0] == 42
    c = Script(name="c", argv=[PY, "-c", "print('a,b'); print('1,2')"], output="csv")
    assert scripts.parse_output(c, scripts.run(c, {})).to_dict("records") == [{"a": 1, "b": 2, "exit_code": 0}]
    e = Script(name="e", argv=[PY, "-c", "raise SystemExit(3)"], output="exitcode")
    assert scripts.parse_output(e, scripts.run(e, {})).value[0] == 3


def test_script_failure_and_timeout():
    bad = Script(name="b", argv=[PY, "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"])
    with pytest.raises(scripts.ScriptError, match="exited with 2: boom"):
        scripts.parse_output(bad, scripts.run(bad, {}))
    slow = Script(name="s", argv=[PY, "-c", "import time; time.sleep(5)"], timeout_secs=1)
    with pytest.raises(scripts.ScriptError, match="longer than 1s"):
        scripts.run(slow, {})


def test_script_params_checked():
    with pytest.raises(scripts.ScriptError, match="needs a value"):
        scripts.resolve_params(_script(), {})
    with pytest.raises(scripts.ScriptError, match="no parameter"):
        scripts.resolve_params(_script(), {"a": "1", "zz": "2"})


def test_compute_join_derive_filter_keep():
    out = {"q": pd.DataFrame({"sym": ["A", "B"], "px": [10, 20]}),
           "l": pd.DataFrame({"sym": ["A", "B"], "up": [9, 25]})}
    step = ComputeStep(name="c", source="q", join_step="l", left_on=["sym"], right_on=["sym"],
                       derive=[Derive(column="over", expr="px - up")], where="over > 0",
                       keep=["sym", "over"])
    assert compute.run_compute(step, out).to_dict("records") == [{"sym": "A", "over": 1}]


def test_conditions():
    df = pd.DataFrame({"v": [1, 5, 9]})
    C = Condition
    assert conditions.evaluate(C(type="has_rows"), df)
    assert not conditions.evaluate(C(type="no_rows"), df)
    assert conditions.evaluate(C(type="row_count", op=">=", value=3), df)
    assert conditions.evaluate(C(type="any_row", column="v", op=">", value="8"), df)
    assert not conditions.evaluate(C(type="all_rows", column="v", op=">", value=2), df)
    assert conditions.evaluate(C(type="aggregate", agg="avg", column="v", op="=", value=5), df)
    assert not conditions.evaluate(C(type="aggregate", agg="max", column="v", op=">", value=1), df.iloc[0:0])
    with pytest.raises(conditions.ConditionError):
        conditions.evaluate(C(type="any_row", column="zz"), df)


def demo_pool():
    conns = {c.name: c for c in demo_connections()}
    sc = {s.name: s for s in demo_scripts()}
    return Pool(conns.get, sc.get)


def test_demo_limit_chain_runs_end_to_end():
    outcomes = run_steps(demo_alerts()[0][0], demo_pool())
    assert [o.error for o in outcomes] == [None] * 4
    assert {"security", "sym", "LIMIT_UP", "LIMIT_DOWN"} <= set(outcomes[2].df.columns)
    assert list(outcomes[3].df.columns) == ["sym", "security", "last", "LIMIT_UP", "LIMIT_DOWN", "side_hit"]


def test_demo_script_chains_run():
    for alert, _ in demo_alerts()[1:]:
        outcomes = run_steps(alert, demo_pool())
        assert all(o.ok for o in outcomes), [o.error for o in outcomes]
    assert "desk" in run_steps(demo_alerts()[2][0], demo_pool())[-1].df.columns


def test_chain_stops_at_first_failure():
    a = Alert(name="x", steps=[KdbStep(name="a", connection="demo_kdb", mode="raw", raw_q="nonsense"),
                               KdbStep(name="b", connection="demo_kdb", table="orders")])
    out = run_steps(a, demo_pool())
    assert len(out) == 1 and "cannot answer" in out[0].error


def test_validate_finds_problems():
    a = Alert(name="x", steps=[
        KdbStep(name="a", connection="missing", table="t"),
        BloombergStep(name="b", connection="demo_kdb", securities="{{later.sym}}", fields=[]),
        ComputeStep(name="c", source="a", where="x.__class__"),
        ScriptStep(name="later", script="nope")],
        condition=Condition(type="any_row", step="zz"))
    problems = "\n".join(validate(a, {c.name: c for c in demo_connections()}, {}))
    for needle in ("no connection called 'missing'", "is a kdb connection", "not an earlier step",
                   "needs at least one field", "uses something", "no registered script",
                   "step 'zz'", "needs a column"):
        assert needle in problems, needle


def test_cron_helpers():
    assert cron.simple([0, 1, 2, 3, 4], "16:30") == "30 16 * * 1,2,3,4,5"
    assert cron.as_simple("30 16 * * 1,2,3,4,5") == ([0, 1, 2, 3, 4], "16:30")
    assert cron.describe("30 16 * * 1,2,3,4,5", "Europe/London") == "weekdays at 16:30 (Europe/London)"
    nxt = cron.next_fire("30 16 * * *", "Europe/London", datetime(2026, 7, 1, 12, tzinfo=timezone.utc))
    assert nxt == datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc)   # BST
    with pytest.raises(cron.CronError):
        cron.validate("every day", "UTC")
    with pytest.raises(cron.CronError):
        cron.zone("Mars/Base")
