import pytest
from fastapi.testclient import TestClient

from heimdall.client import HuginnClient
from heimdall.config import Config
from heimdall.core.chain import Pool
from heimdall.huginn.api import create_app
from heimdall.huginn.engine import Engine
from heimdall.muninn.store import Muninn


@pytest.fixture
def store():
    s = Muninn(":memory:")
    yield s
    s.close()


@pytest.fixture
def sent():
    return {"email": []}


@pytest.fixture
def engine(store, sent):
    cfg = Config(db_path=":memory:", workers=2, bifrost_url="http://bifrost")
    e = Engine(store, cfg, pool=Pool(store.connection, store.script, kdb_factory=lambda h, p: None),
               email_fn=lambda smtp, to, msg: sent["email"].append((to, msg)))
    yield e
    e.stop(wait=False)


@pytest.fixture
def api(store, engine):
    return TestClient(create_app(store, engine, engine.cfg))


@pytest.fixture
def huginn(api):
    c = HuginnClient("http://testserver", user="tester")
    c._http = api
    return c
