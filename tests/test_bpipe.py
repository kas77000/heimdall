"""BpipeSource against a stand-in for Bloomberg's blpapi.

The real B-PIPE needs Bloomberg's network and an application name, so these
check what Heimdall itself is responsible for: the session it asks for (host,
port, application), the requests it builds, and how it reads the answers.
"""
import sys
import types

import pandas as pd
import pytest

from heimdall.core.bloomberg import BpipeSource
from heimdall.core.chain import Pool
from heimdall.core.models import Connection


# -- a small blpapi ------------------------------------------------------------------

class El:
    """An element: a scalar, a list of elements, or named children."""

    def __init__(self, value=None, children=None, items=None):
        self.value, self.children, self.items = value, children or {}, items

    def hasElement(self, name):
        return name in self.children

    def getElement(self, name):
        return self.children[name]

    def getElementAsString(self, name):
        return str(self.children[name].value)

    def getValue(self):
        return self.value

    def isNull(self):
        return self.value is None and not self.children and self.items is None

    def numValues(self):
        return len(self.items)

    def getValueAsElement(self, i):
        return self.items[i]

    def __str__(self):
        return str(self.value if self.value is not None else self.children)


class Msg(El):
    def __init__(self, kind, cids, **children):
        super().__init__(children={k: v if isinstance(v, El) else El(v) for k, v in children.items()})
        self.kind, self.cids = kind, cids

    def messageType(self):
        return self.kind

    def correlationIds(self):
        return self.cids


class Ev:
    def __init__(self, etype, msgs):
        self.etype, self.msgs = etype, msgs

    def eventType(self):
        return self.etype

    def __iter__(self):
        return iter(self.msgs)


class Cid:
    _next = 1000

    def __init__(self, value=None):
        if value is None:
            Cid._next += 1
            value = Cid._next
        self._v = value

    def value(self):
        return self._v

    def __eq__(self, other):
        return isinstance(other, Cid) and other._v == self._v

    def __hash__(self):
        return hash(self._v)


class Request:
    def __init__(self):
        self.securities, self.fields, self.overrides = [], [], []

    def append(self, name, value):
        getattr(self, name).append(value)

    def getElement(self, name):
        assert name == "overrides"
        req = self

        class Overrides:
            def appendElement(self):
                ov = {}
                req.overrides.append(ov)

                class O:
                    def setElement(self, k, v):
                        ov[k] = v
                return O()
        return Overrides()


class Service:
    def createRequest(self, name):
        assert name == "ReferenceDataRequest"
        return Request()


class Event:
    TIMEOUT, RESPONSE, PARTIAL_RESPONSE, SUBSCRIPTION_DATA, SUBSCRIPTION_STATUS = range(5)


class SubscriptionList:
    def __init__(self):
        self.entries = []

    def add(self, topic, fields, options, cid):
        self.entries.append((topic, list(fields), cid))


class Session:
    """Answers a ReferenceDataRequest from REF and subscriptions from LIVE."""
    REF = {"AAPL US Equity": {"PX_LAST": 101.5, "NAME": "APPLE INC"},
           "MSFT US Equity": {"PX_LAST": 402.0, "NAME": "MICROSOFT CORP"}}
    LIVE = {"AAPL US Equity": {"LAST_PRICE": 101.6, "BID": 101.55, "ASK": 101.65}}
    last = None

    def __init__(self, opts):
        self.opts, self.events, self.requests, self.stopped = opts, [], [], False
        self.start_ok = True
        Session.last = self

    def start(self):
        return self.start_ok

    def openService(self, name):
        return True

    def getService(self, name):
        return Service()

    def stop(self):
        self.stopped = True

    def tryNextEvent(self):
        return None

    def nextEvent(self, timeout_ms):
        return self.events.pop(0) if self.events else Ev(Event.TIMEOUT, [])

    def sendRequest(self, req):
        self.requests.append(req)
        cid = Cid()
        datas = []
        for sec in req.securities:
            if sec not in self.REF:
                datas.append(El(children={"security": El(sec), "securityError":
                                          El(children={"message": El("Unknown/Invalid security")})}))
                continue
            fd = {f: El(self.REF[sec][f]) for f in req.fields if f in self.REF[sec]}
            datas.append(El(children={"security": El(sec), "fieldData": El(children=fd)}))
        # one partial answer, then the final one, with an unrelated message in between
        self.events += [Ev(Event.PARTIAL_RESPONSE, [Msg("ReferenceDataResponse", [cid],
                                                        securityData=El(items=datas[:1]))]),
                        Ev(Event.SUBSCRIPTION_STATUS, [Msg("SessionConnectionUp", [])]),
                        Ev(Event.RESPONSE, [Msg("ReferenceDataResponse", [cid],
                                                securityData=El(items=datas[1:]))])]
        return cid

    def subscribe(self, subs):
        self.subscribed = subs
        for topic, fields, cid in subs.entries:
            if topic not in self.LIVE:
                self.events.append(Ev(Event.SUBSCRIPTION_STATUS, [Msg("SubscriptionFailure", [cid])]))
                continue
            vals = self.LIVE[topic]
            # the first tick carries only LAST_PRICE, the next the rest
            self.events.append(Ev(Event.SUBSCRIPTION_DATA, [Msg("MarketDataEvents", [cid],
                                                                LAST_PRICE=vals["LAST_PRICE"])]))
            self.events.append(Ev(Event.SUBSCRIPTION_DATA, [Msg("MarketDataEvents", [cid], **vals)]))

    def unsubscribe(self, subs):
        self.unsubscribed = subs


class SessionOptions:
    def setServerAddress(self, host, port, index):
        self.address = (host, port, index)

    def setSessionIdentityOptions(self, auth):
        self.auth = auth


class AuthOptions:
    @staticmethod
    def createWithApp(appName):
        return ("app", appName)


@pytest.fixture
def blpapi(monkeypatch):
    mod = types.ModuleType("blpapi")
    mod.Session, mod.SessionOptions, mod.AuthOptions = Session, SessionOptions, AuthOptions
    mod.Event, mod.SubscriptionList, mod.CorrelationId = Event, SubscriptionList, Cid
    monkeypatch.setitem(sys.modules, "blpapi", mod)
    return mod


# -- the tests -----------------------------------------------------------------------

def test_session_uses_host_port_and_app_name(blpapi):
    src = BpipeSource("bpipe-01", 8194, "MYFIRM:HEIMDALL")
    assert src.ping()["pong"]
    opts = Session.last.opts
    assert opts.address == ("bpipe-01", 8194, 0)
    assert opts.auth == ("app", "MYFIRM:HEIMDALL")


def test_bdp_reads_partial_and_final_answers(blpapi):
    src = BpipeSource("bpipe-01", 8194, "APP")
    df = src.fetch("bdp", ["AAPL US Equity", "MSFT US Equity"], ["PX_LAST", "NAME", "CRNCY"],
                   {"EQY_FUND_CRNCY": "USD"})
    assert list(df.columns) == ["security", "PX_LAST", "NAME", "CRNCY"]
    assert df.set_index("security").loc["MSFT US Equity", "PX_LAST"] == 402.0
    assert df.set_index("security").loc["AAPL US Equity", "NAME"] == "APPLE INC"
    assert df["CRNCY"].isna().all()                     # not returned: a gap, not an error
    req = Session.last.requests[0]
    assert req.overrides == [{"fieldId": "EQY_FUND_CRNCY", "value": "USD"}]


def test_bdp_keeps_good_securities_beside_a_bad_one(blpapi):
    df = BpipeSource("h", 1, "APP").fetch("bdp", ["AAPL US Equity", "NOPE US Equity"], ["PX_LAST"])
    assert df.set_index("security").loc["AAPL US Equity", "PX_LAST"] == 101.5
    assert pd.isna(df.set_index("security").loc["NOPE US Equity", "PX_LAST"])


def test_bdp_fails_when_every_security_is_bad(blpapi):
    with pytest.raises(RuntimeError, match="Unknown/Invalid security"):
        BpipeSource("h", 1, "APP").fetch("bdp", ["NOPE US Equity"], ["PX_LAST"])


def test_snap_waits_for_every_field_then_unsubscribes(blpapi):
    src = BpipeSource("h", 1, "APP", snap_wait=2)
    df = src.fetch("snap", ["AAPL US Equity"], [])
    assert df.iloc[0].to_dict() == {"security": "AAPL US Equity", "LAST_PRICE": 101.6,
                                    "BID": 101.55, "ASK": 101.65}
    assert Session.last.unsubscribed is Session.last.subscribed


def test_snap_fails_when_no_subscription_works(blpapi):
    with pytest.raises(RuntimeError, match="SubscriptionFailure"):
        BpipeSource("h", 1, "APP", snap_wait=1).fetch("snap", ["NOPE US Equity"], ["LAST_PRICE"])


def test_a_session_that_will_not_start_says_where(blpapi, monkeypatch):
    monkeypatch.setattr(Session, "start", lambda self: False)
    with pytest.raises(RuntimeError, match="bpipe-01:8194 as 'APP'"):
        BpipeSource("bpipe-01", 8194, "APP").ping()


def test_missing_app_name_is_refused_before_connecting(blpapi):
    with pytest.raises(RuntimeError, match="application name"):
        BpipeSource("bpipe-01", 8194, "").ping()


def test_old_bbgrelay_connections_still_load():
    demo = Connection.model_validate({"name": "demo_bbg", "kind": "bloomberg", "url": "demo"})
    assert demo.host == "demo"
    old = Connection.model_validate({"name": "bbg", "kind": "bloomberg", "url": "ws://bbgbox:8484",
                                     "token_env": "BBGRELAY_TOKEN"})
    assert old.host == "" and old.app_name == ""


def test_pool_builds_bpipe_from_the_connection_and_closes_it_when_edited(blpapi):
    conns = {"bbg": Connection(name="bbg", kind="bloomberg", host="bpipe-01", port=8194, app_name="APP")}
    pool = Pool(conns.get, lambda n: None)
    first = pool.bloomberg("bbg")
    assert isinstance(first, BpipeSource) and (first.host, first.port, first.app_name) == ("bpipe-01", 8194, "APP")
    first.ping()
    session = Session.last
    conns["bbg"] = conns["bbg"].model_copy(update={"port": 8196})
    second = pool.bloomberg("bbg")
    assert second is not first and second.port == 8196
    assert session.stopped
