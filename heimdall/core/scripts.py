"""Running a registered script and reading what it printed.

The command is the registry's ``argv`` with ``{param}`` placeholders filled.
A placeholder may sit inside a larger argument (``--date={date}``), but every
argument stays one argument: there is no shell, so a value holding spaces,
quotes or ``&`` is just text.

Output formats:

* ``json``  — a list of objects (one row each), an object (one row), or a
  single value (one cell, column ``value``).
* ``csv``   — a header line, then rows.
* ``value`` — the trimmed stdout as one cell, a number when it reads as one.
* ``exitcode`` — only the exit code matters.

Every table carries an ``exit_code`` column so a condition can test it.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass

import pandas as pd

from heimdall.core.models import Script

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
MAX_OUTPUT = 5_000_000   # characters of stdout we are willing to parse


class ScriptError(RuntimeError):
    pass


@dataclass
class ScriptRun:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration: float


def resolve_params(script: Script, given: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    declared = {p.name: p for p in script.params}
    unknown = sorted(set(given) - set(declared))
    if unknown:
        raise ScriptError(f"script '{script.name}' has no parameter(s): {', '.join(unknown)}")
    for p in script.params:
        v = given.get(p.name, "")
        if v == "" and p.default != "":
            v = p.default
        if v == "" and p.required:
            raise ScriptError(f"script '{script.name}' needs a value for '{p.name}'")
        values[p.name] = v
    return values


def build_argv(script: Script, values: dict[str, str]) -> list[str]:
    def fill(arg: str) -> str:
        def sub(m):
            name = m.group(1)
            if name not in values:
                raise ScriptError(f"'{{{name}}}' in the command is not a declared parameter")
            return values[name]
        return _PLACEHOLDER.sub(sub, arg)

    argv = [fill(a) for a in script.argv]
    # An optional parameter left empty, standing alone, drops out rather than
    # being passed as an empty argument.
    return [a for a, raw in zip(argv, script.argv) if not (a == "" and _PLACEHOLDER.fullmatch(raw))]


def run(script: Script, values: dict[str, str], stdin: str | None = None) -> ScriptRun:
    argv = build_argv(script, values)
    cwd = script.workdir or None
    if cwd and not os.path.isdir(cwd):
        raise ScriptError(f"working directory does not exist: {cwd}")
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                              cwd=cwd, timeout=script.timeout_secs, shell=False,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise ScriptError(f"cannot start '{argv[0]}': not found") from None
    except subprocess.TimeoutExpired:
        raise ScriptError(f"script '{script.name}' took longer than {script.timeout_secs}s and was stopped") from None
    return ScriptRun(argv, proc.returncode, proc.stdout or "", proc.stderr or "",
                     time.monotonic() - started)


def _number(text: str):
    try:
        f = float(text)
    except ValueError:
        return text
    return int(f) if f.is_integer() and re.fullmatch(r"[+-]?\d+", text.strip()) else f


def parse_output(script: Script, result: ScriptRun) -> pd.DataFrame:
    if script.fail_on_nonzero and script.output != "exitcode" and result.exit_code != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        raise ScriptError(f"script '{script.name}' exited with {result.exit_code}"
                          + (": " + " | ".join(tail) if tail else ""))
    text = result.stdout
    if len(text) > MAX_OUTPUT:
        raise ScriptError(f"script '{script.name}' printed more than {MAX_OUTPUT:,} characters")
    if script.output == "exitcode":
        df = pd.DataFrame({"value": [result.exit_code]})
    elif script.output == "value":
        df = pd.DataFrame({"value": [_number(text.strip())]})
    elif script.output == "csv":
        if not text.strip():
            df = pd.DataFrame()
        else:
            try:
                df = pd.read_csv(io.StringIO(text))
            except Exception as exc:  # noqa: BLE001
                raise ScriptError(f"script '{script.name}' did not print valid CSV: {exc}") from None
    else:
        stripped = text.strip()
        if not stripped:
            df = pd.DataFrame()
        else:
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ScriptError(f"script '{script.name}' did not print valid JSON: {exc.msg} "
                                  f"(line {exc.lineno})") from None
            if isinstance(data, list):
                if data and not all(isinstance(r, dict) for r in data):
                    df = pd.DataFrame({"value": data})
                else:
                    df = pd.DataFrame(data)
            elif isinstance(data, dict):
                df = pd.DataFrame([data])
            else:
                df = pd.DataFrame({"value": [data]})
    df = df.copy()
    df["exit_code"] = result.exit_code
    return df
