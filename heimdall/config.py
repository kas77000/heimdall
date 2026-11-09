"""Configuration: ``heimdall.toml`` beside the code (or $HEIMDALL_CONFIG), then
``HEIMDALL_*`` environment variables on top.

Secrets are never written in the file — only the *names* of the environment
variables that hold them (the API token, the SMTP password, relay tokens).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10: the same parser, as a package
    import tomli as tomllib


@dataclass
class Config:
    db_path: str = "data/heimdall.db"
    host: str = "127.0.0.1"
    port: int = 8765
    token_env: str = "HEIMDALL_TOKEN"
    workers: int = 4
    tick_secs: float = 1.0
    snapshot_rows: int = 200
    huginn_url: str = "http://127.0.0.1:8765"
    bifrost_url: str = "http://localhost:8501"
    sleipnir_tick_secs: float = 15.0
    sleipnir_run_grace_secs: int = 300

    @property
    def token(self) -> str:
        return os.environ.get(self.token_env, "") if self.token_env else ""


_ENV = {"HEIMDALL_DB": "db_path", "HEIMDALL_HOST": "host", "HEIMDALL_PORT": "port",
        "HEIMDALL_URL": "huginn_url", "HEIMDALL_BIFROST_URL": "bifrost_url",
        "HEIMDALL_WORKERS": "workers"}


def load(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    candidate = Path(path or os.environ.get("HEIMDALL_CONFIG", "heimdall.toml"))
    if candidate.is_file():
        data = tomllib.loads(candidate.read_text(encoding="utf-8"))
        flat = {}
        for section in ("muninn", "huginn", "sleipnir", "bifrost"):
            for k, v in (data.get(section) or {}).items():
                flat[{"muninn": {"path": "db_path"},
                      "sleipnir": {"tick_secs": "sleipnir_tick_secs", "url": "huginn_url",
                                   "run_grace_secs": "sleipnir_run_grace_secs"},
                      "bifrost": {"url": "bifrost_url"}}.get(section, {}).get(k, k)] = v
        for k, v in flat.items():
            if hasattr(cfg, k):
                setattr(cfg, k, type(getattr(cfg, k))(v))
    for env, attr in _ENV.items():
        if os.environ.get(env):
            setattr(cfg, attr, type(getattr(cfg, attr))(os.environ[env]))
    return cfg
