"""Why did an alert not reach Gjallarhorn? Reads Heimdall's database (read only) and says.

    python scripts\\check-gjallarhorn.py                 every alert
    python scripts\\check-gjallarhorn.py "Price limits"  the alerts whose name contains this

Run it from the heimdall folder, with the same heimdall.toml as Huginn. It changes nothing.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from heimdall import config, gjallarhorn
from heimdall.core.models import Alert

cfg = config.load()
db = Path(cfg.db_path)
if not db.is_file():
    sys.exit(f"No database at {db.resolve()}: run this from the heimdall folder (where heimdall.toml is).")
con = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
match = (sys.argv[1] if len(sys.argv) > 1 else "").lower()

settings = {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT key, value FROM settings")}
g = gjallarhorn.Settings.from_store(settings)
print(f"Database: {db.resolve()}")
if not g.configured:
    print("!! Gjallarhorn has no shared folder in Settings: nothing can be published.")
else:
    reach = Path(g.root).is_dir()
    print(f"Shared folder: {g.root}  ({'reachable' if reach else 'NOT reachable'} from this PC, as {Path.home().name})")
print()

for row in con.execute("SELECT id, name, enabled, body FROM alerts ORDER BY name"):
    if match and match not in row["name"].lower():
        continue
    try:
        a = Alert.model_validate({**json.loads(row["body"]), "id": row["id"], "name": row["name"],
                                  "enabled": bool(row["enabled"])})
    except Exception as exc:  # noqa: BLE001
        print(f"== {row['name']}: unreadable ({exc})\n")
        continue
    st = con.execute("SELECT * FROM alert_state WHERE alert_id = ?", (a.id,)).fetchone()
    pu = a.channels.gjallarhorn
    print(f"== {a.name}  (id {a.id}, severity {a.severity})")
    problems = []
    if not a.enabled:
        problems.append("the alert is disabled")
    if not pu.enabled:
        problems.append("'Pop up on traders' screens' is OFF for this alert (Edit > Notify > Pop-ups)")
    print(f"   pop-ups: {'on' if pu.enabled else 'OFF'}, for {', '.join(pu.audience) or 'every trader'}, "
          f"recovery {'yes' if pu.on_recovery else 'no'}")
    print(f"   re-arm: {a.rearm.mode}" + (f", currently firing since an earlier run" if st and st["last_triggered"] else ""))
    if st and st["last_triggered"] and a.rearm.mode == "transition":
        problems.append("it is still firing from an earlier run: with re-arm 'transition' it notifies only when it "
                        "starts firing again after clearing (Acknowledge does not re-arm it)")
    runs = con.execute("SELECT id, started_at, status, condition_met, triggered, notified, message FROM runs "
                       "WHERE alert_id = ? ORDER BY id DESC LIMIT 5", (a.id,)).fetchall()
    if not runs:
        problems.append("it has never run")
    for r in runs:
        deliv = con.execute("SELECT channel, target, ok, error FROM deliveries WHERE run_id = ?", (r["id"],)).fetchall()
        sent = "; ".join(f"{d['channel']}->{d['target']} {'ok' if d['ok'] else 'FAILED: ' + d['error']}" for d in deliv)
        print(f"   run {r['id']} {r['started_at'][:19]} {r['status']:<5} met={bool(r['condition_met'])!s:<5} "
              f"notified={r['notified'] or '-':<8} {sent or '(nothing sent)'}")
        for d in deliv:
            if d["channel"] == "gjallarhorn" and not d["ok"]:
                problems.append(f"publishing to the share failed on run {r['id']}: {d['error']}")
    if runs and not any(r["notified"] for r in runs) and any(r["condition_met"] for r in runs):
        problems.append("the condition is met but the last runs did not notify (re-arm, cooldown, or active hours)")
    print("   -> " + (" | ".join(dict.fromkeys(problems)) if problems else "looks fine"))
    print()
