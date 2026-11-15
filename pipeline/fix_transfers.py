"""One-shot surgical fix: replace the journeys transfer section in api/app.py.
Replaces from the transfer-cache marker to '@app.get(\"/pulse\")' with clean code.
"""
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "api" / "app.py"
src = P.read_text(encoding="utf-8")

START = "    # Build per-request walk-neighbor cache for transfer points"
END = '@app.get("/pulse")'

i0 = src.index(START)
i1 = src.index(END)

new = '''    # Build per-request walk-neighbor cache for transfer points (<=350 m)
    xfer_cache = {}

    def xfer_neighbors(stop_id):
        if stop_id not in xfer_cache:
            la, lo, _ = stop_pos[stop_id]
            ns = nearest_stops(la, lo, k=5, radius_km=0.35)
            xfer_cache[stop_id] = [
                (n["stop_id"], n["walk_min"], n["name"], n["lat"], n["lng"])
                for n in ns if n["walk_min"] <= 12]
        return xfer_cache[stop_id]

    def _finish(opts):
        if not opts:
            return dict(options=[],
                        note="no route found via max 1 transfer in range",
                        replay_day=strday, replay_mode=replay_mode)
        opts.sort(key=lambda x: x["total_min"])
        seen, picked = set(), []
        for op in opts:
            if op["_key"] in seen:
                continue
            seen.add(op["_key"])
            op.pop("_key")
            picked.append(op)
            if len(picked) == 3:
                break
        return dict(options=picked, replay_day=strday, replay_mode=replay_mode)

    def _emit_transfer(A, B, i, ia, tb, j, oa, tstop, wmin, db_, via=None):
        if len(options) >= MAX_OPTIONS:
            return
        bhour = int((dep_epoch + oa["walk_min"] * 60 + 19800) // 3600 % 24)
        wait_a = wait_minutes(A["route_id"], oa["stop_id"], hour=bhour)
        board_epoch = dep_epoch + oa["walk_min"] * 60 + wait_a * 60
        rideA, t_at_tr, loA, hiA = ride_time(A, i, ia, board_epoch, day=day, win=win)
        thour = int((t_at_tr + 19800) // 3600 % 24)
        wait_b = wait_minutes(B["route_id"], tstop, hour=thour)
        xfer_s = TRANSFER_BUFFER_S + wmin * 60 + wait_b * 60
        t_board2 = t_at_tr + xfer_s
        rideB, _, loB, hiB = ride_time(B, tb, j, t_board2, day=day, win=win)
        total = (oa["walk_min"] + wait_a + rideA / 60 + xfer_s / 60
                 + rideB / 60 + db_["walk_min"])
        tname = stop_pos[tstop][2] if tstop in stop_pos else tstop
        options.append(dict(
            _key=(A["route_id"], B["route_id"], tstop),
            transfers=1,
            total_min=round(total, 1),
            walk_o=oa["walk_min"], walk_d=db_["walk_min"],
            wait_min=round(wait_a + xfer_s / 60, 1),
            ride_min=round((rideA + rideB) / 60, 1),
            route_id=f"{A[\'route_id\']}+{B[\'route_id\']}",
            route_name=f"{route_name(A[\'route_id\'])} + {route_name(B[\'route_id\'])}",
            board=oa["stop_id"], board_name=oa["name"],
            alight=db_["stop_id"], alight_name=db_["name"],
            transfer_stop=tstop, transfer_name=tname,
            transfer_walk_min=round(wmin, 1) if wmin else None,
            transfer_via=via,
            transfer_coord=[stop_pos[tstop][1], stop_pos[tstop][0]]
                if tstop in stop_pos else None,
            board_at=(dep + dt.timedelta(
                minutes=oa["walk_min"] + wait_a)).isoformat(),
            arrive=(dep + dt.timedelta(minutes=total)).isoformat(),
            legs=[
                dict(type="walk", minutes=oa["walk_min"],
                     coords=[[o["lat"], o["lng"]], [oa["lat"], oa["lng"]]]),
                dict(type="bus", minutes=round(rideA / 60, 1),
                     route=route_name(A["route_id"]),
                     stops=_bus_leg_coords(A, i, ia)),
                dict(type="transfer", minutes=round(xfer_s / 60, 1),
                     at=tstop, walk_min=round(wmin, 1) if wmin else None),
                dict(type="bus", minutes=round(rideB / 60, 1),
                     route=route_name(B["route_id"]),
                     stops=_bus_leg_coords(B, tb, j)),
                dict(type="walk", minutes=db_["walk_min"],
                     coords=[[db_["lat"], db_["lng"]], [d["lat"], d["lng"]]]),
            ],
            eta_p50_min=round(total, 1),
            eta_p10_min=round(oa["walk_min"] + wait_a + xfer_s / 60
                              + (loA + loB) / 60, 1),
            eta_p90_min=round(oa["walk_min"] + wait_a + xfer_s / 60
                              + (hiA + hiB) / 60, 1),
            usual_p50_min=round(total, 1),
            confidence="v0.6-walk-transfers",
        ))

    # ---------------- one-transfer options (same-stop + walk transfers) ----
    for oa in o_near:
        for db_ in d_near:
            for pi, i in STOP_MEMBER.get(oa["stop_id"], []):
                A = PAT[pi]
                apos_after = [(s, k) for k, s in enumerate(A["stops"]) if k > i]
                if not apos_after:
                    continue
                for pj, j in STOP_MEMBER.get(db_["stop_id"], []):
                    B = PAT[pj]
                    if pi == pj or A["route_id"] == B["route_id"]:
                        continue
                    bpos = {s: k for k, s in enumerate(B["stops"])}
                    for tstop, ia in apos_after:
                        tb = bpos.get(tstop)
                        if tb is None or tb >= j:
                            continue
                        _emit_transfer(A, B, i, ia, tb, j, oa, tstop, 0.0, db_)
                        if len(options) >= MAX_OPTIONS:
                            return _finish(options)
                        # walk transfers (<=350 m hop between stops)
                        for (t2, wmin, wname, wlat, wlng) in xfer_neighbors(tstop):
                            if t2 == tstop:
                                continue
                            tb2 = bpos.get(t2)
                            if tb2 is None or tb2 >= j:
                                continue
                            _emit_transfer(A, B, i, ia, tb2, j, oa, t2, wmin,
                                           db_, via=(tstop,))
                            if len(options) >= MAX_OPTIONS:
                                return _finish(options)
    return _finish(options)


'''
P.write_text(src[:i0] + new + src[i1:], encoding="utf-8")
print(f"replaced {i1 - i0} chars of transfer section with {len(new)} clean chars")
import ast
ast.parse(P.read_text(encoding="utf-8"))
print("api/app.py parses OK")
