"""
OD comparison table: canonical Delhi pairs → our estimates → framework for
Google Maps comparison (filled manually or via Distance Matrix API key).

Uses well-known Delhi landmarks with approximate coordinates.
Runs through the live API for our bus estimates at rush hour (09:00 IST)
and quiet hour (15:00 IST) to show the traffic effect.

Output: docs/od_comparison.json
"""
import functools
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

print = functools.partial(print, flush=True)

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Canonical Delhi OD pairs (landmarks used by the brief example)
OD_PAIRS = [
    ("Kashmere Gate ISBT", "Nehru Place", 28.6673, 77.2285, 28.5494, 77.2514),
    ("Anand Vihar ISBT", "Dhaula Kuan", 28.6469, 77.3152, 28.5921, 77.1601),
    ("Connaught Place", "Dwarka Sector 9", 28.6316, 77.2167, 28.6159, 77.0267),
    ("AIIMS", "Rohini West", 28.5672, 77.2100, 28.7315, 77.1213),
    ("New Delhi Railway Stn", "Gurgaon Border", 28.6428, 77.2211, 28.4815, 77.0900),
    ("AIIMS", "Karol Bagh", 28.5672, 77.2100, 28.6449, 77.1518),
    ("IIT Delhi", "India Gate", 28.5474, 77.1943, 28.6129, 77.2295),
    ("Saket Mall", "Chandni Chowk", 28.5285, 77.2075, 28.6506, 77.2303),
    ("Vasant Kunj", "Mayur Vihar", 28.5206, 77.1585, 28.6080, 77.2903),
    ("JNU", "Noida Sector 18", 28.5400, 77.1660, 28.5706, 77.3219),
    ("Uttam Nagar", "Anand Vihar", 28.6199, 77.0567, 28.6469, 77.3152),
    ("Karol Bagh", "Noida City Centre", 28.6449, 77.1518, 28.5747, 77.3525),
    ("Mehrauli", "Shivaji Stadium", 28.5178, 77.1827, 28.6300, 77.2220),
    ("Rohini West", "Kashmere Gate", 28.7315, 77.1213, 28.6673, 77.2285),
    ("Dwarka Sec 10", "Connaught Place", 28.6130, 77.0470, 28.6316, 77.2167),
    ("New Delhi Rly", "Nizamuddin Dargah", 28.6428, 77.2211, 28.5906, 77.2445),
    ("Sarojini Nagar", "India Habitat Centre", 28.5724, 77.1959, 28.5921, 77.2210),
    ("Lajpat Nagar", "Janakpuri", 28.5677, 77.2432, 28.6219, 77.0878),
    ("Kalkaji Mandir", "Karol Bagh", 28.5355, 77.2588, 28.6449, 77.1518),
    ("Nehru Place", "Vasant Vihar", 28.5494, 77.2514, 28.5580, 77.1600),
]

def query(base, olat, olng, dlat, dlng, depart_h):
    q = (f"/journeys?from={olat},{olng}&to={dlat},{dlng}"
         f"&depart=2026-11-18T{depart_h:02d}:00:00%2B05:30&max=5")
    try:
        r = base.get(q, timeout=30).json()
        return r
    except Exception as e:
        return {"error": str(e)}


def main():
    base_url = "http://localhost:8010"
    results = []

    with httpx.Client(base_url=base_url, timeout=30) as c:
        for (oname, dname, olat, olng, dlat, dlng) in OD_PAIRS:
            r09 = query(c, olat, olng, dlat, dlng, 9)
            r15 = query(c, olat, olng, dlat, dlng, 15)

            opts09 = r09.get("options") or []
            opts15 = r15.get("options") or []

            our09 = opts09[0]["total_min"] if opts09 else None
            our15 = opts15[0]["total_min"] if opts15 else None
            routes09 = opts09[0]["route_name"] if opts09 else None
            xfers09 = opts09[0]["transfers"] if opts09 else None

            results.append({
                "origin": oname, "destination": dname,
                "our_rush_min": our09, "our_quiet_min": our15,
                "rush_effect_min": round(our09 - our15, 1) if our09 and our15 else None,
                "routes": routes09, "transfers": xfers09,
                "google_maps_min": None,   # filled manually or via API key
            })
            status = f"rush={our09 or '?'}m quiet={our15 or '?'}m via {routes09 or 'no route'}"
            print(f"  {oname} -> {dname}: {status}")

    out = Path(__file__).resolve().parent.parent / "docs" / "od_comparison.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nsaved {len(results)} pairs -> {out}")


if __name__ == "__main__":
    main()
