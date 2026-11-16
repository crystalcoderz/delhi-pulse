"""Assemble the single validation/report artifact from all model outputs.
This is the data backbone of the Ground-Truth page (UI comes later).

Writes docs/report.json:
  { model: {...}, cleaning: per-day drop stats, holdout: {...}, findings: [...] }

Schema contract (fix E — drift is fatal, not silently null):
  model.traversals               <- model/meta.json            (required)
  model.ping_speeds              <- model/meta_pings.json      (required)
  cleaning.<day>                 <- data/clean/stats_<day>.json (required per file)
  holdout.edges                  <- model/holdout_report.json  (optional -> null;
                                    not part of the overnight chain)
  holdout.journeys               <- model/holdout_journeys.json (required *fresh*;
                                    null + holdout.error when missing/stale)
  holdout.pings_leave_one_day_out <- model/holdout_pings.json  (required)

A missing file or missing key in a required input aborts with the file and key
names in the message. The overnight chain runs this only after every producer,
so a missing key there means schema drift, never a benign absence — swallowing
it (the old blanket ``except``) shipped silently wrong reports. The single
tolerated exception is holdout_journeys.json: per fix plan E it must degrade to
``"journeys": null`` with an explicit ``"error"`` so the page can say "pending"
instead of showing stale numbers.
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import CLEAN, MODEL  # noqa: E402

JOURNEYS_STALE_ERROR = "holdout_journeys missing or stale"

# Keys build_report actually reads (kept beside the readers so drift is visible).
TRAVERSAL_KEYS = ("version", "days", "n_edge_hour_cells", "n_routes")
PING_META_KEYS = ("n_routes", "n_route_hour_cells", "source_days")
PING_HOLDOUT_KEYS = (
    "test", "days", "n_cells_evald", "n_cells_intersection", "dow_subset",
    "mae_model_kmh", "mae_model_kmh_weighted",
    "mae_dow_model_kmh", "mae_dow_model_kmh_weighted",
    "mae_city_baseline_kmh", "mae_city_baseline_kmh_weighted",
    "mae_pooled_intersection_kmh", "mae_dow_intersection_kmh",
    "lift_vs_baseline", "lift_dow_vs_pooled",
    "lift_dow_vs_pooled_weighted", "lift_dow_vs_pooled_allrows",
    "mape_model_pct", "within_5kmh", "within_10kmh",
)
EDGE_HOLDOUT_KEYS = ("n_eval", "mae_min", "within_10min")
JOURNEY_KEYS = (
    "n_journeys", "mean_ride_min", "mean_pred_min", "mae_min", "bias_min",
    "within_5_min", "within_10_min", "fallback_leg_share",
    "by_hour_band", "by_length_band",
)   # band_coverage is additive: used when present, not required here
CLEANING_KEYS = ("day", "n_total", "n_kept")


def load_json(path):
    """Read a required JSON input; abort naming the file on absence/corruption."""
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"[report] missing required input: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"[report] invalid JSON in {p}: {e}") from e


def require(obj, keys, path, hint=None):
    """Abort when a required key is absent (schema drift, not an exception)."""
    if not isinstance(obj, dict):
        raise SystemExit(f"[report] expected a JSON object in {path}")
    missing = [k for k in keys if k not in obj]
    if missing:
        msg = f"[report] schema drift in {path}: missing key(s) {missing}"
        raise SystemExit(f"{msg} — {hint}" if hint else msg)
    return obj


def main():
    out = {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat()}

    # ---- model metas (both produced before this in the overnight chain) ----
    meta_path = MODEL / "meta.json"
    meta = require(load_json(meta_path), TRAVERSAL_KEYS, meta_path)
    pings_meta_path = MODEL / "meta_pings.json"
    pings_meta = require(load_json(pings_meta_path), PING_META_KEYS,
                         pings_meta_path)
    out["model"] = {"traversals": meta, "ping_speeds": pings_meta}

    # ---- per-day cleaning stats (real days only) ----
    cleaning = {}
    for p in sorted(CLEAN.glob("stats_*.json")):
        d = require(load_json(p), CLEANING_KEYS, p)
        day = p.stem.replace("stats_", "")
        if d["n_total"] > 1_000_000:     # real days, not synth fixtures
            cleaning[day] = d
    out["cleaning"] = cleaning

    # ---- holdout arms ----
    pings_path = MODEL / "holdout_pings.json"
    pings = require(load_json(pings_path), PING_HOLDOUT_KEYS, pings_path,
                    hint="rerun validation/hold_pings.py")

    edges = None
    edges_path = MODEL / "holdout_report.json"
    if edges_path.is_file():             # optional: not in the nightly chain
        edges = require(load_json(edges_path), EDGE_HOLDOUT_KEYS, edges_path)

    journeys, journeys_error = load_journeys(meta_path)

    out["holdout"] = {
        "edges": edges,
        "journeys": journeys,
        "pings_leave_one_day_out": pings,
    }
    if journeys_error:
        out["holdout"]["error"] = journeys_error

    out["findings"] = findings(out, cleaning, journeys_error)

    dst = Path(__file__).resolve().parent.parent / "docs" / "report.json"
    dst.write_text(json.dumps(out, indent=2))
    print(f"report -> {dst}")
    for line in out["findings"]:
        print(" •", line)


def load_journeys(meta_path):
    """Return (journeys|None, error|None).

    Tolerated failure mode per fix plan E: missing file, corrupt JSON, an
    old-schema file (pre-``n_journeys``), an empty run, or a scoring pass older
    than the model it claims to validate all yield ``journeys: null`` plus the
    explicit error string. The 2 s grace absorbs same-checkout mtimes.
    """
    jp = MODEL / "holdout_journeys.json"
    if not jp.is_file():
        return None, JOURNEYS_STALE_ERROR
    try:
        j = json.loads(jp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, JOURNEYS_STALE_ERROR
    if not isinstance(j, dict) or any(k not in j for k in JOURNEY_KEYS):
        return None, JOURNEYS_STALE_ERROR
    if int(j["n_journeys"]) <= 0:
        return None, JOURNEYS_STALE_ERROR
    if jp.stat().st_mtime < Path(meta_path).stat().st_mtime - 2.0:
        return None, JOURNEYS_STALE_ERROR
    return j, None


def findings(out, cleaning, journeys_error):
    """Human-readable headline stats. No blanket except: inputs are validated."""
    f = []
    days = sorted(cleaning)
    if days:
        tot = sum(cleaning[d]["n_total"] for d in days)
        kept = sum(cleaning[d]["n_kept"] for d in days)
        f.append(f"{len(days)} real October days cleaned: "
                 f"{kept/1e6:.1f}M of {tot/1e6:.1f}M pings kept "
                 f"({100*kept/max(1,tot):.1f}%)")

    pm = out["model"]["ping_speeds"]
    f.append(f"{pm['n_routes']} routes profiled across "
             f"{len(pm['source_days'])} days "
             f"({pm['n_route_hour_cells']} route x hour cells)")

    ho = out["holdout"]["pings_leave_one_day_out"]
    f.append(f"leave-one-day-out route x hour speeds: "
             f"MAE {ho['mae_model_kmh']} km/h "
             f"({ho['mae_model_kmh_weighted']} km/h pair-weighted) vs city "
             f"baseline {ho['mae_city_baseline_kmh']} km/h "
             f"({ho['lift_vs_baseline']}% better), "
             f"n={ho['n_cells_evald']:,} cells")
    if ho["lift_dow_vs_pooled"] is not None:
        f.append(f"day-of-week keying vs pooled: "
                 f"{ho['lift_dow_vs_pooled']}% on the shared subset "
                 f"(n={ho['n_cells_intersection']:,} cells; "
                 f"{ho['lift_dow_vs_pooled_weighted']}% pair-weighted; "
                 f"{ho['lift_dow_vs_pooled_allrows']}% all-rows/asymmetric)")

    jo = out["holdout"]["journeys"]
    if jo is not None:
        cov = jo.get("band_coverage")
        cov_txt = (f", band coverage {int(round(100*cov))}%"
                   if cov is not None else "")
        f.append(f"journey replay (strict Oct 01-24 train / Oct 25-31 score): "
                 f"ride MAE {jo['mae_min']} min on mean ride "
                 f"{jo['mean_ride_min']} min, "
                 f"{int(round(100*jo['within_10_min']))}% within 10 min, "
                 f"n={jo['n_journeys']:,} journeys"
                 f"{cov_txt}")
    elif journeys_error:
        f.append(f"journey replay: unavailable - {journeys_error}")
    return f


if __name__ == "__main__":
    main()
