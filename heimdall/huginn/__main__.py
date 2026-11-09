"""Start Huginn: the engine thread, the HTTP API (for Bifrost) and the local pipe
(for Sleipnir, see heimdall.pipe) in one process.

    python -m heimdall.huginn [-c heimdall.toml] [--host H] [--port P] [--demo]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn

from heimdall import config as config_mod
from heimdall import pipe
from heimdall.huginn.api import create_app
from heimdall.huginn.engine import Engine
from heimdall.muninn.store import Muninn


def build(cfg):
    store = Muninn(cfg.db_path)
    engine = Engine(store, cfg)

    @asynccontextmanager
    async def lifespan(app):
        engine.start()
        sleipnir_pipe = pipe.PipeServer(app, cfg.port, asyncio.get_running_loop())
        sleipnir_pipe.start()
        try:
            yield
        finally:
            sleipnir_pipe.stop()
            engine.stop(wait=False)

    return create_app(store, engine, cfg, lifespan=lifespan), store, engine


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="huginn", description="Heimdall's alerting service")
    p.add_argument("-c", "--config")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--demo", action="store_true", help="load the demo connections, scripts and alerts if missing")
    p.add_argument("--log-level", default="info")
    a = p.parse_args(argv)
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx only carries Sleipnir's pipe calls into the API here; its "HTTP Request" lines would mislead
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = config_mod.load(a.config)
    if a.host:
        cfg.host = a.host
    if a.port:
        cfg.port = a.port
    app, store, _ = build(cfg)
    if a.demo:
        from heimdall.demo import seed
        print(seed(store))
    if not cfg.token and cfg.host not in ("127.0.0.1", "localhost"):
        logging.warning("Huginn is listening on %s with no token: set %s", cfg.host, cfg.token_env)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=a.log_level)


if __name__ == "__main__":
    main()
