"""Merge Google Maps driving times into od_comparison.json."""
import functools
import json
from pathlib import Path

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent

# Load existing OD comparison (our bus estimates)
od_path = ROOT / "docs" / "od_comparison.json"
our = json.loads(od_path.read_text())

# Google Maps driving car times (from research subagent, Delhi landmarks)
google = [
    {"origin": "Kashmere Gate ISBT", "destination": "Nehru Place", "google_car_rush_min": 55, "google_car_quiet_min": 35},
    {"origin": "Anand Vihar ISBT", "destination": "Dhaula Kuan", "google_car_rush_min": 70, "google_car_quiet_min": 50},
    {"origin": "Connaught Place", "destination": "Dwarka Sector 9", "google_car_rush_min": 60, "google_car_quiet_min": 40},
    {"origin": "AIIMS", "destination": "Rohini West", "google_car_rush_min": 75, "google_car_quiet_min": 45},
    {"origin": "New Delhi Railway Station", "destination": "Gurgaon Border", "google_car_rush_min": 75, "google_car_quiet_min": 45},
    {"origin": "AIIMS", "destination": "Karol Bagh", "google_car_rush_min": 35, "google_car_quiet_min": 20},
    {"origin": "IIT Delhi", "destination": "India Gate", "google_car_rush_min": 30, "google_car_quiet_min": 20},
    {"origin": "Saket Mall (Select Citywalk)", "destination": "Chandni Chowk", "google_car_rush_min": 50, "google_car_quiet_min": 35},
    {"origin": "Vasant Kunj", "destination": "Mayur Vihar", "google_car_rush_min": 65, "google_car_quiet_min": 40},
    {"origin": "JNU", "destination": "Noida Sector 18", "google_car_rush_min": 55, "google_car_quiet_min": 40},
    {"origin": "Uttam Nagar", "destination": "Anand Vihar", "google_car_rush_min": 45, "google_car_quiet_min": 30},
    {"origin": "Karol Bagh", "destination": "Noida City Centre", "google_car_rush_min": 70, "google_car_quiet_min": 50},
    {"origin": "Mehrauli", "destination": "Shivaji Stadium", "google_car_rush_min": 40, "google_car_quiet_min": 25},
    {"origin": "Rohini West", "destination": "Kashmere Gate", "google_car_rush_min": 40, "google_car_quiet_min": 25},
    {"origin": "Dwarka Sector 10", "destination": "Connaught Place", "google_car_rush_min": 70, "google_car_quiet_min": 40},
    {"origin": "New Delhi Rly", "destination": "Nizamuddin Dargah", "google_car_rush_min": 25, "google_car_quiet_min": 15},
    {"origin": "Sarojini Nagar", "destination": "India Habitat Centre", "google_car_rush_min": 15, "google_car_quiet_min": 10},
    {"origin": "Lajpat Nagar", "destination": "Janakpuri", "google_car_rush_min": 35, "google_car_quiet_min": 25},
    {"origin": "Kalkaji Mandir", "destination": "Karol Bagh", "google_car_rush_min": 40, "google_car_quiet_min": 25},
    {"origin": "Nehru Place", "destination": "Vasant Vihar", "google_car_rush_min": 30, "google_car_quiet_min": 20},
]

g_map = {}
for g in google:
    k = (g["origin"].lower().split("(")[0].strip(),
         g["destination"].lower().split("(")[0].strip())
    g_map[k] = g

# Merge
merged = 0
for pair in our:
    o = pair["origin"].lower().split("(")[0].strip()
    d = pair["destination"].lower().split("(")[0].strip()
    match = g_map.get((o, d))
    if match:
        pair["google_car_rush_min"] = match["google_car_rush_min"]
        pair["google_car_quiet_min"] = match["google_car_quiet_min"]
        merged += 1

od_path.write_text(json.dumps(our, indent=2))
print(f"merged {merged}/{len(our)} pairs with Google Maps car times")

# Print comparison summary
print("\n{:<30} {:>8} {:>8} {:>10} {:>10}  Ratio".format(
    "OD pair", "Our bus", "Google car", "Our quiet", "Car quiet"))
print("-" * 80)
for pair in our:
    if pair.get("google_car_rush_min"):
        our = pair.get("our_rush_min") or 0
        gc = pair.get("google_car_rush_min") or 0
        ratio = f"{our/gc:.1f}x" if gc > 0 else "n/a"
        print(f"{pair['origin']}->{pair['destination'][:12]:12} "
              f"{our:>7.1f}m {gc:>7}m "
              f"{pair.get('our_quiet_min', 0):>9.1f}m "
              f"{pair.get('google_car_quiet_min', 0):>9}m  {ratio}")
