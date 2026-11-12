"""Severity from the result: rules on the rows, a column, or the value pick the severity
(or decide not to send); a rise in severity notifies again."""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from heimdall import gjallarhorn
from heimdall.core import conditions, evaluate
from heimdall.core.chain import StepOutcome
from heimdall.core.models import Alert, Channels, Condition, Gjallarhorn, KdbStep, Rearm, SeverityRule
from heimdall.demo import seed

T0 = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
ROWS = [SeverityRule(measure="rows", op=">", value=100, severity="critical"),
        SeverityRule(measure="rows", op=">=", value=50, severity="warning"),
        SeverityRule(measure="rows", op=">=", value=10, severity="info")]


def _a(rules=ROWS, otherwise="skip", **kw) -> Alert:
    return Alert(name="fills", steps=[KdbStep(name="o", connection="k", table="t")],
                 severity_rules=rules, severity_else=otherwise, **kw)


def rows(n) -> pd.DataFrame:
    return pd.DataFrame({"sym": [f"S{i}" for i in range(n)], "status": ["FILLED"] * n, "qty": range(n)})


# -- the rules on their own ---------------------------------------------------------

def test_the_number_of_rows_picks_the_severity():
    a = _a()
    assert conditions.grade(a, rows(150)) == ("critical", "150 rows > 100")
    assert conditions.grade(a, rows(100)) == ("warning", "100 rows ≥ 50")
    assert conditions.grade(a, rows(12)) == ("info", "12 rows ≥ 10")
    assert conditions.grade(a, rows(3)) == ("skip", "no severity rule matched")
    assert conditions.grade(_a(otherwise="info"), rows(3))[0] == "info"


def test_rules_on_the_content_of_the_rows():
    df = rows(4)
    df.loc[2, "status"] = "Rejected by risk"
    a = _a([SeverityRule(measure="any_row", column="status", op="contains", value="REJECTED", severity="critical"),
            SeverityRule(measure="any_row", column="status", op="=", value="filled", severity="info")])
    assert conditions.grade(a, df) == ("critical", "1 row with status contains 'REJECTED'")
    df.loc[2, "status"] = "FILLED"
    assert conditions.grade(a, df)[0] == "skip"                 # = is exact: 'filled' is not 'FILLED'
    a.severity_rules[1].value = "FILLED"
    assert conditions.grade(a, df) == ("info", "4 rows with status = 'FILLED'")


def test_rules_on_a_column_and_on_a_single_value():
    a = _a([SeverityRule(measure="value", column="qty", agg="sum", op=">", value=100, severity="critical"),
            SeverityRule(measure="value", column="qty", agg="max", op="<=", value=3, severity="info")],
           otherwise="warning")
    assert conditions.grade(a, rows(20)) == ("critical", "sum qty 190 > 100")
    assert conditions.grade(a, rows(4)) == ("info", "max qty 3 ≤ 3")
    assert conditions.grade(a, rows(8))[0] == "warning"
    text = pd.DataFrame({"result": ["ERROR: feed down since 09:12"]})
    t = _a([SeverityRule(measure="text", op="starts_with", value="error", severity="critical"),
            SeverityRule(measure="text", op="contains", value="slow", severity="warning")])
    assert conditions.grade(t, text) == ("critical", "the result 'ERROR: feed down since 09:12' starts with 'error'")
    assert conditions.grade(t, pd.DataFrame({"result": ["all good"]}))[0] == "skip"


def test_text_comparisons_in_the_alert_condition_too():
    df = pd.DataFrame({"status": ["FILLED", "REJECTED: limit", None]})
    cond = Condition(type="any_row", column="status", op="contains", value="rejected")
    assert conditions.evaluate(cond, df)
    assert len(conditions.matching_rows(cond, df)) == 1
    assert not conditions.evaluate(Condition(type="all_rows", column="status", op="starts_with", value="F"), df)
    assert conditions.evaluate(Condition(type="any_row", column="status", op="not_contains", value="fill"), df)
    with pytest.raises(conditions.ConditionError, match="compares text"):
        conditions.evaluate(Condition(type="row_count", op="contains", value="x"), df)


def test_a_rule_on_a_missing_column_or_a_text_against_a_number_is_an_error():
    with pytest.raises(conditions.ConditionError, match="'nope' is not in the result"):
        conditions.grade(_a([SeverityRule(measure="any_row", column="nope", op="=", value=1)]), rows(3))
    with pytest.raises(conditions.ConditionError, match="compares text"):
        conditions.grade(_a([SeverityRule(measure="rows", op="contains", value="x")]), rows(3))


def test_describe_a_rule():
    assert conditions.describe_rule(ROWS[0]) == "the number of rows is above 100: Critical"
    assert conditions.describe_rule(SeverityRule(measure="any_row", column="status", op="contains",
                                                 value="REJECTED", severity="skip")) == \
        "any row's status contains 'REJECTED': Don't send"


# -- through the engine ---------------------------------------------------------------

@pytest.fixture
def table(monkeypatch):
    box = {"df": pd.DataFrame()}
    monkeypatch.setattr(evaluate, "run_steps",
                        lambda alert, res: [StepOutcome(name="o", kind="kdb", df=box["df"])])
    return box


def _alert(store, rearm="transition", **kw):
    seed(store)
    return store.save_alert(Alert(
        name="fills", enabled=True, steps=[KdbStep(name="o", connection="demo_kdb", table="orders")],
        condition=Condition(type="has_rows"), rearm=Rearm(mode=rearm),
        severity_rules=ROWS, severity_else="skip",
        channels=Channels(email_to=["desk@example.com"]), **kw))


def _run(engine, store, a, n, table, minutes):
    table["df"] = rows(n)
    engine.clock = lambda: T0 + timedelta(minutes=minutes)
    return store.run(engine.run_now(a.id))


def test_below_the_thresholds_nothing_is_sent_and_it_is_not_firing(store, engine, sent, table):
    a = _alert(store)
    run = _run(engine, store, a, 5, table, 0)
    assert (run["status"], run["notified"], run["condition_met"]) == ("ok", None, False)
    assert "below the severity thresholds" in run["message"] and sent["email"] == []


def test_the_mail_carries_the_severity_from_the_result(store, engine, sent, table):
    a = _alert(store)
    run = _run(engine, store, a, 60, table, 0)
    assert run["notified"] == "trigger" and "[warning: 60 rows ≥ 50]" in run["message"]
    to, msg = sent["email"][-1]
    assert msg.subject.startswith("[WARNING] ") and "Severity warning: 60 rows ≥ 50." in msg.text
    assert "WARNING" in msg.html


def test_a_rise_in_severity_notifies_again_a_fall_does_not(store, engine, sent, table):
    a = _alert(store)                                       # transition: once per firing, normally
    assert _run(engine, store, a, 60, table, 0)["notified"] == "trigger"           # warning
    assert _run(engine, store, a, 70, table, 1)["notified"] is None                 # still warning
    run = _run(engine, store, a, 120, table, 2)                                     # critical
    assert run["notified"] == "trigger" and "it is now critical (120 rows > 100), it was warning" in run["message"]
    assert sent["email"][-1][1].subject.startswith("[CRITICAL] ")
    assert "Firing again because it is now critical" in sent["email"][-1][1].text
    assert _run(engine, store, a, 60, table, 3)["notified"] is None                 # back to warning: quiet
    assert _run(engine, store, a, 130, table, 4)["notified"] is None                # critical was notified already
    assert len(sent["email"]) == 2
    # dropping below every threshold clears the firing; the next one starts afresh
    assert _run(engine, store, a, 2, table, 5)["status"] == "ok"
    assert store.state(a.id)["severity"] is None
    assert _run(engine, store, a, 15, table, 6)["notified"] == "trigger"            # info, a new firing
    assert sent["email"][-1][1].subject.startswith("[INFO] ")


def test_the_pop_up_takes_the_severity_and_its_horn(store, engine, sent, table, tmp_path):
    store.set_settings({"gjallarhorn": {"root": str(tmp_path)}})
    a = _alert(store)
    body = a.model_dump()
    body["channels"]["gjallarhorn"] = Gjallarhorn(enabled=True).model_dump()
    a = store.save_alert(Alert.model_validate(body))
    _run(engine, store, a, 150, table, 0)
    (f,) = (tmp_path / "alerts").rglob("*.json")
    data = json.loads(f.read_text(encoding="utf-8"))
    assert (data["severity"], data["sound"]) == ("critical", "horn:critical")
    assert "Severity critical: 150 rows > 100." in data["lines"]


def test_an_alert_without_rules_keeps_its_fixed_severity(store, engine, sent, table):
    seed(store)
    a = store.save_alert(Alert(name="plain", enabled=True, severity="critical",
                               steps=[KdbStep(name="o", connection="demo_kdb", table="orders")],
                               condition=Condition(type="has_rows"), channels=Channels(email_to=["d@x.com"])))
    _run(engine, store, a, 3, table, 0)
    assert sent["email"][-1][1].subject.startswith("[CRITICAL] ")
    assert gjallarhorn  # imported for the pop-up test above
