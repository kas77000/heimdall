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
