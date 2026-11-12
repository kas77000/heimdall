"""Telling people: email over SMTP."""
from __future__ import annotations

import html
import re
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, Optional

import pandas as pd

from heimdall.core import conditions, cron
from heimdall.core.evaluate import Evaluation, severity_of
from heimdall.core.models import DEFAULT_PREFIX, ROW_TRIGGERS, Alert, clean_prefix

SEVERITY_COLOURS = {"critical": "#c62828", "warning": "#b26a00", "info": "#1f6fb2"}
TABLE_ROWS = 20                            # rows in the body of a mail not about rows
PREVIEW_ROWS = 50                          # rows previewed in the body when every row is attached
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024    # mail relays commonly refuse much bigger mails


@dataclass
class SmtpSettings:
    host: str = ""
    port: int = 25
    sender: str = "heimdall@localhost"

    @property
    def configured(self) -> bool:
        return bool(self.host)


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime: str = "text/csv"


@dataclass
class Message:
    subject: str
    text: str
    html: str
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class Delivery:
    channel: str            # email
    target: str
    ok: bool
    error: str = ""


def mail_prefix(settings: Optional[dict]) -> str:
    """The default subject prefix (Settings, Email)."""
    return clean_prefix(((settings or {}).get("smtp") or {}).get("prefix")) or DEFAULT_PREFIX


def prefixes(settings: Optional[dict]) -> list[str]:
    """Every subject prefix to choose from, the default first."""
    s = (settings or {}).get("smtp") or {}
    out: list[str] = []
    for p in [mail_prefix(settings)] + list(s.get("prefixes") or []):
        p = clean_prefix(p)
        if p and p.lower() not in {o.lower() for o in out}:
            out.append(p)
    return out


def subject_of(alert: Alert, prefix: str = DEFAULT_PREFIX) -> str:
    """"[<prefix>] <alert name>": the alert's own prefix, else the default; or its own subject."""
    return alert.channels.subject.strip() or f"[{alert.channels.prefix or prefix or DEFAULT_PREFIX}] {alert.name}"


def compose(alert: Alert, ev: Evaluation, kind: str, bifrost_url: str = "",
            prefix: str = DEFAULT_PREFIX) -> Message:
    label = {"trigger": "TRIGGERED", "error": "ERROR", "recovery": "RECOVERED"}[kind]
    subject = subject_of(alert, prefix) + f": {label}"
    severity = severity_of(alert, ev)
    if kind == "trigger":
        subject = f"[{severity.upper()}] " + subject            # sorts and filters in a mailbox
    lines = [f"{alert.name}: {label}"]
    if alert.description:
        lines.append(alert.description)
    if kind == "trigger":
        lines.append(conditions.describe(alert.condition, ev.judged_step) + ".")
        lines.append(f"{ev.row_count} row(s) in '{ev.judged_step}'.")
        if alert.severity_rules and getattr(ev, "severity_why", ""):
            lines.append(f"Severity {severity}: {ev.severity_why}.")
        if ev.moves:
            lines.append("Firing again because " + "; ".join(ev.moves) + ".")
    elif kind == "error":
        lines.append(f"The alert failed {alert.channels.error_after} time(s) in a row: {ev.error}")
    else:
        lines.append("The condition is no longer met." if ev.status == "ok" else ev.message)
    table = ev.matching if ev.matching is not None and not ev.matching.empty else ev.judged
    if kind != "trigger" or table is None or table.empty:
        table = None
    limit: Optional[int] = TABLE_ROWS
    attachments: list[Attachment] = []
    if table is not None and alert.condition.type in ROW_TRIGGERS:
        if alert.channels.rows_in == "attachment":
            att, note = _csv_attachment(alert, table)
            attachments.append(att)
            if len(table) > PREVIEW_ROWS:
                note += f" The first {PREVIEW_ROWS} are shown below."
            lines.append(note)
            limit = PREVIEW_ROWS
        else:
            limit = alert.channels.body_rows           # None = every row
    text = "\n".join(lines)
    if table is not None:
        head = table if limit is None else table.head(limit)
        text += "\n\n" + head.to_string(index=False)
        if len(table) > len(head):
            text += f"\n… {len(table) - len(head)} more row(s){' in the attachment' if attachments else ''}"
    if bifrost_url:
        text += f"\n\nOpen in Bifrost: {bifrost_url}"
    return Message(subject, text, _html(alert, ev, kind, label, lines, table, limit, bifrost_url,
                                        " in the attachment" if attachments else ""),
                   attachments)


def _csv_attachment(alert: Alert, table: pd.DataFrame) -> tuple[Attachment, str]:
    """Every row as CSV (UTF-8 with a BOM, so Excel shows accents), and a line for the mail."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", alert.name).strip("_")[:60] or "alert"
    filename = f"{name}_{stamp}.csv"
    data = table.to_csv(index=False).encode("utf-8-sig")
    if len(data) <= MAX_ATTACHMENT_BYTES:
        return Attachment(filename, data), f"All {len(table)} row(s) are attached as {filename}."
    # Too big for a mail: keep as many leading rows as fit.
    keep = max(1, int(len(table) * MAX_ATTACHMENT_BYTES / len(data) * 0.95))
    data = table.head(keep).to_csv(index=False).encode("utf-8-sig")
    while len(data) > MAX_ATTACHMENT_BYTES and keep > 1:
        keep = max(1, int(keep * 0.9))
        data = table.head(keep).to_csv(index=False).encode("utf-8-sig")
    mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
    return (Attachment(filename, data),
            f"The first {keep} of {len(table)} row(s) are attached as {filename} "
            f"(a mail attachment is limited to {mb} MB); every row is in Bifrost.")


def _html(alert, ev, kind, label, lines, table: Optional[pd.DataFrame], limit: Optional[int], url,
          where_rest: str = "") -> str:
    severity = severity_of(alert, ev)
    colour = {"trigger": SEVERITY_COLOURS.get(severity, "#c62828"), "error": "#ef6c00", "recovery": "#2e7d32"}[kind]
    if kind == "trigger":
        label = f"{label} · {severity.upper()}"
    parts = [f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1d2433">',
             f'<div style="border-left:4px solid {colour};padding:4px 12px;margin-bottom:12px">',
             f'<div style="font-size:12px;letter-spacing:.08em;color:{colour};font-weight:600">{label}</div>',
             f'<div style="font-size:18px;font-weight:600">{html.escape(alert.name)}</div></div>']
    for line in lines[1:]:
        parts.append(f"<p style=\"margin:6px 0\">{html.escape(line)}</p>")
    if table is not None:
        head = table if limit is None else table.head(limit)
        cells = "".join(f'<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #ccd">{html.escape(str(c))}</th>'
                        for c in head.columns)
        body = "".join("<tr>" + "".join(
            f'<td style="padding:4px 10px;border-bottom:1px solid #eef;font-family:Consolas,monospace">{html.escape(str(v))}</td>'
            for v in row) + "</tr>" for row in head.itertuples(index=False))
        parts.append(f'<table style="border-collapse:collapse;margin-top:10px">'
                     f"<thead><tr>{cells}</tr></thead><tbody>{body}</tbody></table>")
        if len(table) > len(head):
            parts.append(f'<p style="color:#667">… {len(table) - len(head)} more row(s){where_rest}</p>')
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
                      rows: Optional[int], bifrost_url: str = "", prefix: str = DEFAULT_PREFIX) -> Message:
    ac = alert.channels.all_clear
    tz = cron.zone(alert.active_hours.tz)
    stamp = lambda dt: dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")   # noqa: E731
    values = {"alert": alert.name, "group": alert.group or "no group", "checks": checks,
              "since": stamp(since), "until": stamp(until),
              "rows": "-" if rows is None else rows}
    if ac.subject.strip():
        subject = " ".join(fill(ac.subject, values).split())
    else:
        subject = subject_of(alert, prefix) + ": ALL CLEAR"
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
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(msg.text, "plain", "utf-8"))
    body.attach(MIMEText(msg.html, "html", "utf-8"))
    if msg.attachments:
        mime = MIMEMultipart("mixed")
        mime.attach(body)
        for att in msg.attachments:
            part = MIMEBase(*att.mime.split("/", 1))
            part.set_payload(att.content)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=att.filename)
            mime.attach(part)
    else:
        mime = body
    mime["Subject"], mime["From"], mime["To"] = msg.subject, smtp.sender, ", ".join(to)
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
