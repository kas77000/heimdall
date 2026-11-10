"""Every Bifrost page renders against a real Huginn (in-process) without raising."""
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
