"""Start Sleipnir:  python -m heimdall.sleipnir [-c heimdall.toml] [--url http://huginn:8765]"""
from __future__ import annotations

import argparse
import logging

from heimdall import config as config_mod
from heimdall.client import HuginnClient
from heimdall.sleipnir.scheduler import Sleipnir


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="sleipnir", description="Heimdall's scheduler")
    p.add_argument("-c", "--config")
    p.add_argument("--url", help="Huginn's URL")
    p.add_argument("--log-level", default="info")
    a = p.parse_args(argv)
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load(a.config)
    client = HuginnClient(a.url or cfg.huginn_url, token=cfg.token, user="sleipnir")
    Sleipnir(client, cfg.sleipnir_tick_secs, cfg.sleipnir_run_grace_secs).run_forever()


if __name__ == "__main__":
    main()
