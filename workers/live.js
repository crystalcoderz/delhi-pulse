/**
 * Delhi Pulse — live GTFS-RT layer for the Edge Worker (backlog A6, §H idea 1)
 *
 * Design: a Cron Trigger polls the GTFS-RT VehiclePositions feed, merges the
 * new sightings into a rolling window stored under one KV key, derives
 * per-route/per-hour "bucket speeds" under a second KV key, and the journey
 * hot path consults only the (small) speeds blob. Two keys, two jobs:
 *
 *   live:positions  {t, n_poll, vs:{veh:{r:route, p:[ts,lat,lng, ...]}}}
 *                   rolling raw window — read ONLY by the cron (and by
 *                   status()/the stale-fallback), never per request.
 *   live:speeds     {t, n_poll, buckets:{route:{hour:{v:kmh, n:pairs}}}}
 *                   derived, hour-bucketed (IST hour, same index the model
 *                   uses) — the only key the hot path touches.
 *
 * Pair filter (A6): consecutive sightings of a vehicle with 0 < dt <= 200 s.
 * 200 s (vs the local poller's 120 s in api/live.py) because the cron samples
 * the feed at cron cadence rather than every 10 s; at the recommended cron
 * below, observed pair gaps land near 180 s and the filter still drops
 * stale/skipped-poll pairs. Quality gates mirror api/live.py window_speeds:
 * same route on both points, >= 15 m displacement, <= 90 km/h implied speed,
 * and a 15-minute estimate window (technical-note §6) with min_pairs = 3.
 *
 * The planner keeps its trained model as the base truth: a live bucket only
 * replaces the CENTRAL speed estimate for its (route, hour) when it has
 * enough pairs, and everything degrades silently to the model when the feed
 * or KV is unavailable. Bands, waits and usual_* stay model-based.
 *
 * Env: GTFS_RT_URL (secret, same var the local API reads), optional
 * GTFS_RT_API_KEY (sent as x-api-key), KV binding LIVE_KV.
 * No dependencies: GTFS-RT protobuf is decoded with a minimal wire walker.
 */

const KEY_POS = "live:positions";
const KEY_SPD = "live:speeds";
const KEEP_S = 2700;        // rolling store window (45 min, api/live.py parity)
const SPEED_WINDOW_S = 900; // 15-min estimate window (technical-note §6)
const DT_MAX_S = 200;       // A6 pair filter: cron-cadence gap ceiling
const D_MIN_M = 15;         // ignore GPS jitter pairs
const V_MAX_KMH = 90;       // implied-speed sanity cap
const V_CLAMP = [1.5, 90];  // published bucket speeds are clamped to this
const MIN_PAIRS = 3;        // api/live.py min_pairs per (route, hour)
const MAX_PTS_VEH = 8;      // per-vehicle history cap (bounds KV value size)
const MAX_POINTS = 60000;   // global point cap (bounds KV value size)
const STALE_S = 600;        // blob older than this -> live layer reports {}
const TTL_MS = 120000;      // per-isolate cache TTL for the KV blobs

// per-isolate cache: at most one KV read per blob per TTL window, so the hot
// path pays one KV read per isolate per 2 minutes no matter how many calls.
const cache = { spd: { v: null, at: 0 }, pos: { v: null, at: 0 } };
let _env = null;

const nowS = () => Date.now() / 1000;
// IST hour of a POSIX timestamp — the exact hour0 formula from worker.js
const istHour = (ts) => Math.floor((ts + 19800) / 3600) % 24;

function haversineM(lat1, lng1, lat2, lng2) {
  const R = 6371000;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLng = (lng2 - lng1) * Math.PI / 180;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * Math.PI / 180) *
    Math.cos(lat2 * Math.PI / 180) * Math.sin(dLng / 2) ** 2;
  return R * 2 * Math.asin(Math.sqrt(a));
}

// ---- minimal protobuf wire walker (subset needed for VehiclePositions) ----
// cb(fieldNumber, wireType, valueU8orNumber); stops on any malformed byte.
function pbWalk(u8, cb) {
  if (!u8 || u8.length === 0) return;
  let p = 0;
  const n = u8.length;
  while (p < n) {
    let key = 0, sh = 0, b;
    do {
      b = u8[p++];
      key += (b & 0x7f) * 2 ** sh;
      sh += 7;
    } while ((b & 0x80) && sh < 35);
    const field = Math.floor(key / 8), wt = key % 8;
    let end = p, val = null;
    if (wt === 0) {                       // varint
      let v = 0;
      sh = 0;
      do {
        b = u8[p++];
        v += (b & 0x7f) * 2 ** sh;
        sh += 7;
      } while ((b & 0x80) && sh < 70);
      val = v;
      end = p;
    } else if (wt === 1) {                // 64-bit
      end = p + 8;
      val = u8.subarray(p, end);
      p = end;
    } else if (wt === 5) {                // 32-bit
      end = p + 4;
      val = u8.subarray(p, end);
      p = end;
    } else if (wt === 2) {                // length-delimited
      let len = 0;
      sh = 0;
      do {
        b = u8[p++];
        len += (b & 0x7f) * 2 ** sh;
        sh += 7;
      } while ((b & 0x80) && sh < 35);
      end = p + len;
      val = u8.subarray(p, end);
      p = end;
    } else return;                        // unknown wire type: cannot resync
    if (end > n) return;                  // truncated: best-effort stop
    cb(field, wt, val);
    p = end;
  }
}

const TD = new TextDecoder();

function f32le(u8) {
  return new DataView(u8.buffer, u8.byteOffset, 4).getFloat32(0, true);
}

// FeedMessage -> [[ts, veh, route, lat, lng], ...] (same selection as
// api/live.py fetch_once: position + timestamp required, route optional).
function parseFeed(u8) {
  const ents = [];
  pbWalk(u8, (f, w, v) => {
    if (f !== 2 || w !== 2) return;               // FeedMessage.entity
    const e = { id: "", veh: "", route: "", ts: 0, lat: NaN, lng: NaN };
    ents.push(e);
    pbWalk(v, (f2, w2, v2) => {
      if (f2 === 1 && w2 === 2) e.id = TD.decode(v2);        // FeedEntity.id
      else if (f2 === 4 && w2 === 2) {                       // VehiclePosition
        pbWalk(v2, (f3, w3, v3) => {
          if (f3 === 1 && w3 === 2)                          // TripDescriptor
            pbWalk(v3, (f4, w4, v4) => {
              if (f4 === 5 && w4 === 2) e.route = TD.decode(v4);
            });
          else if (f3 === 2 && w3 === 2)                     // Position
            pbWalk(v3, (f4, w4, v4) => {
              if (w4 === 5 && v4.length === 4) {
                const x = f32le(v4);
                if (f4 === 1) e.lat = x;
                else if (f4 === 2) e.lng = x;
              }
            });
          else if (f3 === 8 && w3 === 2)                     // VehicleDescriptor (field 8; 4 is current_status)
            pbWalk(v3, (f4, w4, v4) => {
              if (f4 === 1 && w4 === 2) e.veh = TD.decode(v4);
            });
          else if (f3 === 5 && w3 === 0) e.ts = v3;          // timestamp
        });
      }
    });
  });
  const pts = [];
  for (const e of ents) {
    if (!Number.isFinite(e.lat) || !Number.isFinite(e.lng)) continue;
    let ts = e.ts;
    if (ts > 1e12) ts = Math.round(ts / 1000);   // ms guard
    if (ts <= 0) continue;
    const veh = e.veh || e.id;
    if (!veh) continue;
    pts.push([ts, veh, e.route || "",
      Math.round(e.lat * 1e5) / 1e5, Math.round(e.lng * 1e5) / 1e5]);
  }
  return pts;
}

// Merge fresh sightings into the stored window; prune by time, per-vehicle
// cap, dedupe. Store shape: vs:{veh:{r, p:[t,lat,lng, ...]}} (flat, JSON-lean).
function mergeStore(stored, fresh, ts) {
  const vs = (stored && stored.vs && typeof stored.vs === "object")
    ? stored.vs : {};
  for (const [t, veh, route, lat, lng] of fresh) {
    const v = vs[veh] || (vs[veh] = { r: "", p: [] });
    if (!Array.isArray(v.p)) v.p = [];
    if (route) v.r = route;
    v.p.push(t, lat, lng);
  }
  const cutoff = ts - KEEP_S;
  const out = {};
  for (const veh of Object.keys(vs)) {
    const v = vs[veh];
    if (!v || !Array.isArray(v.p)) continue;
    const trips = [];
    for (let i = 0; i + 2 < v.p.length; i += 3) {
      const t = v.p[i], la = v.p[i + 1], ln = v.p[i + 2];
      if (!Number.isFinite(t) || !Number.isFinite(la) || !Number.isFinite(ln))
        continue;
      if (t <= cutoff) continue;
      trips.push([t, la, ln]);
    }
    trips.sort((a, b) => a[0] - b[0]);
    const kept = [];
    for (const tr of trips) {
      if (kept.length && kept[kept.length - 1][0] === tr[0]) continue; // dedupe
      kept.push(tr);
    }
    const trimmed = kept.slice(-MAX_PTS_VEH);
    if (trimmed.length) {
      const r = (v.r && typeof v.r === "string") ? v.r : "";
      out[veh] = { r, p: trimmed.flat() };
    }
  }
  return out;
}

// Global point cap: shrink every vehicle evenly instead of picking victims.
function capPoints(vs, maxPts) {
  const keys = Object.keys(vs);
  let total = 0;
  for (const k of keys) total += vs[k].p.length / 3;
  if (total <= maxPts) return vs;
  const keepN = Math.max(2, Math.floor(maxPts / Math.max(1, keys.length)));
  for (const k of keys) {
    const p = vs[k].p;
    if (p.length > keepN * 3) vs[k].p = p.slice(p.length - keepN * 3);
  }
  return vs;
}

// Pairs -> per-route/per-IST-hour bucket speeds. THE A6 pair filter lives
// here: 0 < dt <= DT_MAX_S, both points inside the 15-min estimate window,
// d >= D_MIN_M, implied speed <= V_MAX_KMH, same (non-empty) route.
function computeBuckets(vs, ts) {
  const agg = new Map();                       // route -> (hour -> [d, dt, n])
  const cutoff = ts - SPEED_WINDOW_S;
  for (const veh of Object.keys(vs)) {
    const v = vs[veh];
    const p = v.p;
    if (!v.r || p.length < 6) continue;
    for (let i = 0; i + 5 < p.length; i += 3) {
      const t0 = p[i], t1 = p[i + 3];
      const dt = t1 - t0;
      if (dt <= 0 || dt > DT_MAX_S) continue;
      if (t0 < cutoff) continue;
      const d = haversineM(p[i + 1], p[i + 2], p[i + 4], p[i + 5]);
      if (d < D_MIN_M || d / dt * 3.6 > V_MAX_KMH) continue;
      const hr = istHour((t0 + t1) / 2);
      let byHour = agg.get(v.r);
      if (!byHour) agg.set(v.r, (byHour = new Map()));
      const c = byHour.get(hr) || [0, 0, 0];
      c[0] += d;
      c[1] += dt;
      c[2] += 1;
      byHour.set(hr, c);
    }
  }
  const buckets = {};
  for (const [route, byHour] of agg) {
    const o = {};
    for (const [hr, c] of byHour) {
      if (c[2] < MIN_PAIRS) continue;
      const kmh = Math.min(V_CLAMP[1], Math.max(V_CLAMP[0], c[0] / c[1] * 3.6));
      o[hr] = { v: Math.round(kmh * 10) / 10, n: c[2] };
    }
    if (Object.keys(o).length) buckets[route] = o;
  }
  return buckets;
}

const countPairs = (buckets) => {
  let n = 0;
  for (const r of Object.keys(buckets))
    for (const h of Object.keys(buckets[r])) n += buckets[r][h].n;
  return n;
};

async function loadSpd(e) {
  const t = Date.now();
  if (cache.spd.v && t - cache.spd.at < TTL_MS) return cache.spd.v;
  try {
    const raw = await e.LIVE_KV.get(KEY_SPD);
    const v = raw ? JSON.parse(raw) : null;
    cache.spd = { v, at: t };
    return v;
  } catch (err) {
    console.warn("live: speeds read failed:", String(err).slice(0, 80));
    return cache.spd.v;                        // last known on transient error
  }
}

async function loadPos(e) {
  const t = Date.now();
  if (cache.pos.v && t - cache.pos.at < TTL_MS) return cache.pos.v;
  try {
    const raw = await e.LIVE_KV.get(KEY_POS);
    const v = raw ? JSON.parse(raw) : null;
    cache.pos = { v, at: t };
    return v;
  } catch (err) {
    console.warn("live: positions read failed:", String(err).slice(0, 80));
    return cache.pos.v;
  }
}

// ---- cron side: poll -> merge -> derive -> store both keys ----
async function update(env) {
  const e = env || _env;
  if (!e || !e.LIVE_KV) return { ok: false, skipped: "no LIVE_KV binding" };
  if (!e.GTFS_RT_URL) return { ok: false, skipped: "no GTFS_RT_URL secret" };
  const ts = nowS();
  let stored = null;
  try {
    stored = JSON.parse((await e.LIVE_KV.get(KEY_POS)) || "null");
  } catch (err) { stored = null; }             // corrupt value: start fresh
  let u8 = null;
  try {
    const r = await fetch(e.GTFS_RT_URL, {
      headers: e.GTFS_RT_API_KEY
        ? { "x-api-key": e.GTFS_RT_API_KEY } : {},
      cf: { cacheTtl: 0, cacheEverything: false },   // never a cached feed
    });
    if (!r.ok) return { ok: false, error: "feed HTTP " + r.status };
    u8 = new Uint8Array(await r.arrayBuffer());
  } catch (err) {
    return { ok: false, error: "feed fetch: " + String(err).slice(0, 120) };
  }
  const fresh = parseFeed(u8);
  const vs = capPoints(mergeStore(stored, fresh, ts), MAX_POINTS);
  const nPoll = ((stored && stored.n_poll) || 0) + 1;
  const buckets = computeBuckets(vs, ts);
  const pos = { t: Math.round(ts), n_poll: nPoll, vs };
  const spd = { t: Math.round(ts), n_poll: nPoll, buckets };
  try { await e.LIVE_KV.put(KEY_POS, JSON.stringify(pos)); }
  catch (err) { console.error("live kv put:", err && err.message || String(err)); }
  try { await e.LIVE_KV.put(KEY_SPD, JSON.stringify(spd)); }
  catch (err) { console.error("live kv put:", err && err.message || String(err)); }
  cache.pos = { v: pos, at: Date.now() };      // this replica stays fresh too
  cache.spd = { v: spd, at: Date.now() };
  return {
    ok: true, t: Math.round(ts), n_poll: nPoll,
    vehicles: Object.keys(vs).length,
    points: Object.keys(vs).reduce((a, k) => a + vs[k].p.length / 3, 0),
    added: fresh.length,
    routes: Object.keys(buckets).length,
    pairs: countPairs(buckets),
  };
}

// ---- hot path: live speeds for one IST hour bucket ----
// Returns {route: {v: kmh, n: pairs}} for buckets with >= MIN_PAIRS fresh
// pairs; {} whenever live data is absent/stale/unconfigured. Consumes both
// KV keys: live:speeds normally, live:positions only as the stale-fallback
// recompute source. Never throws — the planner must not fail on live faults.
async function getLiveRoutes(env, hour) {
  const e = env || _env;
  const h = ((Math.floor(Number(hour)) % 24) + 24) % 24;
  if (!e || !e.LIVE_KV || !Number.isFinite(Number(hour))) return {};
  try {
    const spd = await loadSpd(e);
    const pick = (buckets) => {
      const out = {};
      for (const route of Object.keys(buckets)) {
        const c = buckets[route][h];
        if (c && c.n >= MIN_PAIRS && c.v > 0) out[route] = { v: c.v, n: c.n };
      }
      return out;
    };
    if (spd && spd.buckets && nowS() - spd.t <= STALE_S)
      return pick(spd.buckets);
    // speeds blob missing/stale: recompute from the raw window if that at
    // least is fresh (covers a failed speeds write without dropping live).
    const pos = await loadPos(e);
    if (pos && pos.vs && nowS() - pos.t <= STALE_S)
      return pick(computeBuckets(pos.vs, pos.t));
    return {};
  } catch (err) {
    console.warn("live: getLiveRoutes degraded:", String(err).slice(0, 80));
    return {};
  }
}

// ---- /status live field (additive: keeps vehicles/url/n_fetch keys) ----
async function status(env) {
  const e = env || _env;
  const base = { vehicles: 0, url: false, n_fetch: 0 };
  if (!e || !e.LIVE_KV) return { ...base, configured: false };
  try {
    const [spd, pos] = await Promise.all([loadSpd(e), loadPos(e)]);
    const t = nowS();
    const fresh = spd && t - spd.t <= STALE_S;
    let vehicles = 0, points = 0;
    if (pos && pos.vs) {
      vehicles = Object.keys(pos.vs).length;
      for (const k of Object.keys(pos.vs)) points += pos.vs[k].p.length / 3;
    }
    const routes = spd && spd.buckets ? Object.keys(spd.buckets).length : 0;
    return {
      vehicles, points, routes,
      pairs: fresh ? countPairs(spd.buckets || {}) : 0,
      url: !!e.GTFS_RT_URL,
      n_fetch: fresh ? spd.n_poll || 0 : 0,    // polls counted in this blob
      last_poll: spd ? spd.t || null : null,
      stale: !fresh,
      configured: true,
    };
  } catch (err) {
    return { ...base, stale: true, configured: true,
             error: String(err).slice(0, 80) };
  }
}

/**
 * initLive(env) -> { getLiveRoutes, update, status }
 *   getLiveRoutes(env, hour)  hot path: {route: {v, n}} for one IST hour
 *   update(env)               cron: poll feed, merge window, store both keys
 *   status(env)               /status live field
 */
export function initLive(env) {
  _env = env || null;
  return { getLiveRoutes, update, status };
}
