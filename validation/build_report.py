"""Assemble the single validation/report artifact from all model outputs.
This is the data backbone of the Ground-Truth page (UI comes later).

Writes docs/report.json:
  { model: {...}, cleaning: per-day drop stats, holdout: {...},
    findings: human-readable headline stats calculated from the data itself }
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import CLEAN, MODEL  # noqa: E402


def jload(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


def main():
    out = {"generated_utc": None}
    import datetime as dt
    out["generated_utc"] = dt.datetime.utcnow().isoformat()

    # model metas
    out["model"] = {
        "traversals": jload(MODEL / "meta.json"),
        "ping_speeds": jload(MODEL / "meta_pings.json"),
    }

    # per-day cleaning stats (real days only)
    cleaning = {}
    for p in sorted(CLEAN.glob("stats_*.json")):
        day = p.stem.replace("stats_", "")
        d = jload(p)
        if d and d.get("n_total", 0) > 1_000_000:     # real days, not synth
            cleaning[day] = d
    out["cleaning"] = cleaning

    # holdout
    out["holdout"] = {
        "edges": jload(MODEL / "holdout_report.json"),
        "journeys": jload(MODEL / "holdout_journeys.json"),
        "pings_leave_one_day_out": jload(MODEL / "holdout_pings.json"),
    }

    # derived findings
    f = []
    try:
        days = sorted(cleaning)
        if days:
            tot = sum(cleaning[d]["n_total"] for d in days)
            kept = sum(cleaning[d]["n_kept"] for d in days)
            f.append(f"{len(days)} real October days cleaned: "
                     f"{kept/1e6:.1f}M of {tot/1e6:.1f}M pings kept "
                     f"({100*kept/max(1,tot):.1f}%)")
        pm = out["model"].get("ping_speeds")
        if pm:
            f.append(f"{pm['n_routes']} routes profiled across "
                     f"{len(pm.get('source_days', []))} days "
                     f"({pm['n_route_hour_cells']} route x hour cells)")
        if out["holdout"]:
            ho = out["holdout"].get("pings_leave_one_day_out")
            if ho:
                f.append(f"leave-one-day-out route x hour speeds: "
                         f"MAE {ho['mae_model_kmh']} km/h vs city baseline "
                         f"{ho['mae_city_baseline_kmh']} km/h "
                         f"({ho['lift_vs_baseline']}% better), "
                         f"n={ho['n_cells_evald']:,} cells")
            jo = out["holdout"].get("journeys")
            if jo:
                f.append(f"journey replay: ride MAE {jo['ride_mae_min']} min, "
                         f"{int(100*jo['within_10min'])}% within 10 min, "
                         f"coverage {int(100*jo['coverage'])}%")
    except Exception as e:
        f.append(f"findings error: {e}")
    out["findings"] = f

    dst = Path(__file__).resolve().parent.parent / "docs" / "report.json"
    dst.write_text(json.dumps(out, indent=2))
    print(f"report -> {dst}")
    for line in f:
        print(" •", line)


if __name__ == "__main__":
    main()
