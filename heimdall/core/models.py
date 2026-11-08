"""The shapes Heimdall stores, sends over the API and evaluates.

Pydantic models, so the API validates what Bifrost sends for free and an
imported bundle is checked by the same rules as one typed in by hand. A step is
a discriminated union on ``kind``: each kind carries only its own fields.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_identifier(value: str, what: str) -> str:
    value = (value or "").strip()
    if not NAME_RE.match(value):
        raise ValueError(f"{what} '{value}' must start with a letter or _ and contain "
                         "only letters, digits and _ (it is used in {{references}})")
    return value


# --------------------------------------------------------------------------
# Connections and the script registry
# --------------------------------------------------------------------------

class Connection(BaseModel):
    """A place a step can get data from.

    ``kdb``: host + port (host ``demo`` is the in-memory mock).
    ``bloomberg``: a BbgRelay URL (``demo`` is the in-process fake) and the name
    of the environment variable holding its token — never the token itself.
    """
    name: str
    kind: Literal["kdb", "bloomberg"] = "kdb"
    host: str = ""
    port: int = 0
    url: str = ""
    token_env: str = ""
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("a connection needs a name")
        return v


class ScriptParam(BaseModel):
    name: str
    required: bool = True
    default: str = ""
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _check_identifier(v, "parameter")


class Script(BaseModel):
    """A command an administrator allows alerts to run.

    ``argv`` is the command as separate arguments, with ``{param}`` placeholders
    filled from the step. It never passes through a shell.
    """
    name: str
    argv: list[str]
    params: list[ScriptParam] = Field(default_factory=list)
    workdir: str = ""
    timeout_secs: int = 60
    output: Literal["json", "csv", "value", "exitcode"] = "json"
    fail_on_nonzero: bool = True
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _check_identifier(v, "script name")

    @field_validator("argv")
    @classmethod
    def _argv(cls, v: list[str]) -> list[str]:
        v = [a for a in (v or []) if a.strip() != ""]
        if not v:
            raise ValueError("a script needs a command")
        return v


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

VALUE_TYPES = ("symbol", "number", "string", "date", "time", "expression")
FILTER_OPS = ("=", "<>", "<", "<=", ">", ">=", "in", "like")


class Filter(BaseModel):
    column: str
    op: Literal["=", "<>", "<", "<=", ">", ">=", "in", "like"] = "="
    value: Union[str, float, int, list] = ""
    value_type: Literal["symbol", "number", "string", "date", "time", "expression"] = "symbol"
    negated: bool = False


class _StepBase(BaseModel):
    name: str
    note: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _check_identifier(v, "step name")


class KdbStep(_StepBase):
    kind: Literal["kdb"] = "kdb"
    connection: str = ""
    mode: Literal["guided", "raw"] = "guided"
    table: str = ""
    filters: list[Filter] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)   # guided: empty = all
    raw_q: str = ""


class BloombergStep(_StepBase):
    kind: Literal["bloomberg"] = "bloomberg"
    connection: str = ""
    operation: Literal["bdp", "snap"] = "bdp"
    # One security per line; a line holding {{step.column}} expands once per value.
    securities: str = ""
    fields: list[str] = Field(default_factory=list)
    overrides: dict[str, str] = Field(default_factory=dict)


class ScriptStep(_StepBase):
    kind: Literal["script"] = "script"
    script: str = ""
    args: dict[str, str] = Field(default_factory=dict)
    stdin_step: str = ""            # an earlier step whose table goes to stdin as JSON


class Derive(BaseModel):
    column: str
    expr: str


class ComputeStep(_StepBase):
    kind: Literal["compute"] = "compute"
    source: str = ""                 # the earlier step this starts from
    join_step: str = ""              # optional second table
    join_how: Literal["inner", "left", "outer"] = "inner"
    left_on: list[str] = Field(default_factory=list)
    right_on: list[str] = Field(default_factory=list)
    derive: list[Derive] = Field(default_factory=list)
    where: str = ""                  # keep rows where this is true
    keep: list[str] = Field(default_factory=list)   # empty = all


Step = Annotated[Union[KdbStep, BloombergStep, ScriptStep, ComputeStep],
                 Field(discriminator="kind")]

STEP_KINDS = {"kdb": KdbStep, "bloomberg": BloombergStep,
              "script": ScriptStep, "compute": ComputeStep}


# --------------------------------------------------------------------------
# Trigger, re-arm, channels, alert
# --------------------------------------------------------------------------

COMPARE_OPS = ("=", "<>", "<", "<=", ">", ">=")
CONDITION_TYPES = ("has_rows", "no_rows", "row_count", "any_row", "all_rows", "aggregate")
AGGREGATES = ("min", "max", "avg", "sum", "count")


class Condition(BaseModel):
    type: Literal["has_rows", "no_rows", "row_count", "any_row", "all_rows",
                  "aggregate"] = "has_rows"
    step: str = ""                   # empty = the last step
    column: str = ""
    op: Literal["=", "<>", "<", "<=", ">", ">="] = ">"
    value: Union[float, int, str] = 0
    agg: Literal["min", "max", "avg", "sum", "count"] = "max"


class Rearm(BaseModel):
    mode: Literal["every_run", "transition", "cooldown", "on_change"] = "transition"
    cooldown_secs: int = 900


class Channels(BaseModel):
    email_to: list[str] = Field(default_factory=list)
    subject: str = ""                # empty = "[Heimdall] <alert name>"
    notify_on_error: bool = False
    error_after: int = 3             # consecutive errors before an error notifies
    notify_on_recovery: bool = False


class Alert(BaseModel):
    id: Optional[int] = None
    name: str
    description: str = ""
    owner: str = ""
    tags: list[str] = Field(default_factory=list)
    enabled: bool = False
    interval_secs: int = 60          # 0 = only when scheduled or run by hand
    steps: list[Step] = Field(default_factory=list)
    condition: Condition = Field(default_factory=Condition)
    rearm: Rearm = Field(default_factory=Rearm)
    channels: Channels = Field(default_factory=Channels)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("an alert needs a name")
        return v

    @field_validator("interval_secs")
    @classmethod
    def _interval(cls, v: int) -> int:
        if v < 0:
            raise ValueError("the interval cannot be negative")
        if 0 < v < 5:
            raise ValueError("the shortest interval is 5 seconds (0 = schedule only)")
        return v


# --------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------

class Schedule(BaseModel):
    id: Optional[int] = None
    alert_id: int
    action: Literal["enable", "disable", "run"]
    cron: str                        # five-field cron, read in ``tz``
    tz: str = "UTC"
    active: bool = True
    note: str = ""
    last_fired_at: Optional[str] = None   # ISO UTC, maintained by Sleipnir
