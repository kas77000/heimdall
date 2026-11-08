"""Stopping a process together with everything it started.

On Windows a venv's python.exe is a launcher that starts the real interpreter
as a child, and ``heimdall bifrost`` starts Streamlit as a child too, so
terminating the process we hold leaves the one doing the work running.
``stop_tree`` ends the whole tree.
"""
from __future__ import annotations

import os
import signal
import subprocess


def stop_tree(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)


def popen_group_kwargs() -> dict:
    """Start a child in its own process group, so ``stop_tree`` can reach its children."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
