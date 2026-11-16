"""API invariants, tested offline (G1).

The live half of this suite is ``workers/smoke.mjs``, which probes the
deployed Worker and asserts the same response invariants (options sorted,
eta_p10 <= eta_p50 <= eta_p90, max clamp, note alongside options).  This
file covers everything that can be checked without the network:

  * the shared IST helpers in ``pipeline/common.py`` (hour/dow) and the
    model knobs that every stage imports (MIN_CELL_N, SHRINK_K, DENSE_N);
  * the pure helpers in ``validation/holdout_journeys.py`` (day_of_file,
    band) and its train/test split;
  * the shipped edge asset ``workers/public/model.json``: the frozen field
    set, band ordering, speed/headway plausibility and the pattern-stop
    indices the Worker indexes directly.

Run:  python -m pytest tests/ -q
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from common import (BBOX, DENSE_N, IST_OFFSET_S, MAX_IMPLIED_KMH,  # noqa: E402
                    MIN_CELL_N, SHRINK_K, dow_ist, hour_ist)

MODEL_JSON = ROOT / "workers" / "public" / "model.json"
WORKER_JS = ROOT / "workers" / "worker.js"
API_PY = ROOT / "api" / "app.py"
IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------
# pipeline/common.py — hour + day-of-week in IST
# --------------------------------------------------------------------------
def test_ist_offset_is_five_thirty():
    assert IST_OFFSET_S == 5 * 3600 + 30 * 60 == 19800


def test_hour_ist_matches_ist_clock():
    """The integer helper must read the same wall clock as IST."""
    base = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    for step in range(0, 24 * 4):
        ts = int((base + timedelta(minutes=15 * step)).timestamp())
        assert hour_ist(ts) == (base + timedelta(minutes=15 * step)).astimezone(IST).hour


def test_hour_ist_rolls_over_on_the_hour():
    ts = int(datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc).timestamp())  # 08:30 IST
    assert hour_ist(ts) == 8
    assert hour_ist(ts + 29 * 60) == 8       # 08:59 IST
    assert hour_ist(ts + 30 * 60) == 9       # 09:00 IST
    assert hour_ist(ts + 24 * 3600) == 8     # same hour next day


def test_dow_ist_is_iso_weekday():
    monday = int(datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc).timestamp())
    for i in range(14):
        ts = monday + i * 86400
        expect = datetime.fromtimestamp(ts, IST).isoweekday()
        assert dow_ist(ts) == expect
        assert 1 <= dow_ist(ts) <= 7


def test_hour_dow_ranges_over_a_month():
    start = int(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc).timestamp())
    for ts in range(start, start + 31 * 86400, 1801):
        assert 0 <= hour_ist(ts) <= 23
        assert 1 <= dow_ist(ts) <= 7


def test_bbox_is_delhi():
    lat_min, lng_min, lat_max, lng_max = BBOX
    assert 28.0 < lat_min < lat_max < 29.5
    assert 76.5 < lng_min < lng_max < 78.0


def test_model_knobs_sane():
    assert isinstance(MIN_CELL_N, int) and MIN_CELL_N >= 5
    assert isinstance(SHRINK_K, int) and SHRINK_K > 0
    assert isinstance(DENSE_N, int) and DENSE_N >= MIN_CELL_N
    assert 0 < MAX_IMPLIED_KMH <= 200


@pytest.mark.skipif(not WORKER_JS.exists(), reason="worker.js not present")
def test_js_planner_uses_the_same_ist_offset():
    """G1 drift guard: the edge planner must not invent its own offset."""
    src = WORKER_JS.read_text(encoding="utf-8")
    assert "19800" in src
    assert str(IST_OFFSET_S) in src


@pytest.mark.skipif(not API_PY.exists(), reason="api/app.py not present")
def test_local_api_uses_the_same_ist_offset():
    src = API_PY.read_text(encoding="utf-8")
    assert str(IST_OFFSET_S) in src


# --------------------------------------------------------------------------
# validation/holdout_journeys.py — pure helpers + split integrity
# --------------------------------------------------------------------------
def _holdout():
    pytest.importorskip("duckdb")
    pytest.importorskip("pandas")
    sys.path.insert(0, str(ROOT / "validation"))
    import holdout_journeys  # noqa: WPS433
    return holdout_journeys


def test_holdout_day_of_file_round_trip():
    hj = _holdout()
    assert hj.day_of_file("traversals_2026_10_01.parquet") == "2026-10-01"
    assert hj.day_of_file("traversals_2026_10_16.parquet") == "2026-10-16"
    assert hj.day_of_file("traversals_2026_10_31.parquet") == "2026-10-31"


def _assert_contiguous_partition(bins, lo_expected, hi_expected):
    """Bins are [lo, hi) pairs: ordered, non-overlapping, gapless."""
    assert bins, "no bins defined"
    assert bins[0][0] == lo_expected
    assert bins[-1][1] == hi_expected
    for (lo, hi), (next_lo, _) in zip(bins, bins[1:]):
        assert lo < hi, f"empty bin {lo}..{hi}"
        assert next_lo == hi, f"gap or overlap between {hi} and {next_lo}"


def test_holdout_bands_partition_hours_and_hops():
    """Labels are derived from these bins; the bins must cover every value."""
    hj = _holdout()
    _assert_contiguous_partition(hj.HOUR_BANDS, 0, 24)
    _assert_contiguous_partition(hj.LEN_BANDS, 5, 41)
    assert len(hj.HOUR_BANDS) == 5
    assert len(hj.LEN_BANDS) == 3


def test_holdout_split_does_not_overlap():
    hj = _holdout()
    assert hj.TRAIN_END < hj.TEST_START, "train window must end before test starts"
    assert hj.TRAIN_END.startswith("2026-10-")
    assert hj.TEST_START.startswith("2026-10-")
    assert hj.MAX_JOURNEYS > 0


@pytest.mark.skipif(
    not (ROOT / "model" / "holdout_journeys.json").exists(),
    reason="holdout output not generated on this machine")
def test_holdout_output_ranges_if_present():
    """Tolerate schema growth (D adds band_coverage/p10/p90 passthrough)."""
    out = json.loads((ROOT / "model" / "holdout_journeys.json").read_text())
    for key in ("n_journeys", "n_samples", "n_scored"):
        if key in out:
            assert isinstance(out[key], int) and out[key] >= 0
    for key in ("mae_min", "ride_mae_min", "bias_min", "ride_bias_min"):
        if key in out:
            assert out[key] == out[key]      # not NaN
    for key in ("band_coverage", "coverage", "within_5_min",
                "within_10_min", "fallback_leg_share"):
        if key in out:
            assert 0.0 <= float(out[key]) <= 1.0, f"{key} outside [0, 1]"


# --------------------------------------------------------------------------
# workers/public/model.json — the asset the deployed API answers from
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def model():
    if not MODEL_JSON.exists():
        pytest.skip("workers/public/model.json not present")
    return json.loads(MODEL_JSON.read_text(encoding="utf-8"))


def test_model_has_the_frozen_fields(model):
    required = {"stops", "patterns", "speeds", "route_avg", "route_names",
                "city_hour", "headway", "membership"}
    missing = required - set(model)
    assert not missing, f"model.json is missing contract fields: {sorted(missing)}"


def test_model_route_layers_agree(model):
    assert len(model["route_avg"]) > 2000          # 2,245 route-hour routes
    assert set(model["speeds"]) == set(model["route_avg"])
    assert len(model["patterns"]) > 2000
    assert len(model["stops"]) > 5000


def test_city_hour_is_a_full_day(model):
    hours = model["city_hour"]
    assert set(hours) == {str(h) for h in range(24)}
    for h, v in hours.items():
        assert 0 < float(v) <= MAX_IMPLIED_KMH, f"city_hour[{h}] = {v}"


def test_band_ratios_are_ordered(model):
    band = model.get("band", {})
    assert band, "band is required for eta_p10/p90 (A3)"
    los, his = [], []
    for route, per_hour in band.items():
        for hour, pair in per_hour.items():
            assert 0 <= int(hour) <= 23
            lo, hi = float(pair[0]), float(pair[1])
            assert 0.0 < lo <= hi, f"band {route}@{hour} = {pair}"
            assert lo <= 1.05, f"band {route}@{hour} p10 ratio {lo} > 1"
            assert hi <= 6.0, f"band {route}@{hour} p90 ratio {hi} > 6"
            los.append(lo)
            his.append(hi)
    los.sort()
    his.sort()
    assert los[len(los) // 2] <= 1.0, "median p10 ratio should not exceed 1"
    assert his[len(his) // 2] >= 1.0, "median p90 ratio should not be below 1"


def test_speeds_are_positive_and_hourly(model):
    """Medians must be plausible; a few degenerate cells exceed the ping cap."""
    values = []
    for route, per_hour in model["speeds"].items():
        for hour, kmh in per_hour.items():
            assert 0 <= int(hour) <= 23, f"{route}@{hour}"
            assert 0 < float(kmh), f"{route}@{hour} = {kmh}"
            values.append(float(kmh))
    for route, kmh in model["route_avg"].items():
        assert 0 < float(kmh) < 3600, f"route_avg[{route}] = {kmh}"
        values.append(float(kmh))
    values.sort()
    assert 5.0 < values[len(values) // 2] < 50.0, "median speed is implausible"
    fast = sum(1 for v in values if v > MAX_IMPLIED_KMH)
    assert fast / len(values) < 0.005, f"{fast} of {len(values)} cells exceed the ping cap"


def test_headways_are_positive_seconds(model):
    for route, per_hour in model["headway"].items():
        for hour, sec in per_hour.items():
            assert 0 <= int(hour) <= 23
            assert 0 < float(sec) <= 86400, f"headway {route}@{hour} = {sec}"


def test_usual_hour_and_detour_are_in_range(model):
    for route, hour in model.get("usual_hour", {}).items():
        assert isinstance(hour, int) and 0 <= hour <= 23, f"{route}: {hour}"
    for route, factor in model.get("detour", {}).items():
        assert 0.9 <= float(factor) <= 3.0, f"detour[{route}] = {factor}"


def test_pattern_stops_exist(model):
    stops = model["stops"]
    for i, pat in enumerate(model["patterns"]):
        assert pat["r"], f"pattern {i} has no route"
        assert len(pat["s"]) >= 2, f"pattern {i} has fewer than 2 stops"
        for sid in pat["s"]:
            assert sid in stops, f"pattern {i} references unknown stop {sid}"
        assert pat["r"] in model["route_names"], f"pattern {i} route lacks a name"


def test_membership_indices_resolve(model):
    """Worker indexes M.patterns[pi].s[si] straight from membership."""
    patterns, stamps = model["patterns"], model["stops"]
    for sid, pairs in model["membership"].items():
        assert sid in stamps, f"membership for unknown stop {sid}"
        for pi, si in pairs:
            assert 0 <= pi < len(patterns), f"stop {sid}: pattern index {pi}"
            assert 0 <= si < len(patterns[pi]["s"]), f"stop {sid}: stop index {si}"
            assert patterns[pi]["s"][si] == sid, f"stop {sid} -> ({pi}, {si}) mismatch"


def test_stop_coordinates_and_names(model):
    for sid, stop in model["stops"].items():
        lat, lng, name = stop[0], stop[1], stop[2]
        assert 27.0 < lat < 30.0, f"stop {sid} lat {lat}"
        assert 74.5 < lng < 79.5, f"stop {sid} lng {lng}"
        assert isinstance(name, str) and name.strip(), f"stop {sid} has no name"


def test_edge_count_is_an_int(model):
    if "edge_count" in model:
        assert isinstance(model["edge_count"], int)
        assert model["edge_count"] > 0
