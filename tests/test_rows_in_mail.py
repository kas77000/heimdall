"""A row-based alert's rows in its mail: in the body (how many), or as a CSV attachment."""
import email
import io
from email.header import decode_header, make_header

import pandas as pd
import pytest

from heimdall.core import notify
from heimdall.core.evaluate import Evaluation
from heimdall.core.models import Alert, Channels, Condition, KdbStep
from heimdall.core.notify import SmtpSettings, compose, send_email
from heimdall.demo import seed

ROWS = pd.DataFrame({"sym": [f"S{i}" for i in range(50)], "px": [float(i) for i in range(50)],
                     "desk": ["Équités"] * 50})


def _alert(cond="has_rows", **channels) -> Alert:
    return Alert(name="Big fills: desk/équités", steps=[KdbStep(name="o", connection="demo_kdb", table="orders")],
                 condition=Condition(type=cond, column="px", op=">", value=0),
                 channels=Channels(email_to=["desk@example.com"], **channels))


def _fired(table=ROWS) -> Evaluation:
    return Evaluation("triggered", True, True, "trigger", "fired", judged_step="o", judged=table)


def _rows_shown(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip().startswith("S"))


# -- in the body -----------------------------------------------------------------

def test_by_default_the_body_shows_20_rows_as_before():
    msg = compose(_alert(), _fired(), "trigger")
    assert _rows_shown(msg.text) == 20 and "30 more row(s)" in msg.text
    assert msg.html.count("<tr>") == 21 and not msg.attachments          # header row + 20


def test_the_body_shows_as_many_rows_as_asked():
    msg = compose(_alert(body_rows=5), _fired(), "trigger")
    assert _rows_shown(msg.text) == 5 and "45 more row(s)" in msg.text
    assert msg.html.count("<tr>") == 6


def test_zero_means_every_row_in_the_body():
    msg = compose(_alert(body_rows=0), _fired(), "trigger")
    assert _rows_shown(msg.text) == 50 and "more row(s)" not in msg.text
    assert msg.html.count("<tr>") == 51


# -- as an attachment ------------------------------------------------------------

def test_attachment_carries_every_row_and_the_body_none():
    msg = compose(_alert(rows_in="attachment"), _fired(), "trigger")
    assert _rows_shown(msg.text) == 0 and "<table" not in msg.html
    (att,) = msg.attachments
    assert att.filename.startswith("Big_fills_desk_qu") and att.filename.endswith(".csv")
    assert f"All 50 row(s) are attached as {att.filename}." in msg.text
    assert att.content.startswith(b"\xef\xbb\xbf")                       # BOM: Excel reads the accents
    back = pd.read_csv(io.BytesIO(att.content), encoding="utf-8-sig")
    assert len(back) == 50 and list(back.columns) == ["sym", "px", "desk"] and back.desk[0] == "Équités"


def test_matching_rows_are_what_gets_attached():
    ev = _fired()
    ev.matching = ROWS[ROWS.px > 44]
    (att,) = compose(_alert("any_row", rows_in="attachment"), ev, "trigger").attachments
    assert len(pd.read_csv(io.BytesIO(att.content), encoding="utf-8-sig")) == 5


def test_an_attachment_too_big_for_a_mail_keeps_the_first_rows(monkeypatch):
    monkeypatch.setattr(notify, "MAX_ATTACHMENT_BYTES", 300)
    msg = compose(_alert(rows_in="attachment"), _fired(), "trigger")
    (att,) = msg.attachments
    kept = len(pd.read_csv(io.BytesIO(att.content), encoding="utf-8-sig"))
    assert len(att.content) <= 300 and 0 < kept < 50
    assert f"The first {kept} of 50 row(s) are attached" in msg.text


# -- only row-based triggers, only firing mails ----------------------------------

@pytest.mark.parametrize("cond", ["no_rows", "aggregate"])
def test_other_triggers_keep_the_usual_mail(cond):
    msg = compose(_alert(cond, rows_in="attachment", body_rows=0), _fired(), "trigger")
    assert not msg.attachments and _rows_shown(msg.text) == 20


@pytest.mark.parametrize("kind", ["error", "recovery"])
def test_error_and_recovery_mails_carry_no_rows(kind):
    msg = compose(_alert(rows_in="attachment"), _fired(), kind)
    assert not msg.attachments and _rows_shown(msg.text) == 0


# -- what goes out over SMTP -----------------------------------------------------

class _FakeSmtp:
    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sendmail(self, sender, to, data):
        _FakeSmtp.sent.append(email.message_from_string(data))


@pytest.fixture
def smtp(monkeypatch):
    _FakeSmtp.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSmtp)
    return _FakeSmtp.sent


def test_the_csv_goes_out_as_a_real_attachment(smtp):
    msg = compose(_alert(rows_in="attachment"), _fired(), "trigger")
    send_email(SmtpSettings(host="relay"), ["desk@example.com"], msg)
    (m,) = smtp
    assert m.get_content_type() == "multipart/mixed"
    assert str(make_header(decode_header(m["Subject"]))) == msg.subject
    body, att = m.get_payload()
    assert body.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in body.get_payload()] == ["text/plain", "text/html"]
    assert att.get_content_type() == "text/csv" and att.get_filename() == msg.attachments[0].filename
    assert att.get_payload(decode=True) == msg.attachments[0].content


def test_a_mail_without_attachment_is_unchanged(smtp):
    send_email(SmtpSettings(host="relay"), ["desk@example.com"], compose(_alert(), _fired(), "trigger"))
    (m,) = smtp
    assert m.get_content_type() == "multipart/alternative"


def test_the_engine_sends_the_attachment_when_an_alert_fires(store, engine, sent):
    seed(store)
    a = store.save_alert(_alert(rows_in="attachment").model_copy(update={"interval_secs": 10, "enabled": True}))
    engine.run_now(a.id)
    (to, msg), = sent["email"]
    (att,) = msg.attachments
    assert to == ["desk@example.com"] and len(pd.read_csv(io.BytesIO(att.content), encoding="utf-8-sig")) > 0
