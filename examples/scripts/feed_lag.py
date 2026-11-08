"""Example Heimdall script: how far behind each market-data feed is.

A real version would ask the feed handlers. This one makes the lag up from the
clock so a demo alert flips now and then. It prints JSON records, which is the
format the script is registered with.

    python feed_lag.py --feeds TSE,HKEX,ASX
"""
import argparse
import json
import math
import time

p = argparse.ArgumentParser()
p.add_argument("--feeds", default="TSE,HKEX,ASX,SGX")
a = p.parse_args()

now = time.time()
rows = []
for i, feed in enumerate(f.strip() for f in a.feeds.split(",") if f.strip()):
    lag = max(0.0, 40 + 50 * math.sin(now / 45 + i * 1.7))
    rows.append({"feed": feed, "lag_secs": round(lag, 1),
                 "last_tick": time.strftime("%H:%M:%S", time.gmtime(now - lag))})
print(json.dumps(rows))
