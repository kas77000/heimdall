"""Telling people: email over SMTP."""
from __future__ import annotations

import html
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, Optional

import pandas as pd

from heimdall.core import conditions, cron
from heimdall.core.evaluate import Evaluation
from heimdall.core.models import Alert

TABLE_ROWS = 20


@dataclass
class SmtpSettings:
    host: str = ""
    port: int = 25
    sender: str = "heimdall@localhost"

    @property
    def configured(self) -> bool:
        return bool(self.host)


@dataclass
class Message:
    subject: str
    text: str
    html: str


@dataclass
class Delivery:
    channel: str            # email
    target: str
    ok: bool
    error: str = ""


def compose(alert: Alert, ev: Evaluation, kind: str, bifrost_url: str = "") -> Message:
    label = {"trigger": "TRIGGERED", "error": "ERROR", "recovery": "RECOVERED"}[kind]
    subject = (alert.channels.subject.strip() or f"[Heimdall] {alert.name}") + f": {label}"
    lines = [f"{alert.name}: {label}"]
    if alert.description:
        lines.append(alert.description)
    if kind == "trigger":
        lines.append(conditions.describe(alert.condition, ev.judged_step) + ".")
        lines.append(f"{ev.row_count} row(s) in '{ev.judged_step}'.")
    elif kind == "error":
        lines.append(f"The alert failed {alert.channels.error_after} time(s) in a row: {ev.error}")
    else:
        lines.append("The condition is no longer met." if ev.status == "ok" else ev.message)
    table = ev.matching if ev.matching is not None and not ev.matching.empty else ev.judged
    text = "\n".join(lines)
    if kind == "trigger" and table is not None and not table.empty:
        text += "\n\n" + table.head(TABLE_ROWS).to_string(index=False)
        if len(table) > TABLE_ROWS:
            text += f"\n… {len(table) - TABLE_ROWS} more row(s)"
    if bifrost_url:
        text += f"\n\nOpen in Bifrost: {bifrost_url}"
    return Message(subject, text, _html(alert, ev, kind, label, lines, table, bifrost_url))


def _html(alert, ev, kind, label, lines, table: Optional[pd.DataFrame], url) -> str:
    colour = {"trigger": "#c62828", "error": "#ef6c00", "recovery": "#2e7d32"}[kind]
    parts = [f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1d2433">',
             f'<div style="border-left:4px solid {colour};padding:4px 12px;margin-bottom:12px">',
             f'<div style="font-size:12px;letter-spacing:.08em;color:{colour};font-weight:600">{label}</div>',
             f'<div style="font-size:18px;font-weight:600">{html.escape(alert.name)}</div></div>']
    for line in lines[1:]:
        parts.append(f"<p style=\"margin:6px 0\">{html.escape(line)}</p>")
    if kind == "trigger" and table is not None and not table.empty:
        head = table.head(TABLE_ROWS)
        cells = "".join(f'<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #ccd">{html.escape(str(c))}</th>'
                        for c in head.columns)
        body = "".join("<tr>" + "".join(
            f'<td style="padding:4px 10px;border-bottom:1px solid #eef;font-family:Consolas,monospace">{html.escape(str(v))}</td>'
            for v in row) + "</tr>" for row in head.itertuples(index=False))
        parts.append(f'<table style="border-collapse:collapse;margin-top:10px">'
                     f"<thead><tr>{cells}</tr></thead><tbody>{body}</tbody></table>")
        if len(table) > TABLE_ROWS:
            parts.append(f'<p style="color:#667">… {len(table) - TABLE_ROWS} more row(s)</p>')
    if url:
        parts.append(f'<p style="margin-top:14px"><a href="{html.escape(url)}">Open in Bifrost</a></p>')
    parts.append("</div>")
    return "".join(parts)


# -- all clear ------------------------------------------------------------------

ALL_CLEAR_FIELDS = ("alert", "group", "checks", "since", "until", "rows")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def fill(template: str, values: dict) -> str:
    """``{alert}`` and the like filled in; anything else is left exactly as typed."""
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
                            template)


def compose_all_clear(alert: Alert, *, checks: int, since: datetime, until: datetime,
                      rows: Optional[int], bifrost_url: str = "") -> Message:
    ac = alert.channels.all_clear
    tz = cron.zone(alert.active_hours.tz)
    stamp = lambda dt: dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")   # noqa: E731
    values = {"alert": alert.name, "group": alert.group or "no group", "checks": checks,
              "since": stamp(since), "until": stamp(until),
              "rows": "-" if rows is None else rows}
    if ac.subject.strip():
        subject = " ".join(fill(ac.subject, values).split())
    else:
        subject = (alert.channels.subject.strip() or f"[Heimdall] {alert.name}") + ": ALL CLEAR"
    if ac.body.strip():
        body = fill(ac.body, values)
    elif ac.mode == "every_check":
        body = fill("{alert} did not fire at {until}.\nRows returned: {rows}.", values)
    else:
        body = fill("{alert} did not fire between {since} and {until}.\n"
                    "Clean checks: {checks}. Rows in the last check: {rows}.", values)
    body += f"\nTimes are {alert.active_hours.tz}."
    text = body + (f"\n\nOpen in Bifrost: {bifrost_url}" if bifrost_url else "")
    colour = "#2e7d32"
    parts = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1d2433">',
             f'<div style="border-left:4px solid {colour};padding:4px 12px;margin-bottom:12px">',
             f'<div style="font-size:12px;letter-spacing:.08em;color:{colour};font-weight:600">ALL CLEAR</div>',
             f'<div style="font-size:18px;font-weight:600">{html.escape(alert.name)}</div></div>']
    parts += [f'<p style="margin:6px 0">{html.escape(line)}</p>' for line in body.splitlines() if line.strip()]
    if bifrost_url:
        parts.append(f'<p style="margin-top:14px"><a href="{html.escape(bifrost_url)}">Open in Bifrost</a></p>')
    parts.append("</div>")
    return Message(subject, text, "".join(parts))


def send_email(smtp: SmtpSettings, to: list[str], msg: Message) -> None:
    if not smtp.configured:
        raise RuntimeError("SMTP is not configured (Settings → Email)")
    mime = MIMEMultipart("alternative")
    mime["Subject"], mime["From"], mime["To"] = msg.subject, smtp.sender, ", ".join(to)
    mime.attach(MIMEText(msg.text, "plain", "utf-8"))
    mime.attach(MIMEText(msg.html, "html", "utf-8"))
    # The company mail relay: no login and no TLS, as KdbMonitor sends.
    with smtplib.SMTP(smtp.host, smtp.port, timeout=30) as s:
        s.sendmail(smtp.sender, to, mime.as_string())


def deliver(alert: Alert, msg: Message, smtp: SmtpSettings,
            email_fn: Callable[[SmtpSettings, list[str], Message], None] = send_email) -> list[Delivery]:
    out: list[Delivery] = []
    to = [a.strip() for a in alert.channels.email_to if a.strip()]
    if to:
        try:
            email_fn(smtp, to, msg)
            out.append(Delivery("email", ", ".join(to), True))
        except Exception as exc:  # noqa: BLE001
            out.append(Delivery("email", ", ".join(to), False, str(exc)))
    return out
