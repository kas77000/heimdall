"""One Bifrost page, for AppTest: the view to render is in session_state['view']."""
import importlib

import streamlit as st

st.session_state.setdefault("_pages", {})
importlib.import_module(f"heimdall.bifrost.views.{st.session_state['view']}").render()
