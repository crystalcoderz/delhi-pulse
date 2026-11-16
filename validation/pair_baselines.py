"""
Pair baselines (backlog F5): observed bus ride medians + GTFS timetable
minima for the 20 canonical OD pairs, per dep hour.

Next to the model estimate (docs/od_comparison.json) and Google's driving
time, the judged pairs need two more numbers that come from the ground
truth itself:

  observed  — the median ride time of REAL bus trips we saw serving both
              mapped stops, boarding in the requested IST hour on a
              weekday. Built from stage-03 passages (the same events the
              model trains on), across every passages_*.parquet day.
  scheduled — the fastest time the GTFS timetable PROMISES between the
              same two stops: same trip_id, stop_sequence increasing,
              scheduled delta > 0, weekday service, trip departing the
              origin stop inside the same IST hour.

Both numbers are pure in-vehicle time, origin stop to destination stop:
no access walk, no wait. The fenced contract for the JSON:

  docs/pair_baselines.json
    meta: filters + provenance
    pairs: "<origin> -> <destination>" -> {origin_stop, destination_stop,
      dep_hours: {"9": {observed_ride: {n, p50_s, p10_s, p90_s, min_s,
                                        max_s, boot_ci95_s, boot_iters},
                        top_route:   {route_id, route_code, route_name,
                                      n, p50_s, share} | null,
                        scheduled:   {n_trips, min_s, median_s,
                                      fastest_route_id,
                                      fastest_route_code} | null,
                        mapping:     {pair_tier, n_obs_at_pair,
                                      n_combos_with_obs,
                                      combos_evaluated}}}}

Stop mapping (F5 "stop mapping") is pair-aware, not per-endpoint:
  1. enumerate every GTFS stop within 1500 m of each landmark
     (all origin x destination combinations per pair);
  2. score every combination by what actually connects the two landmarks
     in the analysis window — observed bus trips riding it (n_obs) and
     weekday scheduled trips serving it (n_sched);
  3. pick the nearest combination that carries real service:
        tier A: n_obs >= 5 (a median that means something)
        tier B: any observed trip (n_obs >= 1)
        tier C: scheduled service only (n_sched >= 1)
        tier D: nearest stops regardless of service (reported as-is)
     "nearest" = origin distance + destination distance, tie-broken by
     observed count. Both numbers for a pair are always computed at the
     SAME stop pair, and the chosen stops + distances are in the JSON.

Observed ride matching (at the chosen stop pair): the same day+trip_id+
    vehicle_id+route_id vehicle-trip passes the origin stop inside the
    IST hour window and the destination stop afterwards — first
    alighting after the boarding, later in stop order, 0 < ride <= 120
    min. Each matched ride is then VERIFIED against the trip's full
    trajectory (a second, heavily pruned passages scan): if any passage
    gap larger than 30 minutes occurs strictly between boarding and
    alighting, the ride is discarded — that pattern is a layover/deadhead
    or an out-and-back label reuse, not a passenger ride. This keeps
    60-90 minute cross-city rides (which a naive candidate-only gap split
    would have broken in half) while killing turnaround contamination.
    Ride medians are quantile_cont; the bootstrap CI of the median is
    computed inside DuckDB (fixed-seed resampling) so only summary
    numbers ever leave the process.

Scheduled ride matching: same trip_id (=> same route), stop_sequence
    increasing, delta > 0, weekday service via calendar flags, origin
    departure inside the IST hour window (e.g. 09:00:00-09:59:59 IST).

Read-only: parquet/CSV inputs are never written; only docs/
pair_baselines.json is produced. Run:
    .venv/Scripts/python.exe validation/pair_baselines.py [--hours 9]
"""
import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
from common import CLEAN, GTFS, dow_ist_expr, hour_ist_expr  # noqa: E402
from validation.od_comparison import OD_PAIRS  # noqa: E402

OUT = ROOT / "docs" / "pair_baselines.json"

CAND_RADIUS_M = 1500.0    # candidate stops considered around a landmark;
                          # ALL stops within this radius are enumerated as
                          # combinations (tiers pick the nearest serviceable)
PAIR_TIER_A_N = 5         # tier A: enough observed trips for a median
MAX_RIDE_MIN = 120        # sanity cap on an observed ride (label reuse)
MIDRIDE_GAP_S = 1800.     # observed ride dies if a >30min passage gap
                          # occurs between boarding and alighting
BOOT_ITERS_DEFAULT = 400  # bootstrap resamples for the median CI
BOOT_SEED = 0.5           # setseed() bound: -1..1
R_EARTH_M = 6_371_000.0
# keep regexes OUT of f-strings — the {2}/{4} below are regex quantifier
# braces, not format placeholders.
DAY_RE = "passages_([0-9]{4}_[0-9]{2}_[0-9]{2})"


def log(*a):
    print(*a, flush=True)


def sql_file_list(paths):
    return ", ".join(f"'{str(p).replace(chr(92), '/')}'" for p in paths)


def hav_m(lat0, lng0, lats, lngs):
    la0, lo0 = np.radians(lat0), np.radians(lng0)
    la, lo = np.radians(lats), np.radians(lngs)
    a = (np.sin((la - la0) / 2) ** 2
         + np.cos(la0) * np.cos(la) * np.sin((lo - lo0) / 2) ** 2)
    return 2 * R_EARTH_M * np.arcsin(np.sqrt(a))


def parse_hours(s):
    hs = sorted({int(x) for x in s.split(",") if x.strip() != ""})
    if not hs or any(h < 0 or h > 23 for h in hs):
        raise SystemExit(f"bad --hours {s!r}: want e.g. '9' or '9,15'")
    return hs


def hhmmss(col):
    return (f"TRY_CAST(split_part({col}, ':', 1) AS INT) * 3600 "
            f"+ TRY_CAST(split_part({col}, ':', 2) AS INT) * 60 "
            f"+ TRY_CAST(split_part({col}, ':', 3) AS INT)")


def pair_candidates(stops):
    """Per pair: every GTFS stop within CAND_RADIUS_M of each landmark.

    Returns (cand_rows, cand_by_key):
      cand_rows: DataFrame [pair_ix, side, stop_id, stop_name, dist_m]
      cand_by_key: {(pair_ix, side): [(stop_id, dist_m), ...]} ordered by
                   distance (per landmark; shared landmarks keep one list)
    """
    lats = stops.stop_lat.to_numpy(float)
    lngs = stops.stop_lon.to_numpy(float)
    rows, by_key = [], {}
    for ix, (oname, dname, olat, olng, dlat, dlng) in enumerate(OD_PAIRS):
        for side, (la, lo) in (("o", (olat, olng)), ("d", (dlat, dlng))):
            d = hav_m(la, lo, lats, lngs)
            sel = np.argsort(d, kind="stable")
            picked = [i for i in sel if d[i] <= CAND_RADIUS_M]
            lst = []
            for i in picked:
                sid, snm = str(stops.stop_id.iloc[i]), \
                    str(stops.stop_name.iloc[i])
                rows.append((ix, side, sid, snm, round(float(d[i]), 1)))
                lst.append((sid, round(float(d[i]), 1)))
            by_key[(ix, side)] = lst
    return pd.DataFrame(rows, columns=["pair_ix", "side", "stop_id",
                                       "stop_name", "dist_m"]), by_key


def choose_combo(obs_df, sched_df, by_key, pair_ix):
    """Tiered pick of the (origin, destination) stop pair to report.

    obs_df columns: o_sid, d_sid, n_obs | sched_df: o_sid, d_sid, n_sched
    """
    dist_o = {s: dm for (s, dm) in by_key[(pair_ix, "o")]}
    dist_d = {s: dm for (s, dm) in by_key[(pair_ix, "d")]}
    obs = {(r.o_sid, r.d_sid): int(r.n_obs) for r in obs_df.itertuples()}
    sch = {(r.o_sid, r.d_sid): int(r.n_sched) for r in sched_df.itertuples()}

    combos = [(a, b) for a in dist_o for b in dist_d]

    def sort_key(cb):
        o, d = cb
        return (dist_o[o] + dist_d[d],   # nearest first
                -obs.get(cb, 0),          # then more observed trips
                -sch.get(cb, 0),
                str(o), str(d))           # determinism

    for tier, keep in (
            ("A", lambda cb: obs.get(cb, 0) >= PAIR_TIER_A_N),
            ("B", lambda cb: obs.get(cb, 0) >= 1),
            ("C", lambda cb: sch.get(cb, 0) >= 1),
    ):
        ok = [cb for cb in combos if keep(cb)]
        if ok:
            cb = sorted(ok, key=sort_key)[0]
            return cb, tier
    cb = sorted(combos, key=sort_key)[0]
    return cb, "D"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", default="9",
                    help="IST departure hour(s), comma-separated (default 9)")
    ap.add_argument("--memory", default="6GB")
    ap.add_argument("--boot-iters", type=int, default=BOOT_ITERS_DEFAULT)
    ap.add_argument("--limit-files", type=int, default=0,
                    help="testing only: use first N passages files")
    a = ap.parse_args()
    hours = parse_hours(a.hours)

    files = sorted(CLEAN.glob("passages_*.parquet"))
    if a.limit_files:
        files = files[:a.limit_files]
    if not files:
        raise SystemExit("no data/clean/passages_*.parquet found")
    f_list = sql_file_list(files)
    log(f"[pb] {len(files)} passages files "
        f"({files[0].stem.replace('passages_', '')}"
        f"..{files[-1].stem.replace('passages_', '')}), hours={hours}")

    stops = pd.read_csv(GTFS / "stops.txt", dtype={"stop_id": str})
    routes_df = pd.read_csv(GTFS / "routes.txt", dtype=str)
    routes = routes_df.set_index("route_id")

    def route_info(rid):
        rid = str(rid)
        if rid in routes.index:
            r = routes.loc[rid]
            if isinstance(r, pd.DataFrame):
                r = r.iloc[0]
            return {"route_id": rid,
                    "route_code": str(r.route_short_name),
                    "route_name": str(r.route_desc or r.route_long_name)}
        return {"route_id": rid, "route_code": None, "route_name": None}

    # ---- candidate stops per pair (geometry first, service decides) -----
    pcand, by_key = pair_candidates(stops)
    _sizes = [len(v) for v in by_key.values()]
    log(f"[pb] candidate stops: {len(pcand)} rows "
        f"(every stop within {CAND_RADIUS_M:.0f} m of a landmark; "
        f"per-side rings of {min(_sizes)}-{max(_sizes)} stops)")

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{a.memory}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SELECT setseed({BOOT_SEED})").fetchall()

    h_expr = hour_ist_expr("tsec")
    d_expr = dow_ist_expr("tsec")

    # ---- scan 1: weekday passages at any candidate stop ------------------
    con.register("pcand_rel", pcand[["pair_ix", "side", "stop_id", "dist_m"]])
    all_ids = pd.DataFrame({"stop_id": sorted(pcand.stop_id.unique())})
    con.register("pool_ids", all_ids)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pool AS
        SELECT day, trip_id, vehicle_id, route_id, stop_id, stop_seq,
               tsec, h_ist
        FROM (
            SELECT regexp_extract(filename, '{DAY_RE}', 1) AS day,
                   COALESCE(trip_id, '-') AS trip_id,
                   COALESCE(vehicle_id, '-') AS vehicle_id,
                   route_id, stop_id, CAST(stop_seq AS BIGINT) AS stop_seq,
                   tsec, {h_expr} AS h_ist, {d_expr} AS dow
            FROM read_parquet([{f_list}], filename = true)
            WHERE stop_id IN (SELECT stop_id FROM pool_ids)
        )
        WHERE dow BETWEEN 1 AND 5 AND day IS NOT NULL AND day <> ''
    """)
    n_pool = con.execute("SELECT COUNT(*) FROM pool").fetchone()[0]
    n_days = con.execute("SELECT COUNT(DISTINCT day) FROM pool").fetchone()[0]
    log(f"[pb] weekday passages pool: {n_pool:,} rows over {n_days} days")

    # ---- GTFS schedule side (once, over candidate stops) -----------------
    st_path = str(GTFS / "stop_times.txt").replace(chr(92), "/")
    tr_path = str(GTFS / "trips.txt").replace(chr(92), "/")
    ca_path = str(GTFS / "calendar.txt").replace(chr(92), "/")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE gtrips AS
        SELECT t.trip_id, CAST(t.route_id AS VARCHAR) AS route_id,
               (c.monday = 1 OR c.tuesday = 1 OR c.wednesday = 1
                OR c.thursday = 1 OR c.friday = 1) AS weekday_svc
        FROM read_csv_auto('{tr_path}') t
        JOIN read_csv_auto('{ca_path}') c ON t.service_id = c.service_id
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE stimes AS
        SELECT trip_id, CAST(stop_id AS VARCHAR) AS stop_id,
               CAST(stop_sequence AS BIGINT) AS stop_sequence,
               {hhmmss('arrival_time')} AS arr_s,
               {hhmmss('departure_time')} AS dep_s
        FROM read_csv_auto('{st_path}')
        WHERE {hhmmss('departure_time')} IS NOT NULL
          AND CAST(stop_id AS VARCHAR) IN (SELECT stop_id FROM pool_ids)
    """)
    n_st = con.execute("SELECT COUNT(*) FROM stimes").fetchone()[0]
    log(f"[pb] GTFS stop_times rows at candidate stops: {n_st:,}")

    name_of = {str(r.stop_id): str(r.stop_name)
               for r in stops[["stop_id", "stop_name"]].itertuples()}

    # ---- per pair x hour: combos -> chosen stop pair + candidate rides ----
    con.execute("""
        CREATE OR REPLACE TEMP TABLE chosen_rides (
            pair_ix INTEGER, hour INTEGER, o_sid VARCHAR, d_sid VARCHAR,
            day VARCHAR, trip_id VARCHAR, vehicle_id VARCHAR,
            route_id VARCHAR, o_tsec DOUBLE, d_tsec DOUBLE, dur_s DOUBLE)
    """)
    results = {}
    for ix, (oname, dname, olat, olng, dlat, dlng) in enumerate(OD_PAIRS):
        pair_out = None
        for h in hours:
            lo_s, hi_s = h * 3600, (h + 1) * 3600

            # 1. observed trip counts per candidate stop pair (weekday, h).
            #    Side-filtered subqueries keep the a x b cross at the
            #    stops of THIS pair (pool holds every pair's candidates).
            obs_df = con.execute(f"""
                WITH a AS (
                    SELECT p.* FROM pool p JOIN pcand_rel c
                      ON  p.stop_id = c.stop_id
                      AND c.pair_ix = {ix} AND c.side = 'o'
                ), b AS (
                    SELECT p.* FROM pool p JOIN pcand_rel c
                      ON  p.stop_id = c.stop_id
                      AND c.pair_ix = {ix} AND c.side = 'd'
                ), j AS (
                    SELECT a.stop_id AS o_sid, b.stop_id AS d_sid,
                           a.day, a.trip_id, a.vehicle_id, a.route_id,
                           b.tsec - a.tsec AS dur_s
                    FROM a JOIN b
                      ON  a.day = b.day AND a.trip_id = b.trip_id
                      AND a.vehicle_id = b.vehicle_id
                      AND a.route_id = b.route_id
                    WHERE a.h_ist = {h}
                      AND b.stop_seq > a.stop_seq
                      AND b.tsec > a.tsec
                      AND b.tsec - a.tsec <= {MAX_RIDE_MIN * 60}
                    QUALIFY row_number() OVER (
                        PARTITION BY a.day, a.trip_id, a.vehicle_id,
                                     a.route_id, a.stop_id, b.stop_id
                        ORDER BY b.tsec - a.tsec) = 1
                )
                SELECT o_sid, d_sid, COUNT(*) AS n_obs
                FROM j GROUP BY 1, 2
            """).df()

            # 2. scheduled trip counts per candidate stop pair (weekday, h)
            sched_df = con.execute(f"""
                WITH o_st AS (
                    SELECT s.* FROM stimes s JOIN pcand_rel c
                      ON  s.stop_id = c.stop_id
                      AND c.pair_ix = {ix} AND c.side = 'o'
                ), d_st AS (
                    SELECT s.* FROM stimes s JOIN pcand_rel c
                      ON  s.stop_id = c.stop_id
                      AND c.pair_ix = {ix} AND c.side = 'd'
                ), td AS (
                    SELECT o.stop_id AS o_sid, d.stop_id AS d_sid,
                           o.trip_id, g.route_id,
                           MIN(d.arr_s - o.dep_s) AS dur
                    FROM o_st o
                    JOIN d_st d
                      ON  d.trip_id = o.trip_id
                      AND d.stop_sequence > o.stop_sequence
                    JOIN gtrips g ON g.trip_id = o.trip_id
                    WHERE g.weekday_svc
                      AND o.dep_s >= {lo_s} AND o.dep_s < {hi_s}
                      AND d.arr_s - o.dep_s > 0
                    GROUP BY 1, 2, 3, 4
                )
                SELECT o_sid, d_sid, COUNT(*) AS n_sched
                FROM td GROUP BY 1, 2
            """).df()

            # 3. tiered pick of the stop pair to report
            (o_sid, d_sid), tier = choose_combo(obs_df, sched_df,
                                                by_key, ix)
            dist_o = dict(by_key[(ix, "o")])[o_sid]
            dist_d = dict(by_key[(ix, "d")])[d_sid]

            # 4. candidate rides at the chosen pair (verified in scan 2)
            con.execute(f"""
                INSERT INTO chosen_rides
                SELECT * FROM (
                    SELECT {ix} AS pair_ix, {h} AS hour,
                           '{o_sid}' AS o_sid, '{d_sid}' AS d_sid,
                           a.day, a.trip_id, a.vehicle_id, a.route_id,
                           a.tsec AS o_tsec, b.tsec AS d_tsec,
                           b.tsec - a.tsec AS dur_s
                    FROM pool a
                    JOIN pool b
                      ON  a.day = b.day AND a.trip_id = b.trip_id
                      AND a.vehicle_id = b.vehicle_id
                      AND a.route_id = b.route_id
                    WHERE a.stop_id = '{o_sid}'
                      AND b.stop_id = '{d_sid}'
                      AND a.h_ist = {h}
                      AND b.stop_seq > a.stop_seq
                      AND b.tsec > a.tsec
                      AND b.tsec - a.tsec <= {MAX_RIDE_MIN * 60}
                    QUALIFY row_number() OVER (
                        PARTITION BY a.day, a.trip_id, a.vehicle_id,
                                     a.route_id
                        ORDER BY b.tsec - a.tsec) = 1
                )
            """)

            # 5. scheduled stats at the chosen stop pair
            sch = con.execute(f"""
                WITH td AS (
                    SELECT o.trip_id, g.route_id,
                           MIN(d.arr_s - o.dep_s) AS dur
                    FROM stimes o
                    JOIN stimes d
                      ON  d.trip_id = o.trip_id
                      AND d.stop_sequence > o.stop_sequence
                    JOIN gtrips g ON g.trip_id = o.trip_id
                    WHERE o.stop_id = '{o_sid}' AND d.stop_id = '{d_sid}'
                      AND g.weekday_svc
                      AND o.dep_s >= {lo_s} AND o.dep_s < {hi_s}
                      AND d.arr_s - o.dep_s > 0
                    GROUP BY 1, 2
                )
                SELECT COUNT(*), MIN(dur), quantile_cont(dur, 0.5),
                       arg_min(route_id, dur)
                FROM td
            """).fetchone()
            if sch and sch[0]:
                ri = route_info(sch[3])
                scheduled = {
                    "n_trips": int(sch[0]),
                    "min_s": round(float(sch[1]), 1),
                    "median_s": round(float(sch[2]), 1),
                    "fastest_route_id": ri["route_id"],
                    "fastest_route_code": ri["route_code"],
                }
            else:
                scheduled = None

            obs_map = {(r.o_sid, r.d_sid): int(r.n_obs)
                       for r in obs_df.itertuples()}
            hour_out = {
                "observed_ride": None,          # filled after scan 2
                "top_route": None,
                "scheduled": scheduled,
                "mapping": {
                    "pair_tier": tier,
                    "n_obs_at_pair": obs_map.get((o_sid, d_sid), 0),
                    "n_combos_with_obs": len(obs_df),
                    "combos_evaluated": len(by_key[(ix, "o")])
                                        * len(by_key[(ix, "d")]),
                },
            }
            if pair_out is None:
                pair_out = {
                    "origin_stop": {"stop_id": o_sid,
                                    "stop_name": name_of[o_sid],
                                    "dist_m": dist_o},
                    "destination_stop": {"stop_id": d_sid,
                                         "stop_name": name_of[d_sid],
                                         "dist_m": dist_d},
                    "dep_hours": {},
                }
            elif (pair_out["origin_stop"]["stop_id"] != o_sid
                  or pair_out["destination_stop"]["stop_id"] != d_sid):
                # a later --hours value could map elsewhere; keep the
                # pair-level stops from the first hour and record this
                # hour's own stops inside its hour block.
                hour_out["mapping"]["origin_stop"] = {
                    "stop_id": o_sid, "stop_name": name_of[o_sid],
                    "dist_m": dist_o}
                hour_out["mapping"]["destination_stop"] = {
                    "stop_id": d_sid, "stop_name": name_of[d_sid],
                    "dist_m": dist_d}
            pair_out["dep_hours"][str(h)] = hour_out

        results[f"{oname} -> {dname}"] = pair_out

    n_chosen = con.execute("SELECT COUNT(*) FROM chosen_rides").fetchone()[0]
    log(f"[pb] candidate rides at chosen stop pairs: {n_chosen:,}")

    # ---- scan 2: verify rides against the trips' FULL trajectories ------
    # Only the matched (day, trip_id, vehicle_id, route_id) keys are read
    # out of the month of passages (SEMI JOIN prunes the scan); a ride
    # survives only if no >30 min passage gap happens strictly inside it.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE vgap AS
        WITH raw AS (
            SELECT regexp_extract(filename, '{DAY_RE}', 1) AS day,
                   COALESCE(trip_id, '-') AS trip_id,
                   COALESCE(vehicle_id, '-') AS vehicle_id,
                   route_id, tsec
            FROM read_parquet([{f_list}], filename = true)
        ), keys AS (
            SELECT DISTINCT day, trip_id, vehicle_id, route_id
            FROM chosen_rides
        ), sel AS (
            SELECT r.* FROM raw r
            SEMI JOIN keys k
              ON  r.day = k.day AND r.trip_id = k.trip_id
              AND r.vehicle_id = k.vehicle_id AND r.route_id = k.route_id
        ), w AS (
            SELECT day, trip_id, vehicle_id, route_id, tsec,
                   tsec - LAG(tsec) OVER wnd AS tgap
            FROM sel
            WINDOW wnd AS (PARTITION BY day, trip_id, vehicle_id, route_id
                           ORDER BY tsec)
        )
        SELECT c.pair_ix, c.hour, c.o_sid, c.d_sid,
               c.day, c.trip_id, c.vehicle_id, c.route_id,
               MAX(w.tgap) AS mid_gap
        FROM chosen_rides c
        JOIN w
          ON  w.day = c.day AND w.trip_id = c.trip_id
          AND w.vehicle_id = c.vehicle_id AND w.route_id = c.route_id
          AND w.tsec > c.o_tsec AND w.tsec < c.d_tsec
        GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE rides_ok AS
        SELECT c.*
        FROM chosen_rides c
        LEFT JOIN vgap v
          ON  c.pair_ix = v.pair_ix AND c.hour = v.hour
          AND c.o_sid = v.o_sid AND c.d_sid = v.d_sid
          AND c.day = v.day AND c.trip_id = v.trip_id
          AND c.vehicle_id = v.vehicle_id AND c.route_id = v.route_id
        WHERE v.mid_gap IS NULL OR v.mid_gap <= {MIDRIDE_GAP_S}
    """)
    n_ok = con.execute("SELECT COUNT(*) FROM rides_ok").fetchone()[0]
    log(f"[pb] rides surviving the mid-ride layover check: "
        f"{n_ok:,}/{n_chosen:,}")

    # ---- final observed stats per pair x hour ----------------------------
    for ix, (oname, dname, *_rest) in enumerate(OD_PAIRS):
        pair_out = results[f"{oname} -> {dname}"]
        for h in hours:
            hour_out = pair_out["dep_hours"][str(h)]

            (n, p50, p10, p90, dmin, dmax) = con.execute(f"""
                SELECT COUNT(*), quantile_cont(dur_s, 0.5),
                       quantile_cont(dur_s, 0.1),
                       quantile_cont(dur_s, 0.9),
                       MIN(dur_s), MAX(dur_s)
                FROM rides_ok
                WHERE pair_ix = {ix} AND hour = {h}
            """).fetchone()
            observed = {"n": int(n)}
            if n:
                ci = con.execute(f"""
                    WITH arr AS (
                        SELECT list(dur_s ORDER BY dur_s) AS xs,
                               COUNT(*) AS nsz
                        FROM rides_ok
                        WHERE pair_ix = {ix} AND hour = {h}
                    ), picks AS (
                        SELECT r.i AS rep,
                               list_extract(a.xs,
                                   (random() * a.nsz)::INTEGER + 1) AS dur_s
                        FROM arr a, range(1, {a.boot_iters + 1}) r(i),
                             generate_series(1, a.nsz) g(k)
                    ), boots AS (
                        SELECT rep, quantile_cont(dur_s, 0.5) AS m
                        FROM picks GROUP BY rep
                    )
                    SELECT quantile_cont(m, 0.025), quantile_cont(m, 0.975)
                    FROM boots
                """).fetchone()
                observed.update({
                    "p50_s": round(float(p50), 1),
                    "p10_s": round(float(p10), 1),
                    "p90_s": round(float(p90), 1),
                    "min_s": round(float(dmin), 1),
                    "max_s": round(float(dmax), 1),
                    "boot_ci95_s": [round(float(ci[0]), 1),
                                    round(float(ci[1]), 1)],
                    "boot_iters": int(a.boot_iters),
                })
            else:
                observed.update({"p50_s": None})
            hour_out["observed_ride"] = observed

            # top observed route at the chosen pair (n >= PAIR_TIER_A_N)
            top_route = None
            if n:
                tr = con.execute(f"""
                    SELECT route_id, COUNT(*) AS rn,
                           quantile_cont(dur_s, 0.5) AS rp50
                    FROM rides_ok
                    WHERE pair_ix = {ix} AND hour = {h}
                    GROUP BY 1 HAVING COUNT(*) >= {PAIR_TIER_A_N}
                    ORDER BY rn DESC, rp50 LIMIT 1
                """).fetchone()
                if tr:
                    top_route = route_info(tr[0])
                    top_route.update({"n": int(tr[1]),
                                      "p50_s": round(float(tr[2]), 1),
                                      "share": round(int(tr[1]) / n, 3)})
            hour_out["top_route"] = top_route

        h0 = str(hours[0])
        r0 = pair_out["dep_hours"][h0]
        obs, sch, top = (r0["observed_ride"], r0["scheduled"],
                         r0["top_route"])
        o_st, d_st = pair_out["origin_stop"], pair_out["destination_stop"]
        obs_txt = (f"obs p50 {obs['p50_s']/60:.1f}m"
                   f" (n={obs['n']}, CI "
                   f"{obs['boot_ci95_s'][0]/60:.1f}-"
                   f"{obs['boot_ci95_s'][1]/60:.1f}m)"
                   if obs["n"] else "obs none")
        top_txt = (f"top {top['route_code']} n={top['n']}"
                   if top else "top -")
        sch_txt = (f"sched min {sch['min_s']/60:.1f}m"
                   f" ({sch['n_trips']} trips,"
                   f" {sch['fastest_route_code']})"
                   if sch else "sched none")
        log(f"  {oname} -> {dname}")
        log(f"    o={o_st['stop_name'][:40]} ({o_st['dist_m']:.0f}m)"
            f" d={d_st['stop_name'][:40]} ({d_st['dist_m']:.0f}m)"
            f" tier={r0['mapping']['pair_tier']}")
        log(f"    {obs_txt} | {top_txt} | {sch_txt}")

    # ---- output -----------------------------------------------------------
    n_obs = sum(1 for r in results.values()
                if any(v["observed_ride"]["n"]
                       for v in r["dep_hours"].values()))
    weekday_days = []
    for f in files:
        stem = f.stem.replace("passages_", "")
        dstr = f"{stem[:4]}-{stem[5:7]}-{stem[8:10]}"
        try:
            if date.fromisoformat(dstr).weekday() < 5:
                weekday_days.append(dstr)
        except ValueError:
            pass

    out = {
        "meta": {
            "generated_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "script": "validation/pair_baselines.py",
            "description": ("Observed bus ride-time medians (stage-03 "
                            "passages) and GTFS timetable minima for the "
                            "20 OD pairs. Both are origin-stop to "
                            "destination-stop in-vehicle times: no access "
                            "walk, no wait."),
            "od_pairs": len(OD_PAIRS),
            "dep_hours_ist": hours,
            "filters": {
                "weekday_only": True,
                "weekdays_ist": "Mon-Fri (dow 1-5)",
                "observed_origin_passage_hour":
                    "same IST hour as departure",
                "observed_match": ("same (day, trip_id, vehicle_id, "
                                   "route_id); first alighting after the "
                                   "boarding; stop_seq increasing; "
                                   "delta > 0; ride <= "
                                   f"{MAX_RIDE_MIN} min"),
                "observed_verification": (f"a ride is dropped if any "
                                          f"passage gap > {MIDRIDE_GAP_S:.0f}"
                                          " s occurs strictly between "
                                          "boarding and alighting "
                                          "(mid-ride layover / out-and-"
                                          "back label reuse)"),
                "scheduled_match": ("same trip_id (=> same route); "
                                    "stop_sequence increasing; delta > 0; "
                                    "weekday service via calendar; origin "
                                    "departure within the IST hour"),
                "hour_bin":
                    "integer arithmetic ((tsec+19800)//3600)%24",
            },
            "stop_mapping": {
                "rule": (f"pair-aware: every GTFS stop within "
                         f"{CAND_RADIUS_M:.0f} m of each landmark is "
                         "enumerated as origin x destination combinations; "
                         "the nearest combination with real service is "
                         "used. Tiers: A >= "
                         f"{PAIR_TIER_A_N} observed trips, B >= 1 "
                         "observed, C scheduled only, D nearest "
                         "regardless. Both numbers for a pair are "
                         "computed at the same stop pair"),
                "tier_a_min_obs": PAIR_TIER_A_N,
                "candidate_radius_m": CAND_RADIUS_M,
            },
            "stats": {
                "median": "quantile_cont (linear interpolation)",
                "bootstrap_ci": (f"{a.boot_iters} resamples of the "
                                "median, percentile 2.5/97.5, fixed seed"),
            },
            "top_route_min_n": PAIR_TIER_A_N,
            "inputs": {
                "passages_files": len(files),
                "weekdays_available": weekday_days,
                "note": ("pipeline day 2026-10-16 has no passages by "
                         "design; any other gap is a pipeline artifact"),
            },
        },
        "pairs": results,
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    log(f"\n[pb] pairs with observed rides: {n_obs}/{len(OD_PAIRS)}")
    log(f"[pb] saved -> {OUT}")
    con.close()


if __name__ == "__main__":
    main()
