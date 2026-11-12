"""Subject prefixes: [Heimdall] by default, or one of your own, per alert."""
from datetime import datetime, timezone

import pandas as pd

from heimdall.core.evaluate import Evaluation
from heimdall.core.models import Alert, Channels, Condition, KdbStep
from heimdall.core.notify import compose, compose_all_clear, mail_prefix, prefixes
from heimdall.demo import seed


def _a(**ch) -> Alert:
    return Alert(name="Price outside limits", steps=[KdbStep(name="o", connection="k", table="t")],
                 channels=Channels(email_to=["d@x.com"], **ch))


EV = Evaluation("triggered", True, True, "trigger", "fired", judged_step="o", judged=pd.DataFrame({"x": [1]}))


def test_the_default_prefix_and_the_list():
    assert mail_prefix({}) == "Heimdall"
    s = {"smtp": {"prefix": "[COVERAGE]", "prefixes": ["Heimdall", "APAC QUANT TEAM", " coverage ", ""]}}
    assert mail_prefix(s) == "COVERAGE"
    assert prefixes(s) == ["COVERAGE", "Heimdall", "APAC QUANT TEAM"]      # the default first, once


def test_an_alert_takes_its_own_prefix_or_the_default():
    assert compose(_a(), EV, "trigger").subject == "[WARNING] [Heimdall] Price outside limits: TRIGGERED"
    assert compose(_a(), EV, "trigger", prefix="COVERAGE").subject.startswith("[WARNING] [COVERAGE] Price")
    mine = _a(prefix="[APAC QUANT TEAM]")
    assert mine.channels.prefix == "APAC QUANT TEAM"                       # brackets typed are dropped
    assert compose(mine, EV, "trigger", prefix="COVERAGE").subject == \
        "[WARNING] [APAC QUANT TEAM] Price outside limits: TRIGGERED"
    assert compose(mine, Evaluation("ok", False, False, "recovery", "ok"), "recovery").subject == \
        "[APAC QUANT TEAM] Price outside limits: RECOVERED"
    t = datetime(2026, 11, 12, tzinfo=timezone.utc)
    assert compose_all_clear(mine, checks=3, since=t, until=t, rows=0).subject == \
        "[APAC QUANT TEAM] Price outside limits: ALL CLEAR"
    # a subject of its own replaces the prefix and the name
    assert compose(_a(prefix="X", subject="Desk check"), EV, "trigger").subject == "[WARNING] Desk check: TRIGGERED"


def test_the_engine_uses_the_default_from_settings(store, engine, sent):
    seed(store)
    store.set_settings({"smtp": {"host": "", "prefix": "COVERAGE", "prefixes": ["COVERAGE", "Heimdall"]}})
    a = store.save_alert(Alert(name="any orders", enabled=True,
                               steps=[KdbStep(name="o", connection="demo_kdb", table="orders")],
                               condition=Condition(type="no_rows"), channels=Channels(email_to=["d@x.com"])))
    engine.resend_test(a)
    assert sent["email"][-1][1].subject == "[TEST] [WARNING] [COVERAGE] any orders: TRIGGERED"
