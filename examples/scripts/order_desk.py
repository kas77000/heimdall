"""Example Heimdall script that reads the previous step's table on stdin.

Heimdall pipes the chosen step's rows as JSON records; this adds the desk that
owns each order and prints the table back, so the next step can use `desk`.
"""
import json
import sys

DESKS = {"VWAP": "Program", "TWAP": "Program", "IS": "Single stock", "POV": "Single stock"}

rows = json.load(sys.stdin)
for r in rows:
    r["desk"] = DESKS.get(r.get("algo"), "Unassigned")
print(json.dumps(rows))
