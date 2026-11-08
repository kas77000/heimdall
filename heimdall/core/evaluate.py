"""One evaluation of one alert: run the chain, judge the result, decide on notifying."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from heimdall.core import conditions
from heimdall.core.chain import Resources, StepOutcome, run_steps
from heimdall.core.fingerprint import result_fingerprint
from heimdall.core.models import Alert


@dataclass
class AlertState:
    """What the previous runs left behind that this run's decisions depend on."""
    last_status: str = ""                 # ok | triggered | error | ""
    last_triggered: bool = False
    last_notified_at: Optional[datetime] = None
    last_triggered_hash: str = ""
    consecutive_errors: int = 0


@dataclass
class Evaluation:
    status: str                           # ok | triggered | error
    condition_met: bool
    triggered: bool
    notify: Optional[str]                 # None | "trigger" | "error" | "recovery"
    message: str
    steps: list[StepOutcome] = field(default_factory=list)
    judged_step: str = ""
    judged: Optional[pd.DataFrame] = None
    matching: Optional[pd.DataFrame] = None
    result_hash: str = ""
    error: Optional[str] = None

    @property
    def row_count(self) -> Optional[int]:
        return None if self.judged is None else len(self.judged)


def evaluate_alert(alert: Alert, res: Resources, state: AlertState, now: datetime,
                   dry_run: bool = False) -> Evaluation:
    outcomes = run_steps(alert, res)
    failed = next((o for o in outcomes if o.error), None)
    if failed is not None or not outcomes:
        error = (f"step '{failed.name}' ({failed.kind}) failed: {failed.error}" if failed
                 else "the alert has no steps")
        errors = state.consecutive_errors + 1
        notify = None
        if (alert.channels.notify_on_error and not dry_run
                and errors == max(1, alert.channels.error_after)):
            notify = "error"
        return Evaluation("error", False, False, notify, f"{alert.name}: {error}",
                          outcomes, error=error)

    target = alert.condition.step or outcomes[-1].name
    judged = next((o for o in outcomes if o.name == target), None)
    if judged is None:
        error = f"the condition judges step '{target}', which did not run"
        return Evaluation("error", False, False, None, f"{alert.name}: {error}", outcomes, error=error)
    df = judged.df if judged.df is not None else pd.DataFrame()
    try:
        met = conditions.evaluate(alert.condition, df)
    except conditions.ConditionError as exc:
        error = f"condition: {exc}"
        return Evaluation("error", False, False, None, f"{alert.name}: {error}", outcomes,
                          judged_step=target, judged=df, error=error)

    digest = result_fingerprint(df)
    triggered = met
    if alert.rearm.mode == "on_change" and met:
        triggered = digest != state.last_triggered_hash
    notify = None
    if not dry_run:
        if conditions.should_notify(state.last_triggered, state.last_notified_at,
                                    triggered, alert.rearm, now):
            notify = "trigger"
        elif (not met and alert.channels.notify_on_recovery
              and (state.last_triggered or state.last_status == "triggered")):
            notify = "recovery"
        elif (not met and alert.channels.notify_on_recovery and alert.channels.notify_on_error
              and state.consecutive_errors >= max(1, alert.channels.error_after)):
            notify = "recovery"

    n = len(df)
    if triggered:
        message = f"{alert.name}: TRIGGERED — {conditions.describe(alert.condition, target).lower()} ({n} rows)"
    elif met:
        message = f"{alert.name}: still triggered, result unchanged ({n} rows)"
    else:
        message = f"{alert.name}: ok ({n} rows)"
    return Evaluation("triggered" if met else "ok", met, triggered, notify, message, outcomes,
                      judged_step=target, judged=df,
                      matching=conditions.matching_rows(alert.condition, df),
                      result_hash=digest)
