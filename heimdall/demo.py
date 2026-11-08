"""Demo connections, scripts and alerts, so Heimdall can be tried with no KDB or Bloomberg."""
from __future__ import annotations

import sys
from pathlib import Path

from heimdall.core.models import (Alert, BloombergStep, Channels, ComputeStep, Condition,
                                  Connection, Derive, Filter, KdbStep, Rearm, Schedule, Script,
                                  ScriptParam, ScriptStep)
from heimdall.muninn.store import Muninn

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "scripts"


def demo_connections() -> list[Connection]:
    return [Connection(name="demo_kdb", kind="kdb", host="demo", port=0,
                       description="In-memory mock: quote, orders, fills"),
            Connection(name="demo_bbg", kind="bloomberg", url="demo",
                       description="In-process fake Bloomberg (limits are ±10% of base price)")]


def demo_scripts() -> list[Script]:
    py = sys.executable
    return [
        Script(name="feed_lag", argv=[py, str(EXAMPLES / "feed_lag.py"), "--feeds", "{feeds}"],
               params=[ScriptParam(name="feeds", required=False, default="TSE,HKEX,ASX,SGX",
                                   description="comma-separated feed names")],
               output="json", timeout_secs=20, description="Lag of each market-data feed (JSON records)"),
        Script(name="order_desk", argv=[py, str(EXAMPLES / "order_desk.py")], output="json",
               timeout_secs=20, description="Adds the owning desk to orders read on stdin"),
        Script(name="count_value", argv=[py, str(EXAMPLES / "count_value.py"), "--base", "{base}"],
               params=[ScriptParam(name="base", required=False, default="10")], output="value",
               timeout_secs=20, description="Prints a single number"),
    ]


def demo_alerts() -> list[tuple[Alert, list[dict]]]:
    limit_breach = Alert(
        name="Price outside Bloomberg limits",
        description="Working orders whose stock trades beyond its limit-up / limit-down price.",
        tags=["demo", "limits"], interval_secs=30, enabled=True,
        steps=[
            KdbStep(name="orders", connection="demo_kdb", table="orders",
                    filters=[Filter(column="state", op="in", value=["working", "new"], value_type="symbol")]),
            KdbStep(name="quotes", connection="demo_kdb", mode="raw",
                    raw_q="select sym, last from quote where sym in {{orders.sym}}"),
            BloombergStep(name="limits", connection="demo_bbg", operation="bdp",
                          securities="{{quotes.sym}} US Equity", fields=["LIMIT_UP", "LIMIT_DOWN"]),
            ComputeStep(name="breaches", source="quotes", join_step="limits",
                        left_on=["sym"], right_on=["sym"],
                        derive=[Derive(column="side_hit", expr="'UP' if last >= LIMIT_UP else 'DOWN'")],
                        where="last >= LIMIT_UP or last <= LIMIT_DOWN",
                        keep=["sym", "security", "last", "LIMIT_UP", "LIMIT_DOWN", "side_hit"]),
        ],
        condition=Condition(type="has_rows"),
        rearm=Rearm(mode="on_change"),
        channels=Channels(notify_on_recovery=True))
    feed_lag = Alert(
        name="Market-data feed lagging",
        description="A feed is more than a minute behind.",
        tags=["demo", "scripts"], interval_secs=20, enabled=True,
        steps=[ScriptStep(name="feeds", script="feed_lag", args={"feeds": "TSE,HKEX,ASX,SGX"})],
        condition=Condition(type="any_row", column="lag_secs", op=">", value=60),
        rearm=Rearm(mode="cooldown", cooldown_secs=300))
    big_fills = Alert(
        name="Large fills by desk",
        description="Fills of 2,500 shares or more, with the desk that owns the order.",
        tags=["demo"], interval_secs=0, enabled=False,
        steps=[
            KdbStep(name="fills", connection="demo_kdb", table="fills",
                    filters=[Filter(column="fillQty", op=">=", value="2500", value_type="number")]),
            KdbStep(name="orders", connection="demo_kdb", mode="raw",
                    raw_q="select orderId, sym, algo from orders where sym in {{fills.sym}}"),
            ScriptStep(name="desks", script="order_desk", stdin_step="orders"),
        ],
        condition=Condition(type="row_count", op=">=", value=1),
        rearm=Rearm(mode="every_run"))
    schedules = [{"action": "run", "cron": "30 16 * * 1,2,3,4,5", "tz": "Europe/London",
                  "note": "the end-of-day check"}]
    hours = [{"action": "enable", "cron": "0 8 * * 1,2,3,4,5", "tz": "Europe/London", "note": "market open"},
             {"action": "disable", "cron": "30 17 * * 1,2,3,4,5", "tz": "Europe/London", "note": "after close"}]
    return [(limit_breach, []), (feed_lag, hours), (big_fills, schedules)]


def seed(store: Muninn) -> str:
    added = []
    for c in demo_connections():
        if store.connection(c.name) is None:
            store.save_connection(c); added.append(c.name)
    for s in demo_scripts():
        if store.script(s.name) is None:
            store.save_script(s); added.append(s.name)
    for alert, schedules in demo_alerts():
        if store.alert_by_name(alert.name) is None:
            saved = store.save_alert(alert)
            for sc in schedules:
                store.save_schedule(Schedule(alert_id=saved.id, **sc))
            added.append(alert.name)
    if added:
        store.log_event("demo", "demo loaded", detail=", ".join(added))
    return "demo: added " + (", ".join(added) if added else "nothing (already present)")
