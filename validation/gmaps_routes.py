"""
Real Google Maps comparison via the Routes API v2 — traffic-aware DRIVING at
the exact same departure times as our planner estimates.

Why we compare against Google's DRIVING time and not Google transit:
  * The Routes API v2 has no transit coverage for Delhi. Probed 2026-11-16:
    TRANSIT mode returns a walking fallback (zero transit legs) even for pure
    metro pairs like Kashmere Gate -> Rajiv Chowk (3 stops on the Yellow Line).
  * Legacy Directions / Distance Matrix APIs cannot be enabled on new
    Google Maps Platform projects (403 LegacyApiNotActivatedMapError).
  So the honest, automated, reproducible comparison at scale is Google's
  traffic-aware driving time. The brief's "compare with Google Maps for the
  same pairs and times" is satisfied with the same OD pairs and the same
  departure timestamps. Google-transit spot checks remain manual (Maps app).

For each canonical OD pair, at 09:00 (rush) and 15:00 (quiet) IST on
Wednesday 2026-11-18:
  * Google Routes API v2 computeRoutes, travelMode=DRIVE,
    routingPreference=TRAFFIC_AWARE, departureTime = the same instant
  * Delhi Pulse LIVE edge planner (Cloudflare Worker) /api/journeys

Output: docs/od_comparison.json with REAL Google values (replaces the earlier
research-agent estimates, which were approximations).
"""
import functools
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT / "validation"))

from od_comparison import OD_PAIRS  # canonical 20 pairs, same coords

KEY = os.environ["GOOGLE_MAPS_KEY"]
GMAPS_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
EDGE = "https://delhi-pulse.prayaas.workers.dev/api/journeys"
DEPART_TIMES = {
    "rush": "2026-11-18T09:00:00+05:30",   # weekday morning peak
    "quiet": "2026-11-18T15:00:00+05:30",  # mid-afternoon off-peak
}


def gmaps_drive(olat, olng, dlat, dlng, depart_iso):
    """Traffic-aware driving time via Routes API v2. Returns (min, km) or None."""
    body = {
        "origin": {"location": {"latLng": {"latitude": olat, "longitude": olng}}},
        "destination": {"location": {"latLng": {"latitude": dlat, "longitude": dlng}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "departureTime": depart_iso,
    }
    h = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": KEY,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }
    r = requests.post(GMAPS_URL, headers=h, json=body, timeout=30)
    if r.status_code != 200:
        print(f"    google drive error {r.status_code}: {r.text[:120]}")
        return None
    d = r.json()
    if not d.get("routes"):
        return None
    dur_s = float(d["routes"][0]["duration"][:-1])
    km = d["routes"][0]["distanceMeters"] / 1000
    return round(dur_s / 60, 1), round(km, 1)


def gmaps_transit_probe(olat, olng, dlat, dlng, depart_iso):
    """One honest attempt at Google transit. None when Delhi is not covered."""
    body = {
        "origin": {"location": {"latLng": {"latitude": olat, "longitude": olng}}},
        "destination": {"location": {"latLng": {"latitude": dlat, "longitude": dlng}}},
        "travelMode": "TRANSIT",
        "departureTime": depart_iso,
    }
    h = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": KEY,
        "X-Goog-FieldMask": "routes",
    }
    try:
        r = requests.post(GMAPS_URL, headers=h, json=body, timeout=30)
        if r.status_code != 200 or not r.json().get("routes"):
            return None
        legs = r.json()["routes"][0]["legs"]
        if not any("transitDetails" in l for l in legs):
            return None  # walking fallback => no transit coverage
        return round(sum(float(l["duration"][:-1]) for l in legs) / 60, 1)
    except Exception:
        return None


def our_estimate(olat, olng, dlat, dlng, depart_iso):
    """Live Delhi Pulse edge planner: first (best) option."""
    q = {
        "from": f"{olat},{olng}",
        "to": f"{dlat},{dlng}",
        "depart": depart_iso,
        "max": "5",
    }
    try:
        r = requests.get(EDGE, params=q, timeout=60)
        d = r.json()
        opts = d.get("options") or []
        if not opts:
            return None, None, None
        o = opts[0]
        return o["total_min"], o.get("route_name"), o.get("transfers")
    except Exception as e:
        print(f"    planner error: {e}")
        return None, None, None


def main():
    reuse = "--reuse-google" in sys.argv
    # existing google values keyed by origin|destination (avoids re-calling
    # the Routes API when we are only refreshing planner estimates)
    prev = {}
    if reuse:
        prev_path = ROOT / "docs" / "od_comparison.json"
        if prev_path.exists():
            for r in json.loads(prev_path.read_text(encoding="utf-8")):
                prev[(r["origin"], r["destination"])] = r

    results = []
    for i, (oname, dname, olat, olng, dlat, dlng) in enumerate(OD_PAIRS, 1):
        print(f"[{i:02d}/20] {oname} -> {dname}")

        our09, route, xfers = our_estimate(olat, olng, dlat, dlng, DEPART_TIMES["rush"])
        our15, _, _ = our_estimate(olat, olng, dlat, dlng, DEPART_TIMES["quiet"])
        if reuse and (oname, dname) in prev:
            p = prev[(oname, dname)]
            g09 = ((p["google_drive_rush_min"], p["google_drive_rush_km"])
                   if p.get("google_drive_rush_min") else None)
            g15 = ((p["google_drive_quiet_min"], p["google_drive_quiet_km"])
                   if p.get("google_drive_quiet_min") else None)
            gt = p.get("google_transit_rush_min")
        else:
            g09 = gmaps_drive(olat, olng, dlat, dlng, DEPART_TIMES["rush"])
            g15 = gmaps_drive(olat, olng, dlat, dlng, DEPART_TIMES["quiet"])
            gt = gmaps_transit_probe(olat, olng, dlat, dlng, DEPART_TIMES["rush"])
            time.sleep(0.2)

        rec = {
            "origin": oname,
            "destination": dname,
            "our_rush_min": our09,
            "our_quiet_min": our15,
            "rush_effect_min": round(our09 - our15, 1) if our09 and our15 else None,
            "routes": route,
            "transfers": xfers,
            "google_drive_rush_min": g09[0] if g09 else None,
            "google_drive_rush_km": g09[1] if g09 else None,
            "google_drive_quiet_min": g15[0] if g15 else None,
            "google_drive_quiet_km": g15[1] if g15 else None,
            "google_transit_rush_min": gt,
        }
        results.append(rec)
        print(f"    ours: {our09 or '?'}m (rush) {our15 or '?'}m (quiet) via {route or 'none'}")
        print(f"    google drive: {g09[0] if g09 else '?'}m/{g09[1] if g09 else '?'}km (rush), "
              f"{g15[0] if g15 else '?'}m (quiet); transit: {gt if gt else 'no coverage (walk fallback)'}")

    out = ROOT / "docs" / "od_comparison.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nsaved {len(results)} pairs -> {out}")

    # summary stats
    pairs = [r for r in results if r["our_rush_min"] and r["google_drive_rush_min"]]
    if pairs:
        import statistics
        ratios = [r["our_rush_min"] / r["google_drive_rush_min"] for r in pairs]
        print(f"\npairs with both values: {len(pairs)}/20")
        print(f"bus / google-drive ratio at rush hour: median "
              f"{statistics.median(ratios):.2f}x  mean {statistics.mean(ratios):.2f}x")
        covered = sum(1 for r in results if r["google_transit_rush_min"])
        print(f"google transit coverage for Delhi: {covered}/{len(results)} pairs "
              "(API returns walking fallback -> no transit data for Delhi)")


if __name__ == "__main__":
    main()
