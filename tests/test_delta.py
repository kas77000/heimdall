"""Re-notifying a firing alert when a value moved enough since the last notification."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from heimdall.core import chain, conditions, evaluate
from heimdall.core.chain import StepOutcome
from heimdall.core.models import Alert, Channels, Condition, KdbStep, Rearm
from heimdall.demo import seed

T0 = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)


def R(**kw) -> Rearm:
    return Rearm(**({"mode": "delta", "delta_column": "px"} | kw))


# -- measuring and comparing ---------------------------------------------------------

def test_measure_whole_table_per_key_and_rows():
    df = pd.DataFrame({"sym": ["A", "A", "B"], "px": [1.0, 3.0, 10.0]})
    assert conditions.measure(R(), df) == {"": 10.0}
    assert conditions.measure(R(delta_agg="sum", delta_key="sym"), df) == {"A": 4.0, "B": 10.0}
    assert conditions.measure(R(delta_measure="rows"), df) == {"": 3.0}
    assert conditions.measure(R(delta_measure="rows", delta_key="sym"), df) == {"A": 2.0, "B": 1.0}
    with pytest.raises(conditions.ConditionError, match="not in the result"):
        conditions.measure(R(delta_column="nope"), df)
    with pytest.raises(conditions.ConditionError, match="not numeric"):
        conditions.measure(R(delta_column="sym"), df)


def test_moves_by_amount_percent_and_direction():
    assert conditions.moves(R(delta_amount=5), {"": 100}, {"": 104}) == []
    assert conditions.moves(R(delta_amount=5), {"": 100}, {"": 95}) == ["max px moved 100 → 95 (-5, -5.00%)"]
    assert conditions.moves(R(delta_amount=5, delta_unit="pct"), {"": 100}, {"": 104}) == []
    assert conditions.moves(R(delta_amount=5, delta_unit="pct"), {"": 100}, {"": 106}) != []
    assert conditions.moves(R(delta_amount=1, delta_direction="up"), {"": 10}, {"": 5}) == []
    assert conditions.moves(R(delta_amount=1, delta_direction="down"), {"": 10}, {"": 5}) != []
    assert conditions.moves(R(delta_amount=1, delta_unit="pct"), {"": 0}, {"": 2}) != []


def test_moves_per_key_and_new_keys():
    r = R(delta_key="sym", delta_amount=1)
    moved = conditions.moves(r, {"A": 10, "B": 20}, {"A": 10.5, "B": 22, "C": 1})
    assert moved == ["max px for B moved 20 → 22 (+2, +10.00%)", "max px for C is new (1)"]
    assert conditions.moves(r, {"A": 10, "B": 20}, {"A": 10}) == []     # a key going away is not a move


def test_describe_and_validate():
    assert conditions.describe_delta(R(delta_amount=2, delta_unit="pct", delta_key="sym")) == (
        "Notifies again while firing when the max of px for any sym moves by at least 2% "
        "since the last notification")
    bad = Alert(name="x", steps=[KdbStep(name="o", connection="k", table="t")],
                rearm=Rearm(mode="delta", delta_column="", delta_amount=0))
    problems = chain.validate(bad, {}, {})
    assert any("column to watch" in p for p in problems)
    assert any("above zero" in p for p in problems)


# -- through the engine --------------------------------------------------------------

@pytest.fixture
def table(monkeypatch):
    """The judged table the next run sees: set ``table["df"]``."""
    box = {"df": pd.DataFrame()}
    monkeypatch.setattr(evaluate, "run_steps",
                        lambda alert, res: [StepOutcome(name="o", kind="kdb", df=box["df"])])
    return box


def _alert(store, rearm: Rearm):
    seed(store)
    return store.save_alert(Alert(
        name="px watch", enabled=True, interval_secs=60,
        steps=[KdbStep(name="o", connection="demo_kdb", table="orders")],
        condition=Condition(type="any_row", column="px", op=">", value=100),
        rearm=rearm, channels=Channels(email_to=["desk@example.com"])))


def _run(engine, store, a, df, table, minutes):
    table["df"] = df
    engine.clock = lambda: T0 + timedelta(minutes=minutes)
    return store.run(engine.run_now(a.id))


def test_a_firing_alert_notifies_again_only_when_the_value_moved_enough(store, engine, sent, table):
    a = _alert(store, R(delta_amount=5))
    px = lambda v: pd.DataFrame({"sym": ["A"], "px": [v]})   # noqa: E731

    assert _run(engine, store, a, px(110), table, 0)["notified"] == "trigger"      # first firing
    assert _run(engine, store, a, px(113), table, 1)["notified"] is None          # +3: not enough
    run = _run(engine, store, a, px(116), table, 2)                                # +6 from 110
    assert run["notified"] == "trigger" and run["triggered"]
    assert "moved 110 → 116" in run["message"]
    assert "Firing again because max px moved 110 → 116" in sent["email"][-1][1].text
    assert _run(engine, store, a, px(119), table, 3)["notified"] is None          # +3 from 116
    assert _run(engine, store, a, px(111), table, 4)["notified"] == "trigger"     # -5 from 116
    assert len(sent["email"]) == 3

    assert _run(engine, store, a, px(50), table, 5)["status"] == "ok"             # clears
    assert store.state(a.id)["delta_ref"] is None
    assert _run(engine, store, a, px(112), table, 6)["notified"] == "trigger"     # a new episode
    assert len(sent["email"]) == 4


def test_per_key_a_new_symbol_notifies(store, engine, sent, table):
    a = _alert(store, R(delta_key="sym", delta_amount=10, delta_unit="pct"))
    df = pd.DataFrame({"sym": ["A", "B"], "px": [200.0, 150.0]})
    assert _run(engine, store, a, df, table, 0)["notified"] == "trigger"
    df2 = pd.DataFrame({"sym": ["A", "B"], "px": [210.0, 160.0]})                 # +5%, +6.7%
    assert _run(engine, store, a, df2, table, 1)["notified"] is None
    df3 = pd.DataFrame({"sym": ["A", "B", "C"], "px": [210.0, 160.0, 101.0]})
    run = _run(engine, store, a, df3, table, 2)
    assert run["notified"] == "trigger" and "for C is new" in run["message"]


def test_rows_added_or_removed_notifies_again(store, engine, sent, table):
    a = _alert(store, Rearm(mode="rows_change"))
    rows = lambda n: pd.DataFrame({"sym": [f"S{i}" for i in range(n)], "px": [150.0] * n})   # noqa: E731

    assert _run(engine, store, a, rows(3), table, 0)["notified"] == "trigger"     # first firing
    assert _run(engine, store, a, rows(3), table, 1)["notified"] is None          # same count
    run = _run(engine, store, a, rows(5), table, 2)
    assert run["notified"] == "trigger" and "went from 3 to 5 rows (2 more)" in run["message"]
    assert "Firing again because the result went from 3 to 5 rows (2 more)" in sent["email"][-1][1].text
    run = _run(engine, store, a, rows(4), table, 3)
    assert run["notified"] == "trigger" and "(1 fewer)" in run["message"]
    assert _run(engine, store, a, rows(4), table, 4)["notified"] is None
    assert len(sent["email"]) == 3


def test_a_missing_watch_column_is_an_error(store, engine, table):
    a = _alert(store, R(delta_column="nope"))
    run = _run(engine, store, a, pd.DataFrame({"px": [500.0]}), table, 0)
    assert run["status"] == "error" and "nope" in run["error"]
