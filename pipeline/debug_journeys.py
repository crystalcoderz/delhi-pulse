"""Debug: why does the 543 direct ride take 71 min instead of ~53?"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.app import (PAT, RH, RS, CH, E50, STOP_MEMBER, leg_seconds,
                     ride_time, wait_minutes, stops)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

for pi, p in enumerate(PAT):
    if p["route_id"] == "543" and p["stops"][0] == "S0001":
        j = p["stops"].index("S0010")
        print(f"pattern {pi}: {len(p['stops'])} stops, S0010 at index {j}")
        print("legs (m):", [round(x) for x in p["leg_m"]])
        print("sum legs:", round(sum(p["leg_m"])), "m")
        print("RH(543,9):", RH.get(("543", 9)), " RS(543):", RS.get("543"))
        dep = dt.datetime(2026, 10, 20, 9, 54, tzinfo=IST)
        board = dep.timestamp()
        per_leg = []
        t = board
        for k in range(0, j):
            h = int((t + 19800) // 3600 % 24)
            s = leg_seconds(p["route_id"], p["stops"][k], p["stops"][k + 1],
                            p["leg_m"][k], h)
            per_leg.append((p["stops"][k], h, round(p["leg_m"][k]), round(s)))
            t += s
        print("per-leg (stop, hour, meters, seconds):")
        for r in per_leg:
            print("  ", r)
        print("total ride:", round(sum(r[3] for r in per_leg)), "s =",
              round(sum(r[3] for r in per_leg) / 60, 1), "min")
        print("E50 keys sample:", list(E50)[:5])
        break
