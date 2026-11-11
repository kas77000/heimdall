"""Every Bifrost page renders against a real Huginn (in-process) without raising."""
import json

import pytest
from streamlit.testing.v1 import AppTest

from heimdall.bifrost import common
from heimdall.demo import seed


@pytest.fixture
def wired(huginn, store, engine, monkeypatch):
    seed(store)
    for a in store.alerts():
        if a.steps and a.interval_secs:
            engine.run_now(a.id)
    monkeypatch.setattr(common, "huginn", lambda: huginn)
    return huginn


def _page(name):
    def script(view):
        import importlib

        import streamlit as st
        st.session_state.setdefault("_pages", {})
        importlib.import_module(f"heimdall.bifrost.views.{view}").render()
    return AppTest.from_function(script, args=(name,), default_timeout=30)


@pytest.mark.parametrize("view", ["overview", "alerts", "history", "schedules", "settings", "builder"])
def test_page_renders(wired, view):
    at = _page(view).run()
    assert not at.exception, at.exception
    assert not [e.value for e in at.error], [e.value for e in at.error]


def test_builder_test_run_and_sections(wired, store):
    limit = next(a for a in store.alerts() if a.name.startswith("Price outside"))
    at = _page("builder")
    at.session_state["builder_request"] = {"id": limit.id}
    at.run()
    assert not at.exception
    next(b for b in at.button if b.label == "Test run").click().run()
    assert not at.exception
    assert any("Test run" in m.value for m in at.markdown)
    for section in ("Trigger", "Notify", "Details", "Steps"):
        at.segmented_control[0].set_value(section).run()
        assert not at.exception, section


def test_history_sections(wired):
    at = _page("history").run()
    for view in ("Changes", "Clean up", "Runs"):
        at.segmented_control[0].set_value(view).run()
        assert not at.exception, view
        assert not [e.value for e in at.error]


def test_settings_sections(wired):
    at = _page("settings").run()
    for view in ("Scripts", "Email", "Import / export", "Connections"):
        at.segmented_control[0].set_value(view).run()
        assert not at.exception, view


def _button(at, label):
    return next(b for b in at.button if b.label == label)


def test_new_bpipe_connection_shows_only_bpipe_fields_and_can_be_tested(wired, monkeypatch):
    from heimdall.core import chain

    class Reachable:
        def __init__(self, host, port, app_name):
            self.host, self.port, self.app_name = host, port, app_name

        def ping(self):
            return {"pong": True, "server": f"{self.host}:{self.port}", "app": self.app_name}

    monkeypatch.setattr(chain, "BpipeSource", Reachable)
    at = _page("settings").run()
    at.segmented_control(key="conn_new_kind").set_value("bloomberg").run()
    labels = [t.label for t in at.text_input]
    assert "B-PIPE host" in labels and "Application name" in labels
    assert not any("relay" in l.lower() or "url" in l.lower() or "token" in l.lower() for l in labels)
    at.text_input[0].set_value("bpipe")
    next(t for t in at.text_input if t.label == "B-PIPE host").set_value("bpipe-01")
    next(t for t in at.text_input if t.label == "Application name").set_value("MYFIRM:HEIMDALL")
    _button(at, "Test").click().run()
    assert not at.exception
    assert any("bpipe-01:8194" in s.value and "MYFIRM:HEIMDALL" in s.value for s in at.success), \
        [e.value for e in at.error]


def test_kdb_connection_has_no_bloomberg_fields(wired):
    at = _page("settings").run()
    labels = [t.label for t in at.text_input]
    assert "KDB host" in labels and "Application name" not in labels


def test_existing_connection_can_be_edited(wired, store):
    at = _page("settings").run()
    at.selectbox(key="conn_pick").set_value("demo_kdb").run()
    assert not at.exception
    desc = next(t for t in at.text_input if t.label == "Description")
    desc.set_value("edited from Bifrost")
    _button(at, "Save").click().run()
    assert not at.exception
    assert store.connection("demo_kdb").description == "edited from Bifrost"


def test_export_choice_and_import_selection(wired):
    at = _page("settings").run()
    at.segmented_control[0].set_value("Import / export").run()
    assert not at.exception
    for what in ("connections", "alerts", "schedules", "scripts", "everything"):
        at.radio(key="export_what").set_value(what).run()
        assert not at.exception, what
    at.multiselect(key="import_what").set_value(["schedules"]).run()
    assert not at.exception and not at.error


def _open_builder(store, name):
    alert = next(a for a in store.alerts() if a.name.startswith(name))
    at = _page("builder")
    at.session_state["builder_request"] = {"id": alert.id}
    return alert, at.run()


def test_builder_active_hours_and_all_clear_are_saved(wired, store):
    alert, at = _open_builder(store, "Market-data feed")
    at.segmented_control[0].set_value("Active hours").run()
    assert not at.exception
    next(t for t in at.toggle if t.label == "Only run during set hours").set_value(True).run()
    assert not at.exception
    assert any("Runs 09:00-17:00 Mon-Fri (Europe/London)" in m.value for m in at.markdown)
    _button(at, "Add window").click().run()
    assert len([t for t in at.time_input if t.label.startswith("From")]) == 2

    at.segmented_control[0].set_value("Notify").run()
    at.text_area[0].set_value("desk@example.com")
    next(t for t in at.toggle if t.label == "Also mail when all clear").set_value(True).run()
    assert not at.exception
    at.selectbox(key=next(s.key for s in at.selectbox if s.label == "When")).set_value("4 hours").run()

    at.segmented_control[0].set_value("Details").run()
    group = next(s for s in at.selectbox if s.label == "Group")
    group.set_value("➕ New group…").run()
    next(t for t in at.text_input if t.label == "New group name").set_value("Market data").run()
    _button(at, "Save alert").click().run()
    assert not at.exception, at.exception

    saved = store.alert(alert.id)
    assert saved.active_hours.enabled and len(saved.active_hours.windows) == 2
    assert saved.channels.all_clear.enabled and saved.channels.all_clear.interval_secs == 14400
    assert saved.group == "Market data"


def test_builder_sends_pop_ups_to_some_desks(wired, store):
    alert, at = _open_builder(store, "Market-data feed")
    at.segmented_control[0].set_value("Notify").run()
    next(t for t in at.toggle if t.label == "Pop up on traders' screens (Gjallarhorn)").set_value(True).run()
    assert not at.exception, at.exception
    at.segmented_control(key=next(s.key for s in at.segmented_control if s.label == "Severity")).set_value("critical").run()
    next(m for m in at.multiselect if m.label == "For").set_value(["Equities", "jdoe"]).run()
    assert any("Stays on screen" in c.value for c in at.caption)
    _button(at, "Save alert").click().run()
    assert not at.exception, at.exception
    saved = store.alert(alert.id)
    g = saved.channels.gjallarhorn
    assert (g.enabled, saved.severity, g.audience) == (True, "critical", ["Equities", "jdoe"])


def test_builder_picks_a_sound_and_plays_it(wired, store, tmp_path):
    store.set_settings({"gjallarhorn": {"root": str(tmp_path / "gj")}})
    alert, at = _open_builder(store, "Market-data feed")
    at.segmented_control[0].set_value("Notify").run()
    sound = next(s for s in at.selectbox if s.label == "Sound")
    assert sound.value == ""                                   # the severity's horn
    assert at.get("audio"), "the default horn can be heard"
    sound.set_value("horn:critical").run()
    _button(at, "Save alert").click().run()
    assert not at.exception, at.exception
    assert store.alert(alert.id).sound == "horn:critical"


def test_settings_gjallarhorn(wired, store, tmp_path):
    at = _page("settings").run()
    at.segmented_control(key="set_view").set_value("Gjallarhorn").run()
    assert not at.exception, at.exception
    assert any("Set the shared folder first" in i.value for i in at.info)
    next(t for t in at.text_input if t.label == "Shared folder").set_value(str(tmp_path / "gj"))
    _button(at, "Save").click().run()
    assert not at.exception, at.exception
    assert store.get_setting("gjallarhorn")["root"] == str(tmp_path / "gj")
    _button(at, "Publish the app").click().run()
    assert (tmp_path / "gj" / "app" / "gjallarhorn.ps1").is_file()
    _button(at, "Send a warning test to every trader").click().run()
    assert not at.exception, at.exception
    (sent,) = (tmp_path / "gj" / "alerts").rglob("*.json")
    first = json.loads(sent.read_text(encoding="utf-8"))
    assert (first["severity"], first["audience"], first["sound"]) == ("warning", [], "horn:warning")

    # a critical test for some desks only
    at.segmented_control(key="gj_test_sev").set_value("critical").run()
    at.radio(key="gj_test_scope").set_value("chosen").run()
    assert _button(at, "Send a critical test to nobody yet").disabled
    at.multiselect(key="gj_test_to").set_value(["Equities", "jdoe"]).run()
    _button(at, "Send a critical test to Equities, jdoe").click().run()
    assert not at.exception, at.exception
    second = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "gj" / "alerts").rglob("*.json")
              if p != sent][0]
    assert (second["severity"], second["audience"], second["sound"]) == ("critical", ["Equities", "jdoe"], "horn:critical")
    assert any("install-gjallarhorn.ps1" in c.value for c in at.code)
    at.selectbox(key="gj_snd_info").set_value("none").run()
    _button(at, "Save sounds").click().run()
    assert not at.exception, at.exception
    g = store.get_setting("gjallarhorn")
    assert g["sounds"]["info"] == "none" and g["root"] == str(tmp_path / "gj")    # the folder is kept


def test_builder_saves_a_renotify_on_move_rule(wired, store):
    alert, at = _open_builder(store, "Market-data feed")
    at.segmented_control[0].set_value("Trigger").run()
    assert not at.exception, at.exception
    next(r for r in at.radio if r.label == "Notify").set_value("delta").run()
    assert not at.exception, at.exception
    at.selectbox(key=next(s.key for s in at.selectbox if s.label == "Watch")).set_value("rows").run()
    at.selectbox(key=next(s.key for s in at.selectbox if s.label == "Direction")).set_value("up").run()
    next(n for n in at.number_input if n.label == "At least").set_value(3.0).run()
    at.selectbox(key=next(s.key for s in at.selectbox if s.label == "Unit")).set_value("pct").run()
    assert any("rises by at least 3%" in c.value for c in at.caption)
    _button(at, "Save alert").click().run()
    assert not at.exception, at.exception
    r = store.alert(alert.id).rearm
    assert (r.mode, r.delta_measure, r.delta_direction, r.delta_amount, r.delta_unit) == \
        ("delta", "rows", "up", 3.0, "pct")


def test_overview_shows_groups_and_off_hours(wired, store):
    from heimdall.core.models import ActiveHours, Window
    first, second = store.alerts()[:2]
    store.save_alert(first.model_copy(update={"group": "Equities"}))
    store.save_alert(second.model_copy(update={"active_hours": ActiveHours(
        enabled=True, days=[], windows=[Window(start="00:00", end="00:01")], tz="UTC")}))
    at = _page("overview").run()
    assert not at.exception, at.exception
    labels = [e.label for e in at.expander]
    assert any("Equities" in l for l in labels) and any("Ungrouped" in l for l in labels)


def test_alerts_page_moves_and_manages_groups(wired, store):
    at = _page("alerts").run()
    assert not at.exception
    a = store.alerts()[0]
    store.save_alert(a.model_copy(update={"group": "Equities"}))
    at = _page("alerts").run()
    assert not at.exception
    next(t for t in at.text_input if t.key == "grp_name_Equities").set_value("Stocks").run()
    next(b for b in at.button if b.key == "grp_ren_Equities").click().run()
    assert not at.exception
    assert store.alert(a.id).group == "Stocks"


def test_builder_chooses_rows_in_the_body_or_attached(wired, store):
    alert, at = _open_builder(store, "Market-data feed")          # trigger: any row where …
    at.segmented_control[0].set_value("Notify").run()
    rows_in = next(r for r in at.radio if r.label == "Rows in the mail")
    assert rows_in.value == "body"
    every = next(x for x in at.checkbox if x.label == "All rows")
    assert every.value and not [n for n in at.number_input if n.label == "How many rows"]
    assert any("Every row is shown in the mail" in c.value for c in at.caption)
    every.set_value(False).run()
    how_many = next(n for n in at.number_input if n.label == "How many rows")
    assert how_many.value == 20
    how_many.set_value(30).run()
    assert any("first 30 row(s)" in c.value for c in at.caption)
    _button(at, "Save alert").click().run()
    assert not at.exception, at.exception
    assert (store.alert(alert.id).channels.rows_in, store.alert(alert.id).channels.body_rows) == ("body", 30)

    alert, at = _open_builder(store, "Market-data feed")
    at.segmented_control[0].set_value("Notify").run()
    next(r for r in at.radio if r.label == "Rows in the mail").set_value("attachment").run()
    assert any("first 50 rows are shown in the mail as a preview" in c.value for c in at.caption)
    assert not [n for n in at.number_input if n.label == "How many rows"]
    assert not [x for x in at.checkbox if x.label == "All rows"]
    _button(at, "Save alert").click().run()
    assert not at.exception, at.exception
    assert store.alert(alert.id).channels.rows_in == "attachment"


def test_builder_hides_the_rows_choice_for_triggers_not_about_rows(wired, store):
    from heimdall.core.models import Condition
    alert = next(a for a in store.alerts() if a.name.startswith("Market-data feed"))
    store.save_alert(alert.model_copy(update={"condition": Condition(type="aggregate", column="lag_secs")}))
    _, at = _open_builder(store, "Market-data feed")
    at.segmented_control[0].set_value("Notify").run()
    assert not at.exception, at.exception
    assert not [r for r in at.radio if r.label == "Rows in the mail"]
