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
