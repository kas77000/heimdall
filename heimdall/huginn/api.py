"""Huginn's HTTP API — the only way in to Muninn for Bifrost and Sleipnir."""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from heimdall import __version__
from heimdall.config import Config
from heimdall.core import cron, hours
from heimdall.core.chain import Pool, validate as validate_alert
from heimdall.core.conditions import describe
from heimdall.core.kdb import list_columns, list_tables
from heimdall.core.models import Alert, Connection, Schedule, Script
from heimdall.huginn.engine import Engine
from heimdall.muninn.store import Conflict, Muninn, NotFound, parse_iso, utcnow

BUNDLE_VERSION = 1
BUNDLE_PARTS = ("connections", "scripts", "alerts", "schedules")


class PurgeRequest(BaseModel):
    older_than_days: Optional[float] = None
    keep_last: Optional[int] = None
    alert_id: Optional[int] = None
    everything: bool = False
    keep_unacknowledged: bool = False
    dry_run: bool = False


class AckRequest(BaseModel):
    run_ids: list[int] = Field(default_factory=list)
    alert_id: Optional[int] = None
    all: bool = False


class FiredRequest(BaseModel):
    at: str
    outcome: Literal["applied", "unchanged", "skipped", "failed"] = "applied"
    detail: str = ""


class GroupRename(BaseModel):
    old: str
    new: str = ""


class GroupSwitch(BaseModel):
    group: str
    enabled: bool


class Bundle(BaseModel):
    version: int = BUNDLE_VERSION
    connections: list[Connection] = Field(default_factory=list)
    scripts: list[Script] = Field(default_factory=list)
    alerts: list[Alert] = Field(default_factory=list)
    schedules: list[dict] = Field(default_factory=list)   # {"alert": name, ...schedule}


def create_app(store: Muninn, engine: Engine, cfg: Config, lifespan=None) -> FastAPI:
    app = FastAPI(title="Huginn", version=__version__, lifespan=lifespan,
                  description="Heimdall's alerting service. Bifrost and Sleipnir talk to it here.")

    def auth(x_heimdall_token: str = Header(default="")) -> None:
        token = cfg.token
        if token and not hmac.compare_digest(token, x_heimdall_token or ""):
            raise HTTPException(401, "missing or wrong X-Heimdall-Token")

    def actor(x_heimdall_user: str = Header(default="")) -> str:
        return (x_heimdall_user or "bifrost").strip()[:64]

    @app.exception_handler(NotFound)
    async def _nf(_: Request, exc: NotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    async def _cf(_: Request, exc: Conflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    secured = [Depends(auth)]

    # -- health & status -----------------------------------------------------

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/status", dependencies=secured)
    def status():
        now = utcnow()
        states = store.states()
        inflight = engine.inflight()
        alerts = []
        for a in store.alerts():
            st = states.get(a.id) or {}
            nd = engine.next_due(a, st)
            unack = store.count_runs(alert_id=a.id, unacknowledged=True)
            alerts.append({
                "id": a.id, "name": a.name, "enabled": a.enabled, "interval_secs": a.interval_secs,
                "tags": a.tags, "owner": a.owner, "group": a.group, "running": a.id in inflight,
                "off_hours": a.enabled and not hours.is_active(a.active_hours, now),
                "active_hours": hours.describe(a.active_hours),
                "last_run_at": st.get("last_run_at"), "last_status": st.get("last_status"),
                "last_message": st.get("last_message"), "last_run_id": st.get("last_run_id"),
                "consecutive_errors": st.get("consecutive_errors", 0),
                "next_due_at": nd.isoformat(timespec="seconds") if nd else None,
                "unacknowledged": unack, "steps": len(a.steps),
                "kinds": sorted({s.kind for s in a.steps}),
                "condition": describe(a.condition, a.steps[-1].name if a.steps else ""),
            })
        return {"engine": {"running": engine.running, "paused": engine.paused,
                           "started_at": engine.started_at.isoformat(timespec="seconds") if engine.started_at else None,
                           "last_tick": engine.last_tick.isoformat(timespec="seconds") if engine.last_tick else None,
                           "workers": cfg.workers,
                           "sleipnir_seen": engine.sleipnir_seen.isoformat(timespec="seconds") if engine.sleipnir_seen else None},
                "now": now.isoformat(timespec="seconds"),
                "today": store.stats(now.replace(hour=0, minute=0, second=0, microsecond=0)),
                "alerts": alerts}

    @app.post("/engine/pause", dependencies=secured)
    def pause(who: str = Depends(actor)):
        engine.set_paused(True, who)
        return {"paused": True}

    @app.post("/engine/resume", dependencies=secured)
    def resume(who: str = Depends(actor)):
        engine.set_paused(False, who)
        return {"paused": False}

    # -- connections -----------------------------------------------------------

    @app.get("/connections", dependencies=secured)
    def get_connections():
        return [c.model_dump() for c in store.connections()]

    @app.put("/connections/{name}", dependencies=secured)
    def put_connection(name: str, conn: Connection, who: str = Depends(actor)):
        store.save_connection(conn, old_name=name)
        engine.pool.forget(name); engine.pool.forget(conn.name)
        store.log_event(who, "connection saved", detail=conn.name)
        return conn.model_dump()

    @app.delete("/connections/{name}", dependencies=secured)
    def delete_connection(name: str, who: str = Depends(actor)):
        users = [a.name for a in store.alerts()
                 if any(getattr(s, "connection", None) == name for s in a.steps)]
        if users:
            raise HTTPException(409, f"'{name}' is used by: {', '.join(users)}")
        store.delete_connection(name)
        engine.pool.forget(name)
        store.log_event(who, "connection deleted", detail=name)
        return {"deleted": name}

    def _try(conn: Connection, pool: Pool) -> dict:
        try:
            if conn.kind == "kdb":
                tables = list_tables(pool.kdb(conn.name))
                return {"ok": True, "detail": f"{len(tables)} table(s)", "tables": tables}
            src = pool.bloomberg(conn.name)
            pong = src.ping() if hasattr(src, "ping") else {"pong": True}
            if pong.get("fake"):
                return {"ok": True, "detail": "built-in fake Bloomberg"}
            return {"ok": bool(pong.get("pong", True)),
                    "detail": f"B-PIPE session started at {pong.get('server')} as '{pong.get('app')}'; "
                              "reference and market data services are open"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}

    @app.post("/connections/test", dependencies=secured)
    def test_unsaved_connection(conn: Connection):
        """Tries a connection as typed, before it is saved, then lets it go."""
        pool = engine.pool.for_one(conn)
        try:
            return _try(conn, pool)
        finally:
            pool.forget(conn.name)

    @app.post("/connections/{name}/test", dependencies=secured)
    def test_connection(name: str):
        conn = store.connection(name)
        if conn is None:
            raise NotFound(f"no connection called '{name}'")
        engine.pool.forget(name)
        return _try(conn, engine.pool)

    @app.get("/connections/{name}/tables", dependencies=secured)
    def tables(name: str):
        try:
            return list_tables(engine.pool.kdb(name))
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"could not list tables: {exc}")

    @app.get("/connections/{name}/columns", dependencies=secured)
    def columns(name: str, table: str):
        try:
            return list_columns(engine.pool.kdb(name), table)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"could not list columns: {exc}")

    # -- scripts -----------------------------------------------------------

    @app.get("/scripts", dependencies=secured)
    def get_scripts():
        return [s.model_dump() for s in store.scripts()]

    @app.put("/scripts/{name}", dependencies=secured)
    def put_script(name: str, script: Script, who: str = Depends(actor)):
        store.save_script(script, old_name=name)
        store.log_event(who, "script registered", detail=script.name)
        return script.model_dump()

    @app.delete("/scripts/{name}", dependencies=secured)
    def delete_script(name: str, who: str = Depends(actor)):
        users = [a.name for a in store.alerts()
                 if any(getattr(s, "script", None) == name for s in a.steps)]
        if users:
            raise HTTPException(409, f"'{name}' is used by: {', '.join(users)}")
        store.delete_script(name)
        store.log_event(who, "script removed", detail=name)
        return {"deleted": name}

    # -- alerts ------------------------------------------------------------

    def _problems(alert: Alert) -> list[str]:
        return validate_alert(alert, {c.name: c for c in store.connections()},
                              {s.name: s for s in store.scripts()})

    @app.get("/alerts", dependencies=secured)
    def get_alerts():
        meta = store.alert_meta()
        return [a.model_dump() | meta.get(a.id, {}) for a in store.alerts()]

    @app.get("/alerts/{alert_id}", dependencies=secured)
    def get_alert(alert_id: int):
        a = store.alert(alert_id)
        return a.model_dump() | store.alert_meta().get(alert_id, {})

    @app.post("/alerts/validate", dependencies=secured)
    def validate(alert: Alert):
        return {"problems": _problems(alert)}

    @app.post("/alerts", dependencies=secured)
    def create_alert(alert: Alert, who: str = Depends(actor)):
        alert.id = None
        problems = _problems(alert)
        if problems:
            raise HTTPException(422, {"problems": problems})
        saved = store.save_alert(alert)
        store.log_event(who, "created", saved, detail="enabled" if saved.enabled else "disabled")
        return saved.model_dump()

    @app.put("/alerts/{alert_id}", dependencies=secured)
    def update_alert(alert_id: int, alert: Alert, who: str = Depends(actor)):
        before = store.alert(alert_id)
        alert.id = alert_id
        problems = _problems(alert)
        if problems:
            raise HTTPException(422, {"problems": problems})
        saved = store.save_alert(alert)
        if before.steps != saved.steps or before.condition != saved.condition:
            store.reset_state(alert_id)   # a different question deserves a fresh start
        else:
            if before.channels.all_clear != saved.channels.all_clear:
                store.update_state(alert_id, clear_since=None, clear_checks=0, clear_dirty=0)
            if before.rearm != saved.rearm:
                store.update_state(alert_id, delta_ref=None)   # the next firing sets a new reference
        store.log_event(who, "edited", saved)
        if before.enabled != saved.enabled:
            store.log_event(who, "enabled" if saved.enabled else "disabled", saved)
        return saved.model_dump()

    # -- groups --------------------------------------------------------------------

    @app.get("/groups", dependencies=secured)
    def groups():
        out: dict[str, dict] = {}
        for a in store.alerts():
            if a.group:
                g = out.setdefault(a.group, {"name": a.group, "alerts": 0, "enabled": 0})
                g["alerts"] += 1
                g["enabled"] += int(a.enabled)
        return sorted(out.values(), key=lambda g: g["name"].lower())

    @app.post("/groups/rename", dependencies=secured)
    def rename_group(body: GroupRename, who: str = Depends(actor)):
        """Moves every alert of ``old`` to ``new``: onto an existing group merges
        the two, and an empty ``new`` dissolves the group."""
        old, new = Alert(name="x", group=body.old).group, Alert(name="x", group=body.new).group
        if not old:
            raise HTTPException(422, "name the group to rename")
        moved = [a for a in store.alerts() if a.group == old]
        if not moved:
            raise NotFound(f"no group called '{old}'")
        for a in moved:
            store.save_alert(a.model_copy(update={"group": new}))
        store.log_event(who, "group dissolved" if not new else "group renamed",
                        detail=f"{old} → {new}" if new else old)
        return {"moved": len(moved), "group": new}

    @app.post("/groups/switch", dependencies=secured)
    def switch_group(body: GroupSwitch, who: str = Depends(actor)):
        changed = 0
        for a in store.alerts():
            if a.group == body.group and store.set_enabled(a.id, body.enabled):
                changed += 1
                store.log_event(who, "enabled" if body.enabled else "disabled", a,
                                detail=f"whole group {body.group}")
        return {"changed": changed}

    @app.delete("/alerts/{alert_id}", dependencies=secured)
    def delete_alert(alert_id: int, keep_history: bool = True, who: str = Depends(actor)):
        a = store.alert(alert_id)
        store.delete_alert(alert_id)
        if not keep_history:
            store.purge(everything=True, alert_id=alert_id)
        store.log_event(who, "deleted", alert_id=alert_id, alert_name=a.name,
                        detail="history kept" if keep_history else "history removed")
        return {"deleted": alert_id}

    @app.post("/alerts/{alert_id}/enable", dependencies=secured)
    def enable(alert_id: int, who: str = Depends(actor), detail: str = ""):
        changed = store.set_enabled(alert_id, True)
        if changed:
            store.log_event(who, "enabled", store.alert(alert_id), detail=detail)
        return {"enabled": True, "changed": changed}

    @app.post("/alerts/{alert_id}/disable", dependencies=secured)
    def disable(alert_id: int, who: str = Depends(actor), detail: str = ""):
        changed = store.set_enabled(alert_id, False)
        if changed:
            store.log_event(who, "disabled", store.alert(alert_id), detail=detail)
        return {"enabled": False, "changed": changed}

    @app.post("/alerts/{alert_id}/run", dependencies=secured)
    def run(alert_id: int, source: Literal["manual", "schedule"] = "manual"):
        run_id = engine.run_now(alert_id, source=source)
        if run_id is None:
            raise HTTPException(409, "this alert is already running")
        return store.run(run_id)

    @app.post("/alerts/preview", dependencies=secured)
    def preview(alert: Alert, limit: int = Query(200, le=2000)):
        return engine.preview(alert, limit=limit) | {"problems": _problems(alert)}

    @app.post("/alerts/test-notification", dependencies=secured)
    def test_notification(alert: Alert):
        return {"deliveries": engine.resend_test(alert)}

    # -- schedules -----------------------------------------------------------

    def _schedule_out(s: Schedule) -> dict:
        d = s.model_dump()
        try:
            d["next_fire_at"] = cron.next_fire(s.cron, s.tz, utcnow()).isoformat(timespec="seconds")
            d["describe"] = cron.describe(s.cron, s.tz)
        except cron.CronError as exc:
            d["next_fire_at"], d["describe"] = None, str(exc)
        return d

    def _check_schedule(s: Schedule) -> None:
        try:
            cron.validate(s.cron, s.tz)
        except cron.CronError as exc:
            raise HTTPException(422, {"problems": [str(exc)]})

    @app.get("/schedules", dependencies=secured)
    def get_schedules(alert_id: Optional[int] = None, who: str = Depends(actor)):
        if who == "sleipnir":
            engine.sleipnir_seen = utcnow()   # Sleipnir polls here: that is its heartbeat
        return [_schedule_out(s) for s in store.schedules(alert_id)]

    @app.post("/schedules", dependencies=secured)
    def create_schedule(s: Schedule, who: str = Depends(actor)):
        s.id = None
        _check_schedule(s)
        saved = store.save_schedule(s)
        store.log_event(who, "schedule added", store.alert(s.alert_id),
                        detail=f"{s.action} {cron.describe(s.cron, s.tz)}")
        return _schedule_out(saved)

    @app.put("/schedules/{schedule_id}", dependencies=secured)
    def update_schedule(schedule_id: int, s: Schedule, who: str = Depends(actor)):
        s.id = schedule_id
        _check_schedule(s)
        saved = store.save_schedule(s)
        store.log_event(who, "schedule changed", store.alert(s.alert_id),
                        detail=f"{s.action} {cron.describe(s.cron, s.tz)}")
        return _schedule_out(saved)

    @app.delete("/schedules/{schedule_id}", dependencies=secured)
    def delete_schedule(schedule_id: int, who: str = Depends(actor)):
        s = store.schedule(schedule_id)
        store.delete_schedule(schedule_id)
        store.log_event(who, "schedule removed", alert_id=s.alert_id,
                        detail=f"{s.action} {cron.describe(s.cron, s.tz)}")
        return {"deleted": schedule_id}

    @app.post("/schedules/{schedule_id}/fired", dependencies=secured)
    def fired(schedule_id: int, body: FiredRequest, who: str = Depends(actor)):
        s = store.schedule(schedule_id)
        store.mark_fired(schedule_id, parse_iso(body.at))
        if body.outcome != "unchanged":
            store.log_event(who, f"schedule {body.outcome}", alert_id=s.alert_id,
                            alert_name=_name_or_blank(s.alert_id),
                            detail=f"{s.action}: {body.detail}".strip(": "))
        return {"ok": True}

    def _name_or_blank(alert_id: int) -> str:
        try:
            return store.alert(alert_id).name
        except NotFound:
            return ""

    # -- history -----------------------------------------------------------

    @app.get("/runs", dependencies=secured)
    def runs(alert_id: Optional[int] = None, status: Optional[str] = None,
             since: Optional[str] = None, until: Optional[str] = None,
             triggered_only: bool = False, unacknowledged: bool = False,
             limit: int = Query(200, le=5000), offset: int = 0):
        kw = dict(alert_id=alert_id, status=status, since=parse_iso(since), until=parse_iso(until),
                  triggered_only=triggered_only, unacknowledged=unacknowledged)
        return {"total": store.count_runs(**kw), "runs": store.runs(**kw, limit=limit, offset=offset)}

    @app.get("/runs/{run_id}", dependencies=secured)
    def run_detail(run_id: int):
        return store.run(run_id)

    @app.post("/runs/acknowledge", dependencies=secured)
    def acknowledge(body: AckRequest, who: str = Depends(actor)):
        if body.all or (body.alert_id is not None and not body.run_ids):
            n = store.acknowledge_all(body.alert_id, by=who)
        else:
            n = store.acknowledge(body.run_ids, by=who)
        return {"acknowledged": n}

    @app.post("/history/purge", dependencies=secured)
    def purge(body: PurgeRequest, who: str = Depends(actor)):
        try:
            result = store.purge(older_than_days=body.older_than_days, keep_last=body.keep_last,
                                 alert_id=body.alert_id, everything=body.everything,
                                 keep_unacknowledged=body.keep_unacknowledged, dry_run=body.dry_run)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if not body.dry_run:
            store.log_event(who, "history cleaned", alert_id=body.alert_id,
                            detail=f"removed {result['runs']} run(s), {result['events']} event(s)")
        return result

    @app.get("/history/stats", dependencies=secured)
    def stats(days: int = 1):
        return store.stats(utcnow() - timedelta(days=days))

    @app.get("/history/daily", dependencies=secured)
    def daily(days: int = 14, alert_id: Optional[int] = None):
        return store.daily_counts(utcnow() - timedelta(days=days), alert_id)

    @app.get("/events", dependencies=secured)
    def events(alert_id: Optional[int] = None, limit: int = Query(200, le=5000)):
        return store.events(alert_id, limit)

    # -- settings ----------------------------------------------------------

    @app.get("/settings", dependencies=secured)
    def get_settings():
        s = store.settings()
        return {"smtp": s.get("smtp") or {}, "retention": s.get("retention") or {},
                "engine_paused": bool(s.get("engine_paused", False))}

    @app.put("/settings", dependencies=secured)
    def put_settings(values: dict, who: str = Depends(actor)):
        allowed = {k: v for k, v in values.items() if k in ("smtp", "retention")}
        store.set_settings(allowed)
        store.log_event(who, "settings changed", detail=", ".join(sorted(allowed)))
        return get_settings()

    # -- export / import -----------------------------------------------------

    def _parts(parts: Optional[str]) -> set[str]:
        chosen = {p.strip() for p in (parts or "").split(",") if p.strip()} or set(BUNDLE_PARTS)
        unknown = chosen - set(BUNDLE_PARTS)
        if unknown:
            raise HTTPException(422, f"unknown part(s): {', '.join(sorted(unknown))}; "
                                     f"choose from {', '.join(BUNDLE_PARTS)}")
        return chosen

    @app.get("/export", dependencies=secured)
    def export(alert_ids: Optional[str] = None, include_connections: bool = True,
               include_scripts: bool = True, parts: Optional[str] = None):
        """``parts`` picks what goes in (connections, scripts, alerts, schedules;
        all by default). ``alert_ids`` narrows it to some alerts and what they use."""
        chosen = _parts(parts)
        wanted = {int(x) for x in alert_ids.split(",")} if alert_ids else None
        alerts = [a for a in store.alerts() if wanted is None or a.id in wanted]
        names = {a.id: a.name for a in alerts}
        schedules = [s.model_dump(exclude={"id", "alert_id", "last_fired_at"}) | {"alert": names[s.alert_id]}
                     for s in store.schedules() if s.alert_id in names]
        used_conns = {getattr(s, "connection", "") for a in alerts for s in a.steps}
        used_scripts = {getattr(s, "script", "") for a in alerts for s in a.steps}
        b = Bundle(
            connections=[c for c in store.connections() if include_connections and "connections" in chosen
                         and (wanted is None or c.name in used_conns)],
            scripts=[s for s in store.scripts() if include_scripts and "scripts" in chosen
                     and (wanted is None or s.name in used_scripts)],
            alerts=[a.model_copy(update={"id": None, "enabled": False}) for a in alerts] if "alerts" in chosen else [],
            schedules=schedules if "schedules" in chosen else [])
        return b.model_dump()

    @app.post("/import", dependencies=secured)
    def import_bundle(bundle: Bundle, replace: bool = False, parts: Optional[str] = None,
                      who: str = Depends(actor)):
        """Brings in the chosen ``parts`` of a bundle (all by default). Schedules
        attach to the alert of the same name: one imported now, or one already here."""
        chosen = _parts(parts)
        bundle = Bundle(connections=bundle.connections if "connections" in chosen else [],
                        scripts=bundle.scripts if "scripts" in chosen else [],
                        alerts=bundle.alerts if "alerts" in chosen else [],
                        schedules=bundle.schedules if "schedules" in chosen else [])
        report = {"connections": [], "scripts": [], "alerts": [], "schedules": 0, "skipped": []}
        for c in bundle.connections:
            if store.connection(c.name) and not replace:
                report["skipped"].append(f"connection {c.name}")
                continue
            store.save_connection(c); engine.pool.forget(c.name)
            report["connections"].append(c.name)
        for s in bundle.scripts:
            if store.script(s.name) and not replace:
                report["skipped"].append(f"script {s.name}")
                continue
            store.save_script(s)
            report["scripts"].append(s.name)
        ids: dict[str, int] = {}
        for a in bundle.alerts:
            existing = store.alert_by_name(a.name)
            if existing and not replace:
                report["skipped"].append(f"alert {a.name}")
                continue
            # Imported alerts arrive switched off: someone should look first.
            if existing:
                for old in store.schedules(existing.id):
                    store.delete_schedule(old.id)
            a = a.model_copy(update={"id": existing.id if existing else None, "enabled": False})
            saved = store.save_alert(a)
            ids[saved.name] = saved.id
            report["alerts"].append(saved.name)
        cleared: set[int] = set()
        for raw in bundle.schedules:
            name = raw.get("alert")
            if name in ids:
                alert_id = ids[name]
            elif (here := store.alert_by_name(name or "")) is not None:
                alert_id = here.id
                if bundle.alerts and any(a.name == name for a in bundle.alerts):
                    # its alert was in the file but skipped as already here: so are its schedules
                    report["skipped"].append(f"schedule for {name} (its alert was skipped)")
                    continue
                if replace and alert_id not in cleared:     # the file's schedules replace this alert's
                    for old in store.schedules(alert_id):
                        store.delete_schedule(old.id)
                    cleared.add(alert_id)
            else:
                report["skipped"].append(f"schedule for {name} (no alert called that here)")
                continue
            body = {k: v for k, v in raw.items() if k != "alert"}
            s = Schedule(**body, alert_id=alert_id)
            try:
                cron.validate(s.cron, s.tz)
            except cron.CronError:
                report["skipped"].append(f"schedule for {name} ({s.cron})")
                continue
            if any((o.action, o.cron, o.tz) == (s.action, s.cron, s.tz) for o in store.schedules(alert_id)):
                report["skipped"].append(f"schedule for {name} ({s.action} at {s.cron}, already here)")
                continue
            store.save_schedule(s)
            report["schedules"] += 1
        store.log_event(who, "imported", detail=f"{len(report['alerts'])} alert(s), "
                        f"{len(report['connections'])} connection(s), {len(report['scripts'])} script(s), "
                        f"{report['schedules']} schedule(s)")
        return report

    return app
