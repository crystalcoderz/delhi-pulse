/**
 * Delhi Pulse — Edge Worker v4
 *
 * v3: FULL journey planner at the edge. Loads model.json.gz from its own
 * static asset on first request, serves walk/wait/ride/transfers with
 * real Delhi speeds, headways, quantile-style bands.
 *
 * v4 (fix-plan A + judge-surface P0):
 *   - model.json.gz is the PRIMARY asset (970 KB vs 3.8 MB cold parse);
 *     plain model.json is a dev-only fallback now.
 *   - P0: direct+transfer candidate selection rebuilt on per-request
 *     pattern->positions maps (FULL membership, no first-24 slice), so
 *     hub stops (ISBT Kashmere Gate serves 88+ patterns) no longer lose
 *     trunk routes and coordinate queries match name queries.
 *   - A7 scored fuzzy stop match; C5 walk transfers (<=350 m hop);
 *     C6 per-leg speed provenance + n; B7-band holdout calibration;
 *     D9 /journeys cache-control; C2-lite rank-before-cut.
 *   - E2-final: Places session tokens (X-Session-Token) on
 *     autocomplete + resolve; billed endpoints behind an explicit
 *     origin allowlist with the null-origin ban.
 *
 * Endpoints:
 *   /api/journeys?from=&to=&depart=&max=   → full journey search
 *   /api/stops/nearby?lat=&lng=&radius=     → stops within radius
 *   /api/stops/search?q=                    → fuzzy stop search
 *   /api/places/autocomplete?input=         → Google Places (New) typing
 *                                             suggestions, Delhi-restricted
 *   /api/places/resolve?place_id=           → place id → name + coords
 *   /api/gmaps-drive?from=&to=&depart=      → Google Routes v2 traffic-aware
 *                                             driving time (comparison)
 *   /api/pulse                               → city speed curve
 *   /api/geo?hour=&limit=&stride=            → route shapes colored by speed
 *   /api/status                              → model + edges + live counters
 *   /api/maps-config                         → browser basemap key (v2 UI)
 *   /api/health                              → system status
 *
 * The Google server key lives as the Worker secret GOOGLE_MAPS_KEY and is
 * never shipped to clients. The browser basemap uses a separate, referrer-
 * restricted key, GOOGLE_MAPS_BROWSER_KEY, served by /api/maps-config.
 *
 * Static assets serve: /index.html (planner UI), /model.json.gz (PRIMARY;
 * model.json kept only as a dev fallback)
 */
const JSONH = { "content-type": "application/json" };
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,OPTIONS",
  "access-control-allow-headers": "*",
};

const j = (obj, status = 200, headers) =>
  new Response(JSON.stringify(obj),
    { status, headers: { ...JSONH, ...CORS, ...headers } });

// E2 / security agent 6: the billed Google proxies get an explicit ORIGIN
// ALLOWLIST, not CORS *. The allowlist is this Worker's own host (the
// edge-served UI is same-origin), localhost/127 for dev, and — only when
// the deployment pins it — the exact env.VERCEL_HOST for the Vercel-hosted
// UI. The old *.vercel.app suffix matched anyone's project (billing risk)
// and is gone. A present-but-foreign Origin gets 403; third-party pages
// always send Origin. `Origin: null` / malformed origins are blocked too.
//
// null-origin (absent Origin header) is banned on the BILLED endpoints
// only: absent Origin means tooling/bots, not the web UI. The one
// legitimate origin-less caller is the same-origin UI itself — browsers
// omit Origin on same-origin GET fetches but always attach Fetch
// Metadata, so sec-fetch-site same-origin/same-site/none passes. Read
// endpoints keep their open default (read API stays CORS *).
const originAllowedHost = (host, url, env) =>
  [url.hostname, "localhost", "127.0.0.1"].includes(host)
  || (env && env.VERCEL_HOST
      && host === String(env.VERCEL_HOST).trim().toLowerCase());

const originBlocked = (request, url, env = null, billed = false) => {
  const origin = request.headers.get("origin");
  if (!origin) {
    if (!billed) return false;
    const sfs = (request.headers.get("sec-fetch-site") || "").toLowerCase();
    return !(sfs === "same-origin" || sfs === "same-site" || sfs === "none");
  }
  try {
    return !originAllowedHost(new URL(origin).hostname, url, env);
  } catch { return true; }
};

const jBilled = (obj, status, request, url, env = null) => {
  const h = { ...JSONH, "access-control-allow-methods": "GET,OPTIONS", "vary": "Origin" };
  const origin = request.headers.get("origin");
  if (origin && !originBlocked(request, url, env, false)) {
    h["access-control-allow-origin"] = origin;
    h["access-control-allow-headers"] = "*";
  }
  return new Response(JSON.stringify(obj), { status, headers: h });
};

const inNCR = (la, ln) =>
  la >= 26.5 && la <= 30.5 && ln >= 74.5 && ln <= 79.5;

// per-isolate cache for billed Google drive comparisons (E2)
const driveCache = new Map();

// ---- model cache (per-isolate) ----
let M = null; // model data

async function getModel(env) {
  if (M) return M;
  // v4 / H2-P1: the gzip asset is PRIMARY — 970 KB vs 3.8 MB means ~4x
  // faster fetch+decompress+parse on a cold isolate. The plain model.json
  // asset stays as a fallback for local dev setups only.
  let lastErr = "no asset served";
  try {
    const gz = await env.ASSETS.fetch(
      new Request("https://delhi-pulse.prayaas.workers.dev/model.json.gz"));
    if (gz.ok) {
      const buf = await gz.arrayBuffer();
      const ds = new DecompressionStream("gzip");
      const text = await new Response(
        new Blob([buf]).stream().pipeThrough(ds)).text();
      M = JSON.parse(text);
      return M;
    }
    lastErr = "model.json.gz asset missing (HTTP " + gz.status + ")";
  } catch (e) { lastErr = String(e && e.message || e); }
  try {
    const resp = await env.ASSETS.fetch(
      new Request("https://delhi-pulse.prayaas.workers.dev/model.json"));
    if (resp.ok) {
      M = JSON.parse(await resp.text());
      return M;
    }
    lastErr = "model.json asset missing (HTTP " + resp.status + ")";
  } catch (e) { lastErr = String(e && e.message || e); }
  throw new Error("model load failed: " + lastErr);
}

// ---- helpers ----
function haversineM(lat1, lng1, lat2, lng2) {
  const R = 6371000;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLng = (lng2 - lng1) * Math.PI / 180;
  const a = Math.sin(dLat/2) ** 2 + Math.cos(lat1 * Math.PI/180) *
    Math.cos(lat2 * Math.PI/180) * Math.sin(dLng/2) ** 2;
  return R * 2 * Math.asin(Math.sqrt(a));
}

// ---- A7: fuzzy stop-name scoring (shared by resolve() + /stops/search) ----
// Weights per fix-plan: substring 3x + all-token-prefix 2x + token-overlap
// 1x; a STOP_EXACT_SCORE match always outranks any component pile-up, so
// exact beats substring and stops the resolve() scan early.
const STOP_EXACT_SCORE = 7;   // strictly above any component total (6)
const stopTokens = (nameLower) => nameLower.split(/[^a-z0-9]+/).filter(Boolean);
function stopScore(nameLower, nameTokens, q, qTokens) {
  if (nameLower === q) return STOP_EXACT_SCORE;
  let sc = 0;
  if (nameLower.includes(q)) sc += 3;
  if (qTokens.length) {
    let allPrefix = true, anyOverlap = false;
    for (const qt of qTokens) {
      let hit = false;
      for (const nt of nameTokens) {
        if (nt === qt) { hit = true; anyOverlap = true; break; }
        if (!hit && nt.startsWith(qt)) hit = true;
      }
      if (!hit) { allPrefix = false; break; }
    }
    if (allPrefix) sc += 2;
    if (anyOverlap) sc += 1;
  }
  return sc;
}

// ---- E2-final: Places session tokens ----
// One token per typing session is what the UI sends (crypto.randomUUID,
// rotated on visibilitychange). Autocomplete + resolve carrying the same
// token bill as ONE session. Absent or malformed header keeps the legacy
// sessionless behaviour, so older UIs and plain API probes still work.
const placesSessionToken = (request) => {
  const t = (request.headers.get("x-session-token") || "").trim();
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
    .test(t) ? t : null;
};

// ---- H1: cap user input echoed back in notes/errors ----
const clipEcho = (t) => String(t ?? "").slice(0, 120);

// ---- C5: walk-transfer neighbours (port of api/app.py xfer_neighbors) ----
// Stops within a 350 m hop of the alight stop can host the second leg's
// boarding even when pattern B does not serve the alight stop. The lookup
// is O(nearby) thanks to a one-off 0.005-degree grid (like GEO, built once
// per isolate) and the small per-stop result lists are memoised per
// isolate — the model asset is immutable once loaded, so cache it.
let XFGRID = null, XFNBR = null;
const XFER_HOP_MAX_M = 350, XFER_HOP_K = 5, XFER_STOP_CAP = 15;
function xferNeighborsOf(M, sid) {
  if (!XFNBR) XFNBR = new Map();
  const memo = XFNBR.get(sid);
  if (memo) return memo;
  if (!XFGRID) {                    // built once per isolate, O(stops)
    XFGRID = new Map();
    for (const sid2 of Object.keys(M.stops)) {
      const s = M.stops[sid2];
      const key = Math.floor(s[0] / 0.005) + "," + Math.floor(s[1] / 0.005);
      const cell = XFGRID.get(key);
      if (cell) cell.push(sid2); else XFGRID.set(key, [sid2]);
    }
  }
  const s = M.stops[sid];
  const out = [];
  if (s) {        // 0.005 deg cells (~>=486 m on both axes at Delhi's
                  // latitude) so the 3x3 window covers every <=350 m hop
    const cl = Math.floor(s[0] / 0.005), cg = Math.floor(s[1] / 0.005);
    for (let dl = -1; dl <= 1; dl++) {
      for (let dg = -1; dg <= 1; dg++) {
        const cell = XFGRID.get((cl + dl) + "," + (cg + dg));
        if (!cell) continue;
        for (const t of cell) {
          if (t === sid) continue;
          const p = M.stops[t];
          const d = haversineM(s[0], s[1], p[0], p[1]);
          if (d > 0 && d <= XFER_HOP_MAX_M && (M.membership[t] || []).length)
            out.push([t, Math.round(d * 1.3 / 80 * 10) / 10]);
        }
      }
    }
    out.sort((a, b) => a[1] - b[1]);
    out.length = Math.min(out.length, XFER_HOP_K);   // k=5 nearest, as local
  }
  if (XFNBR.size > 2048) XFNBR.clear();   // driveCache-style containment
  XFNBR.set(sid, out);
  return out;
}

// ---- City Pulse geometry (per-isolate cache) ----
// One line per route with observed speeds: its longest stop pattern, drawn
// stop-to-stop (the edge model carries stop sequences, not GTFS shapes).
// Geometry is pre-serialized once so each /geo response is string assembly.
let GEO = null;

function getRouteGeo(M) {
  if (GEO) return GEO;
  const longest = new Map();
  for (const pat of M.patterns) {
    if (M.route_avg[pat.r] == null) continue;
    const cur = longest.get(pat.r);
    if (!cur || pat.s.length > cur.s.length) longest.set(pat.r, pat);
  }
  const out = [];
  for (const [route, pat] of longest) {
    const pts = [];
    let lenM = 0, prev = null;
    for (const sid of pat.s) {
      const s = M.stops[sid];
      if (!s) continue;
      if (prev) lenM += haversineM(prev[0], prev[1], s[0], s[1]);
      pts.push("[" + s[1].toFixed(5) + "," + s[0].toFixed(5) + "]");
      prev = s;
    }
    if (pts.length < 2) continue;
    out.push({ route, name: M.route_names[route] || route, lenM,
               geom: '"geometry":{"type":"LineString","coordinates":['
                     + pts.join(",") + "]}" });
  }
  // longest first: a stable set of trunk routes, so only colors change by hour
  out.sort((a, b) => b.lenM - a.lenM);
  GEO = out;
  return GEO;
}

// same ramp as api/app.py _ramp(): slow amber/red -> fast teal
function speedColor(kmh, vmax = 36) {
  const t = Math.max(0, Math.min(1, kmh / vmax));
  const c = (a, b) => Math.round(a + (b - a) * t).toString(16).padStart(2, "0");
  return "#" + c(240, 60) + c(110, 210) + c(40, 160);
}

// ---- export ----
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const p = url.pathname;

    try {
      // ---- health ----
      if (p === "/api/health" || p === "/health") {
        const M = await getModel(env);
        return j({ ok: true, stops: Object.keys(M.stops).length,
                   patterns: M.patterns.length,
                   routes: Object.keys(M.route_avg).length });
      }

      // ---- Google Maps Platform proxies (no model load needed) ----
      // GKEY = env.GOOGLE_MAPS_KEY (Cloudflare secret, Places + Routes only).
      // It is never sent to clients. The browser basemap uses a separate
      // key, env.GOOGLE_MAPS_BROWSER_KEY, restricted to the Maps JavaScript
      // API and this site's referrer in Google Cloud.
      const GKEY = env.GOOGLE_MAPS_KEY || null;
      // The Referer lets the key carry an HTTP-referrer restriction that also
      // covers these server-side calls.
      const gHead = (extra) => ({ "content-type": "application/json",
                                  "x-goog-api-key": GKEY,
                                  referer: "https://delhi-pulse.prayaas.workers.dev/",
                                  ...extra });

      // Browser config for the Maps JavaScript API basemap. The browser key
      // is public BY DESIGN (it must ship to the page); the real control is
      // the Google-side HTTP-referrer + API restriction. The Origin check
      // here only stops third-party web pages from reading it conveniently;
      // non-browser callers can always fetch it.
      if (p === "/api/maps-config" || p === "/maps-config") {
        // H1: a malformed Origin header used to throw inside new URL() and
        // turn this endpoint into a 500. Route it through the shared
        // allowlist gate instead (billed=false: unbilled endpoint, so the
        // null-origin tooling default stays allowed, as before).
        if (originBlocked(request, url, env, false))
          return new Response(JSON.stringify({ key: null, note: "forbidden" }), {
            status: 403, headers: JSONH });
        const BKEY = env.GOOGLE_MAPS_BROWSER_KEY || null;
        if (!BKEY) return j({ key: null, note: "maps not configured" }, 503);
        return new Response(JSON.stringify({
          key: BKEY, mapId: env.GOOGLE_MAPS_MAP_ID || "DEMO_MAP_ID" }), {
          headers: { ...JSONH, "cache-control": "private, max-age=300" } });
      }
      // Delhi NCR bounding box used to scope every Places lookup
      const DELHI_BOX = { rectangle: {
        low: { latitude: 28.15, longitude: 76.60 },
        high: { latitude: 29.00, longitude: 77.65 } } };

      // typing suggestions for the planner search box
      if (p === "/api/places/autocomplete" || p === "/places/autocomplete") {
        if (originBlocked(request, url, env, true))
          return jBilled({ error: "origin not allowed" }, 403, request, url, env);
        const input = (url.searchParams.get("input")
                     || url.searchParams.get("q") || "").slice(0, 60);
        if (input.trim().length < 3)      // E2: min 3 chars — re-verified
          return jBilled({ suggestions: [] }, 400, request, url, env);
        if (!GKEY) return jBilled({ suggestions: [],
                              note: "places search not configured" }, 503, request, url, env);
        // E2-final: Places session billing. The UI's X-Session-Token
        // (crypto.randomUUID) groups this typing session with the resolve
        // call that terminates it. Absent/malformed = legacy sessionless.
        const session = placesSessionToken(request);
        const body = { input, locationRestriction: DELHI_BOX,
                       languageCode: "en", includedRegionCodes: ["in"] };
        if (session) body.sessionToken = session;
        const r = await fetch("https://places.googleapis.com/v1/places:autocomplete", {
          method: "POST",
          // H1: field mask moved from ?$fields= to the x-goog-fieldmask
          // header (hardcoded mask; no client-controllable mask parsing)
          headers: gHead({ "x-goog-fieldmask":
            "suggestions.placePrediction.placeId,suggestions.placePrediction.text"
            + ",suggestions.placePrediction.types" }),
          body: JSON.stringify(body),
        });
        if (!r.ok) return jBilled({ suggestions: [], note: "google error" }, 502, request, url, env);
        const d = await r.json().catch(() => null);   // H1: junk body, not a 500
        const sugg = d && Array.isArray(d.suggestions)
          ? d.suggestions.map(s => { const pr = s.placePrediction || {};
              return { id: pr.placeId, text: (pr.text || {}).text,
                       types: (pr.types || []).slice(0, 3) }; })
            .filter(x => x.id && x.text)
          : [];
        return jBilled({ suggestions: sugg.slice(0, 6),
                   source: "google-places-new" }, 200, request, url, env);
      }

      // place id → coordinates (for journey search after suggestion click)
      if (p === "/api/places/resolve" || p === "/places/resolve") {
        if (originBlocked(request, url, env, true))
          return jBilled({ error: "origin not allowed" }, 403, request, url, env);
        const pidRaw = url.searchParams.get("place_id") || "";
        const pid = pidRaw.replace(/^places\//, "");
        if (!/^[A-Za-z0-9_-]{10,300}$/.test(pid))  // H1: some real ids run long
          return jBilled({ error: "valid place_id required" }, 400, request, url, env);
        if (!GKEY) return jBilled({ error: "places not configured" }, 503, request, url, env);
        // E2-final: terminate the Places session the autocomplete calls
        // opened by passing the same token to Place Details (New) via
        // ?sessionToken=... — the documented session termination path.
        // Absent token = legacy sessionless call.
        const session = placesSessionToken(request);
        const r = await fetch(
          `https://places.googleapis.com/v1/places/${encodeURIComponent(pid)}`
          + (session ? `?sessionToken=${encodeURIComponent(session)}` : ""), {
            headers: gHead({ "x-goog-fieldmask":
              "id,location,displayName,formattedAddress,types" }),
          });
        if (!r.ok) return jBilled({ error: "place not found" }, 502, request, url, env);
        const d = await r.json().catch(() => null);   // H1: junk body, not a 500
        const loc = d ? d.location : null;
        if (!loc || loc.latitude == null)
          return jBilled({ error: "place not found" }, 502, request, url, env);
        // E2: the billed Place-Details proxy stays Delhi-scoped
        if (!inNCR(loc.latitude, loc.longitude || 0))
          return jBilled({ error: "place is outside the Delhi region" }, 404, request, url, env);
        return jBilled({ place_id: d.id || pid,
                   name: (d.displayName || {}).text || "",
                   address: d.formattedAddress || null,
                   lat: loc.latitude ?? null, lng: loc.longitude ?? null,
                   types: (d.types || []).slice(0, 3),
                   source: "google-places-new" }, 200, request, url, env);
      }

      // live google traffic-aware DRIVING time for the UI comparison strip
      if (p === "/api/gmaps-drive" || p === "/gmaps-drive") {
        const from = url.searchParams.get("from") || "";
        const to = url.searchParams.get("to") || "";
        const departT = url.searchParams.get("depart") || "";
        const m1 = from.match(/^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$/);
        const m2 = to.match(/^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$/);
        if (!m1 || !m2) return jBilled({ error: "from,to must be lat,lng" }, 400, request, url, env);
        if (!GKEY) return jBilled({ drive_min: null, note: "not configured" }, 503, request, url, env);
        if (originBlocked(request, url, env, true))
          return jBilled({ error: "origin not allowed" }, 403, request, url, env);
        // E2-geo: the billed Routes proxy is scoped to Delhi-NCR and to
        // plausible city trips (was world-open)
        const olat = parseFloat(m1[1]), olng = parseFloat(m1[2]);
        const dlat = parseFloat(m2[1]), dlng = parseFloat(m2[2]);
        if (!inNCR(olat, olng) || !inNCR(dlat, dlng))
          return jBilled({ error: "outside the Delhi region" }, 400, request, url, env);
        if (haversineM(olat, olng, dlat, dlng) > 120000)
          return jBilled({ error: "origin and destination are too far apart" }, 400, request, url, env);
        // E2: cache keyed on ROUNDED coords (kills spelling-variant re-billing)
        // + the 15-min now-bucket; in-window departures also keyed by their
        // own bucket so different future times within one now-bucket differ
        const nowBucket = Math.floor(Date.now() / 900000);
        let depKey = "";
          let s = (departT || "").trim();
          s = s.replace(/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}(:\d{2})?)$/, "$1T$2");
          s = s.replace(/ ([+-]\d{2}:?\d{2})$/, "$1");
          s = s.replace(/ (\d{2}:?\d{2})$/, "+$1");
          if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(s)) s += "+05:30";
          const t = new Date(s).getTime();
          if (Number.isFinite(t))
            depKey = Number.isFinite(t) && t > Date.now()
              ? String(Math.floor(t / 900000)) : "live";
        const ckey = olat.toFixed(4) + "," + olng.toFixed(4) + "|"
                   + dlat.toFixed(4) + "," + dlng.toFixed(4) + "|"
                   + nowBucket + "|" + depKey;
        const cached = driveCache.get(ckey);
        if (cached) return jBilled(cached, 200, request, url, env);
        const body = {
          origin: { location: { latLng: { latitude: olat, longitude: olng } } },
          destination: { location: { latLng: { latitude: dlat, longitude: dlng } } },
          travelMode: "DRIVE",
          routingPreference: "TRAFFIC_AWARE",
        };
        if (departT) {
          // Google requires departureTime within [now, now + 7 days]; outside
          // that window it 400s, so let it default to live traffic instead.
          if (Number.isFinite(t) && t > Date.now()
              && t < Date.now() + 7 * 24 * 3600 * 1000)
            body.departureTime = new Date(t).toISOString();
        }
        // H1: field mask moved from ?$fields= into the header; hard-coded
        const r = await fetch(
          "https://routes.googleapis.com/directions/v2:computeRoutes", {
            method: "POST",
            headers: gHead({ "x-goog-fieldmask":
              "routes.duration,routes.distanceMeters" }),
            body: JSON.stringify(body),
          });
        if (!r.ok)
          return jBilled({ drive_min: null, note: "google error" }, 200, request, url, env);
        const d = await r.json().catch(() => null);   // H1: junk body, not a 500
        if (!d || !Array.isArray(d.routes) || !d.routes.length)
          return jBilled({ drive_min: null }, 200, request, url, env);
        const dur = parseFloat(d.routes[0].duration);
        const out = {
          drive_min: dur ? Math.round(dur / 60 * 10) / 10 : null,
          drive_km: Math.round((d.routes[0].distanceMeters || 0) / 100) / 10,
          source: "google-routes-v2-traffic-aware",
        };
        if (out.drive_min != null) {          // store in the bucketed cache
          if (driveCache.size > 512) driveCache.clear();
          driveCache.set(ckey, out);
        }
        return jBilled(out, 200, request, url, env);
      }

      // ---- load model for all other endpoints ----
      const M = await getModel(env);

      // ---- nearby stops (inlined, same logic as debug which works) ----
      if (p === "/api/stops/nearby" || p === "/stops/nearby") {
        const lat = parseFloat(url.searchParams.get("lat"));
        const lng = parseFloat(url.searchParams.get("lng"));
        // H1: finite coords only, Delhi-scope enforced, radius clamped
        const rRaw = parseFloat(url.searchParams.get("radius") || "2000");
        const radius = Number.isFinite(rRaw)
          ? Math.min(20000, Math.max(50, rRaw)) : 2000;
        if (!Number.isFinite(lat) || !Number.isFinite(lng))
          return j({ error: "lat,lng required" }, 400);
        if (!inNCR(lat, lng))
          return j({ error: "coordinates are outside the Delhi region" }, 400);

        const sorted = Object.keys(M.stops)
          .map(sid => {
            const s = M.stops[sid];
            return { sid, d: haversineM(lat, lng, s[0], s[1]),
                     name: s[2], lat: s[0], lng: s[1] };
          })
          .sort((a, b) => a.d - b.d)
          .slice(0, 6)
          .filter(x => x.d <= radius);

        return j(sorted.map(x => ({
          stop_id: x.sid,
          name: x.name,
          lat: x.lat, lng: x.lng,
          distance_m: Math.round(x.d),
          walk_min: Math.round(x.d * 1.3 / 80 * 10) / 10,
        })));
      }

      // ---- stop search (A7: scored fuzzy match, top 10) ----
      if (p === "/api/stops/search" || p === "/stops/search") {
        const q = (url.searchParams.get("q") || "").trim().toLowerCase();
        if (q.length < 2) return j([]);   // D6: no arbitrary matches
        const qTokens = stopTokens(q);
        // One O(stops) pass with a bounded top-10 buffer (insertion by
        // score desc, then shorter name, then scan order). Exact-name
        // hits score STOP_EXACT_SCORE and always outrank substring hits.
        const top = [];
        let seq = 0;
        for (const sid of Object.keys(M.stops)) {
          const s = M.stops[sid];
          const nl = s[2].toLowerCase();
          const sc = stopScore(nl, stopTokens(nl), q, qTokens);
          if (!sc) continue;
          const len = s[2].length, sq = seq++;
          if (top.length >= 10) {
            const w = top[9];
            if (w.sc > sc || (w.sc === sc && (w.len < len
                || (w.len === len && w.seq < sq)))) continue;
          }
          let posn = top.length;
          while (posn > 0) {
            const u = top[posn - 1];
            if (u.sc > sc || (u.sc === sc && (u.len < len
                || (u.len === len && u.seq < sq)))) break;
            posn--;
          }
          top.splice(posn, 0, { sc, len, seq: sq, sid, s });
          if (top.length > 10) top.pop();
        }
        return j(top.map(({ sid, s, sc }) => ({
          stop_id: sid, name: s[2], lat: s[0], lng: s[1], score: sc })));
      }

      // ---- PULSE: city curve ----
      if (p === "/api/pulse" || p === "/pulse") {
        const M2 = await getModel(env);
        return j({ city_hour: M2.city_hour, source: "real-ping-pairs @ edge" });
      }

      // ---- GEO: route lines colored by observed speed (City Pulse map) ----
      // Same contract as the local API's /geo. Routes with a speed observed
      // in this hour come first; the rest fall back to their all-day average
      // and are marked source="route-avg" so the client can fade them.
      if (p === "/api/geo" || p === "/geo") {
        const hRaw = parseInt(url.searchParams.get("hour") ?? "9", 10);
        const hour = Number.isFinite(hRaw) ? Math.min(23, Math.max(0, hRaw)) : 9;
        const lRaw = parseInt(url.searchParams.get("limit") ?? "600", 10);
        const limit = Number.isFinite(lRaw) ? Math.min(1500, Math.max(10, lRaw)) : 600;
        const sRaw = parseInt(url.searchParams.get("stride") ?? "1", 10);
        const stride = Number.isFinite(sRaw) ? Math.min(5, Math.max(1, sRaw)) : 1;

        const G = getRouteGeo(M);
        const feats = [];
        let seen = 0;
        const add = (g, sp, source) => {
          if (seen++ % stride !== 0) return;   // thin before serializing
          feats.push(
            '{"type":"Feature","properties":' + JSON.stringify({
              route: g.route, name: g.name,
              speed_kmh: Math.round(sp * 10) / 10,
              color: speedColor(sp), source, stride }) + "," + g.geom + "}");
        };
        for (const g of G) {
          if (feats.length >= limit) break;
          const sp = M.speeds[g.route]?.[hour];
          if (sp != null) add(g, sp, "route-hour");
        }
        for (const g of G) {
          if (feats.length >= limit) break;
          if (M.speeds[g.route]?.[hour] == null)
            add(g, M.route_avg[g.route], "route-avg");
        }
        return new Response(
          '{"type":"FeatureCollection","features":[' + feats.join(",")
          + '],"hour":' + hour + ',"stride":' + stride + ',"day":null}',
          { headers: { ...JSONH, ...CORS,
                       "cache-control": "public, max-age=3600" } });
      }

      // ---- JOURNEYS: full planner (inlined for same scoping pattern) ----
      if (p === "/api/journeys" || p === "/journeys") {
        const fromT = url.searchParams.get("from");
        const toT = url.searchParams.get("to");
        const departT = url.searchParams.get("depart");
        const asOfT = url.searchParams.get("asOf");
        const maxRaw = parseInt(url.searchParams.get("max") || "3", 10);
        const maxO = Number.isFinite(maxRaw)
          ? Math.min(6, Math.max(1, maxRaw)) : 3;   // D3: clamp 1..6

        if (!fromT || !toT) return j({ error: "from, to required" }, 400);

        // resolve places
        function resolve(text) {
          text = (text || "").trim();
          const mc = text.match(/^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$/);
          if (mc) return { lat: parseFloat(mc[1]), lng: parseFloat(mc[2]) };
          if (text.startsWith("stop:")
              && Object.prototype.hasOwnProperty.call(M.stops, text.slice(5))) {
            const s = M.stops[text.slice(5)];
            return { lat: s[0], lng: s[1], stop_id: text.slice(5) };
          }
          const q = text.toLowerCase();
          if (q.length < 2) return null;   // 1-char/blank matched everything
          const qTokens = stopTokens(q);
          // A7: ONE O(stops) scored pass — substring 3 + all-token-prefix 2
          // + token-overlap 1. An exact name scores STOP_EXACT_SCORE, so
          // exact beats any substring pile-up, and hitting that threshold
          // STOPS the scan immediately; otherwise the best fuzzy score
          // wins ("Kashmere Gate ISBT" no longer needs a literal substring).
          let best = null, bestScore = 0;
          for (const sid of Object.keys(M.stops)) {
            const s = M.stops[sid];
            const nl = s[2].toLowerCase();
            const sc = stopScore(nl, stopTokens(nl), q, qTokens);
            if (sc <= bestScore) continue;
            bestScore = sc;
            best = { lat: s[0], lng: s[1], stop_id: sid };
            if (sc === STOP_EXACT_SCORE) break;   // threshold: stop the scan
          }
          return best;
        }
        const origin = resolve(fromT);
        const dest = resolve(toT);
        // H1: echoed user input is capped; D9: uncachable by default
        if (!origin) return j({ options: [], note: `cannot find "${clipEcho(fromT)}"` },
          200, { "cache-control": "no-store" });
        if (!dest) return j({ options: [], note: `cannot find "${clipEcho(toT)}"` },
          200, { "cache-control": "no-store" });

        // D4: reject coordinate queries that are nowhere near Delhi
        const isCoordText = (t) =>
          /^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$/.test((t || "").trim());
        const inDelhi = (lat, lng) =>
          lat > 27.0 && lat < 30.0 && lng > 74.5 && lng < 79.5;
        for (const t of [fromT, toT]) {
          if (isCoordText(t)) {
            const [la, ln] = t.trim().split(",").map(parseFloat);
            if (!inDelhi(la, ln))
              return j({ error: "coordinates are outside the Delhi region" }, 400);
          }
        }

        // D1: structural depart parsing — handles every real form. The old
        // regex-replace chain corrupted "...00 +05:30" into "00++05:30" and
        // "2026-11-17 08:30" into garbage; a single structural match is
        // simpler and provable. Bare local times = IST (local API parity);
        // date-only = midnight IST; garbage -> 400 with a hint.
        let depEpoch = Date.now() / 1000;
        if (departT) {
          let s = departT.trim();
          const m = s.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*([+-]\d{2}:?\d{2})?$/);
          if (m) {
            const off = m[3]
              ? m[3].replace(/^([+-])(\d{2}):?(\d{2})$/, "$1$2:$3")
              : "+05:30";
            s = m[1] + "T" + m[2] + off;
          } else if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
            s = s + "T00:00:00+05:30";
          }
          const t = new Date(s).getTime();
          if (!Number.isFinite(t))
            return j({ error: `depart cannot be parsed: "${clipEcho(departT)}"`,
                       hint: "use ISO 8601, e.g. 2026-11-17T08:30:00+05:30" }, 400);
          depEpoch = t / 1000;
        }
        // hour normalized for any epoch (pre-1970 negative-safe)
        const hour0 = (((Math.floor((depEpoch + 19800) / 3600) % 24) + 24) % 24);
        // D9: a future-depart answer is immutable for its window, so it is
        // public-cacheable for 5 minutes; everything priced off "now"
        // (no depart given) must never be cached.
        const depCacheHeaders = (departT && depEpoch > Date.now() / 1000)
          ? { "cache-control": "public, max-age=300" }
          : { "cache-control": "no-store" };

        // C3-lite: trips a human walks are answered walk-only (this also
        // protects the search from degenerate same-point scans, P1 1102)
        const crowFlyM = haversineM(origin.lat, origin.lng, dest.lat, dest.lng);
        if (crowFlyM <= 250) {
          const walkOnly = Math.max(0.5, crowFlyM * 1.3 / 80);
          const o = {
            transfers: 0,
            total_min: Math.round(walkOnly * 10) / 10,
            walk_o: 0, walk_d: Math.round(walkOnly * 10) / 10,
            wait_min: 0, ride_min: 0,
            route_id: null, route_name: "Walk",
            board: null, board_name: null,
            alight: null, alight_name: null,
            board_at: new Date(depEpoch * 1000).toISOString(),
            arrive: new Date((depEpoch + walkOnly * 60) * 1000).toISOString(),
            legs: [{ type: "walk", minutes: Math.round(walkOnly * 10) / 10,
                     coords: [[origin.lat, origin.lng], [dest.lat, dest.lng]] }],
            eta_p50_min: Math.round(walkOnly * 10) / 10,
            eta_p10_min: Math.round(walkOnly * 10) / 10,
            eta_p90_min: Math.round(walkOnly * 1.2 * 10) / 10,
            confidence: "walk-only",
          };
          return j({ options: [o], note: "walkable trip" }, 200, depCacheHeaders);
        }

        // ---- P0 fix: nearby stops feed pattern->positions maps ----
        // The old scan kept the FIRST 24 patterns of each stop's membership
        // in insertion order, so hub stops (ISBT Kashmere Gate serves 88+,
        // ISBT Nityanand Marg 183) lost their trunk routes and coordinate
        // versions of the featured queries returned zero options while the
        // same pair by stop name worked. Every nearby stop now contributes
        // its FULL membership to per-pattern position maps; the direct
        // candidate set is the intersection (measured on the shipped model:
        // the 183x183 worst case shrinks to the tens of routes serving BOTH
        // neighbourhoods) — bounded, complete and ORDER-INDEPENDENT. Exact
        // stop: references also inject their full membership (unsliced).
        const PATTERN_WALK_WINDOW = 90;
        const DIRECT_PATTERN_CAP = 256;   // pathological-intersection guard
        let oNear, dNear;
        const mkNearby = (lat, lng, k = 8, limit = 2500) =>
          Object.keys(M.stops)
            .map(sid => { const s = M.stops[sid];
              return { sid, d: haversineM(lat, lng, s[0], s[1]),
                       name: s[2], lat: s[0], lng: s[1] }; })
            .filter(x => (M.membership[x.sid] || []).length > 0)
            .filter(x => x.d <= limit)
            .sort((a, b) => a.d - b.d).slice(0, k);

        const hasStop = (id) =>
          M.stops != null && Object.prototype.hasOwnProperty.call(M.stops, id);
        oNear = mkNearby(origin.lat, origin.lng, 8);
        if (origin.stop_id && hasStop(origin.stop_id)) {
          const s = M.stops[origin.stop_id];
          const exact = { sid: origin.stop_id, name: s[2],
                          lat: s[0], lng: s[1], d: 0, walk_min: 0 };
          oNear = [exact, ...oNear.filter(n => n.sid !== origin.stop_id)];
        }
        dNear = mkNearby(dest.lat, dest.lng, 8);
        if (dest.stop_id && hasStop(dest.stop_id)) {
          const s = M.stops[dest.stop_id];
          const exact = { sid: dest.stop_id, name: s[2],
                          lat: s[0], lng: s[1], d: 0, walk_min: 0 };
          dNear = [exact, ...dNear.filter(n => n.sid !== dest.stop_id)];
        }
        oNear = oNear.map(x => x.walk_min !== undefined ? x :
          {...x, walk_min: Math.round(x.d * 1.3 / 80 * 10) / 10});
        dNear = dNear.map(x => x.walk_min !== undefined ? x :
          {...x, walk_min: Math.round(x.d * 1.3 / 80 * 10) / 10});

        if (!oNear.length || !dNear.length) {
          return j({ options: [], note: "no bus stops within walking distance" },
            200, depCacheHeaders);
        }

        // pattern -> [[position, nearby-idx] ...], built once from the FULL
        // membership of all 8 nearby stops per side (no slice). oPats is
        // reused by the transfer scan for its A legs.
        const oPats = new Map(), dPats = new Map();
        oNear.forEach((oa, oi) => {
          for (const [pi, pos] of (M.membership[oa.sid] || [])) {
            let l = oPats.get(pi);
            if (!l) { l = []; oPats.set(pi, l); }
            l.push([pos, oi]);
          }
        });
        dNear.forEach((da, di) => {
          for (const [pj, pos] of (M.membership[da.sid] || [])) {
            let l = dPats.get(pj);
            if (!l) { l = []; dPats.set(pj, l); }
            l.push([pos, di]);
          }
        });
        oPats.forEach((l) => l.sort((a, b) => a[0] - b[0]));

        // direct journey search
        // ride-time helpers: dwelled traversal speeds x per-route detour
        // factor (along-route vs chord calibration, B9)
        const detourOf = (route) => M.detour?.[route] ?? 1.077;
        // B7-band: holdout band-coverage calibration exported as
        // M.band_cal = [lo_factor, hi_factor] by the export agent
        // (defaults to [1,1] when the holdout had nothing to calibrate).
        // The clamps keep lo<=1<=hi so p10<=p50<=p90 survives even a
        // malformed calibration blob.
        const BAND_CAL = Array.isArray(M.band_cal) && M.band_cal.length === 2
          && Number.isFinite(+M.band_cal[0]) && Number.isFinite(+M.band_cal[1])
          ? [+M.band_cal[0], +M.band_cal[1]] : [1, 1];
        const bandOf = (route, hour) => {
          const b = M.band?.[route]?.[hour] ?? [0.85, 1.35];
          return [Math.min(1, b[0] * BAND_CAL[0]),
                  Math.max(1, b[1] * BAND_CAL[1])];
        };
        // C6: the speed chain reports provenance + sample count
        // (M.speeds_n / M.route_avg_n from the export agent) so callers
        // see whether a ride is priced on route-hour data, a route
        // average, or the city default.
        const speedInfoOf = (route, hour) => {
          const rh = M.speeds[route]?.[hour];
          if (Number.isFinite(rh)) {
            const n = M.speeds_n?.[route]?.[hour];
            return { sp: Math.max(3, rh), src: "route-hour",
                     n: Number.isFinite(n) ? n : null };
          }
          const ra = M.route_avg[route];
          if (Number.isFinite(ra)) {
            const n = M.route_avg_n?.[route];
            return { sp: Math.max(3, ra), src: "route-avg",
                     n: Number.isFinite(n) ? n : null };
          }
          return { sp: Math.max(3, M.city_hour[hour] ?? 18),
                   src: "city", n: null };
        };
        const usualWaitOf = (route, hour) => {
          const w = M.headway[route]?.[hour];
          if (w == null || w > 3600) return null;
          return Math.min(60, Math.max(0.75, w / 120));
        };
        // B4 service helper shared by direct + transfer phases: no
        // observed headway within ±1h means not running (quoted waits cap
        // at 60 min). Small cache: hub routes repeat across phases.
        const waitCache = new Map();
        const waitAt = (route, hour) => {
          const k = route + "#" + hour;
          let w = waitCache.get(k);
          if (w !== undefined) return w;
          for (const dh of [0, -1, 1]) {
            const h = ((hour + dh) % 24 + 24) % 24;
            const hv = M.headway[route]?.[h];
            // actual observed service only: headways beyond an hour are
            // schedule arithmetic on dormant routes, not real service
            if (hv != null && hv <= 3600) {
              w = Math.min(60, Math.max(0.75, hv / 120));
              waitCache.set(k, w);
              return w;
            }
          }
          waitCache.set(k, null);
          return null;
        };
        // cumulative chord length per pattern, memoised per request: an
        // O(1) position->position span lookup for candidate ranking
        const cumOf = (() => {
          const cache = new Map();
          return (pi) => {
            let cum = cache.get(pi);
            if (cum) return cum;
            const pat = M.patterns[pi] || { s: [] };
            cum = [0];
            let acc = 0;
            for (let k = 1; k < pat.s.length; k++) {
              const p0 = M.stops[pat.s[k - 1]], p1 = M.stops[pat.s[k]];
              if (p0 && p1) acc += haversineM(p0[0], p0[1], p1[0], p1[1]);
              cum.push(acc);
            }
            cache.set(pi, cum);
            return cum;
          };
        })();

        const options = [];
        const seen = new Set();
        const distinct = new Map();   // C1 push-dedupe: best per route sequence

        // ---- phase 1 (C2-lite): pre-score every intersection candidate ----
        // Direct candidates are the oPats ∩ dPats intersection of FULL
        // memberships; each is priced with O(1) cumulative-chord spans and
        // the cheapest origin/dest position pairing wins. PRUNING STAYS
        // PERMISSIVE: the only rejections mirror the full phase's own (no
        // shared pattern, backwards span, B4 no-service, non-finite) —
        // every candidate that could survive the full computation
        // survives phase 1, so when the pool is small ALL of them are
        // fully evaluated and nothing found early crowds out a better
        // late candidate (the old first-30 truncation, C2).
        const candidates = [];
        for (const [pi, oList] of oPats) {
          const dList = dPats.get(pi);
          if (!dList) continue;
          const pat = M.patterns[pi];
          if (!pat) continue;
          const waitMin = waitAt(pat.r, hour0);
          if (waitMin == null) continue;
          const sp = speedInfoOf(pat.r, hour0).sp;
          const cum = cumOf(pi);
          let best = null;
          for (const [i, oi] of oList) {
            if (i < 0 || i >= cum.length - 1) continue;
            const walkOMin = oNear[oi].d * 1.3 / 80;
            for (const [j2, di] of dList) {
              if (j2 <= i || j2 >= cum.length) continue;
              const est = walkOMin + dNear[di].d * 1.3 / 80 + waitMin
                + (cum[j2] - cum[i]) * detourOf(pat.r) * 3.6 / sp / 60;
              if (Number.isFinite(est) && (best == null || est < best.est))
                best = { est, oi, di, i, j2 };
            }
          }
          if (best)
            candidates.push({ est: best.est, pi, route: pat.r,
                              oa: oNear[best.oi], da: dNear[best.di],
                              i: best.i, j2: best.j2 });
        }
        // rank before cutting (C2-lite); the cap is a same-cost-envelope
        // guard against pathological mega-hub intersections only
        candidates.sort((a, b) => a.est - b.est);
        if (candidates.length > DIRECT_PATTERN_CAP)
          candidates.length = DIRECT_PATTERN_CAP;

        // ---- phase 2: full computation, cheapest candidate first ----
        for (const cand of candidates) {
          const { oa, da, pi, i, j2, route } = cand;
          const pat = M.patterns[pi];
          if (!pat) continue;
          const rName = M.route_names[route] || route;

          // exact along-stop distances + leg geometry for this span
          let distM = 0;
          const legCoords = [];
          for (let k = i; k <= j2; k++) {
            const sid = pat.s[k];
            const s = M.stops[sid];
            if (s) legCoords.push([s[0], s[1]]);
            if (k > i) {
              const prev = M.stops[pat.s[k-1]];
              if (prev && s) distM += haversineM(prev[0], prev[1], s[0], s[1]);
            }
          }

          // speed floor 3 km/h: the model contains near-zero cells that
          // would otherwise produce multi-thousand-minute rides
          const spd = speedInfoOf(route, hour0);
          const rideS = distM * detourOf(route) * 3.6 / spd.sp;
          const waitMin = waitAt(route, hour0);
          if (waitMin == null) continue;   // B4: not running this hour
          const walkOMin = oa.d * 1.3 / 80;
          const walkDMin = da.d * 1.3 / 80;
          const total = walkOMin + waitMin + rideS/60 + walkDMin;
          if (!Number.isFinite(total) || total < 0) continue;

          const key = route + "|" + oa.sid + "|" + da.sid;
          if (seen.has(key)) continue;
          seen.add(key);

          // C1 push-time dedupe: keep only the best candidate per route
          // sequence, so nothing burns slots on duplicates
          const seqKey = "0|" + route;
          const prevBest = distinct.get(seqKey);
          if (prevBest != null && prevBest <= total) continue;
          distinct.set(seqKey, total);

          options.push({
            transfers: 0,
            total_min: Math.round(total * 10) / 10,
            walk_o: Math.round(walkOMin * 10) / 10,
            walk_d: Math.round(walkDMin * 10) / 10,
            wait_min: Math.round(waitMin * 10) / 10,
            ride_min: Math.round(rideS / 60 * 10) / 10,
            route_id: route,
            route_code: rName,
            route_name: rName,
            board: oa.sid, board_name: oa.name,
            alight: da.sid, alight_name: da.name,
            board_at: new Date((depEpoch + (walkOMin + waitMin) * 60) * 1000).toISOString(),
            arrive: new Date((depEpoch + total * 60) * 1000).toISOString(),
            legs: [
              { type: "walk", minutes: Math.round(walkOMin * 10) / 10,
                coords: [[origin.lat, origin.lng], [oa.lat, oa.lng]] },
              { type: "bus", minutes: Math.round(rideS/60 * 10) / 10,
                route: rName, stops: legCoords,
                source: spd.src, n: spd.n },          // C6 provenance
              { type: "walk", minutes: Math.round(walkDMin * 10) / 10,
                coords: [[da.lat, da.lng], [dest.lat, dest.lng]] },
            ],
            eta_p50_min: Math.round(total * 10) / 10,
            // D2: bands cover the whole journey incl. final walk;
            // A3: ratios from the trained quantile cells (B7-calibrated)
            eta_p10_min: Math.round((walkOMin + waitMin + walkDMin
              + rideS * bandOf(route, hour0)[0] / 60) * 10) / 10,
            eta_p90_min: Math.round((walkOMin + waitMin + walkDMin
              + rideS * bandOf(route, hour0)[1] / 60) * 10) / 10,
            confidence: "edge-route-hour",
          });
          // A5: usual time at this route's quieter service hour
          {
            const uh = M.usual_hour?.[route];
            if (uh != null) {
              const spU = M.speeds[route]?.[uh]
                ?? M.route_avg[route] ?? M.city_hour[uh] ?? 18;
              const rideU = distM * detourOf(route) * 3.6 / spU;
              const waitU = usualWaitOf(route, uh);
              if (waitU != null) {
                const up50 = walkOMin + waitU + rideU / 60 + walkDMin;
                options[options.length - 1].usual_p50_min =
                  Math.round(up50 * 10) / 10;
                options[options.length - 1].usual_hour_ist = uh;
                options[options.length - 1].rush_effect_min =
                  Math.round((total - up50) * 10) / 10;
              }
            }
          }
        }

        // ---- one-transfer search (same-stop + C5 walk transfers) ----
        // Port of api/app.py v0.6 transfer planner: ride pattern A from the
        // origin stop to any downstream stop that pattern B serves before
        // the destination alight point; C5 lets B board up to a 350 m walk
        // away from the alight stop. 2-min transfer buffer (+ the walk hop)
        // + route-hour waits, second-leg speed taken at the hour the
        // transfer actually happens.
        // H3-P3: the skip condition no longer excludes short trips
        // (crowFlyM < 3000 silently lost transfer answers); the scan now
        // runs whenever the direct answers are insufficient.
        try {
          if (options.length < Math.max(4, maxO)) {
          const maxLegM = crowFlyM * 1.8 + 6000;   // prune far-out zigzags

          // dest side: stop -> [[pj, j1, j2, d-index]] for every stop before
          // the alight point. P0: built from the FULL dNear membership —
          // the old 24-pattern slice dropped trunk second legs at hub-ish
          // destinations too. Measured worst hub on the shipped model
          // contributes only ~7.7k positions, so completeness is cheap.
          const destMap = new Map();
          dNear.forEach((da, di) => {
            for (const [pj, j2] of (M.membership[da.sid] || [])) {
              const B = M.patterns[pj];
              if (!B) continue;
              for (let j1 = 0; j1 < j2; j1++) {
                const s = B.s[j1];
                let a = destMap.get(s);
                if (!a) { a = []; destMap.set(s, a); }
                a.push([pj, j1, j2, di]);
              }
            }
          });

          const seenX = new Set();
          let evaluated = 0;
          const xferWalk = new Set();   // C5: <=15 unique transfer stops/req

          // One transfer candidate. `false` return = CPU guard tripped, the
          // caller breaks the scan. Shared by same-stop (hop 0) and C5
          // walk transfers; local API parity: transfer_stop = the BOARD
          // stop, and for walk transfers transfer_via carries the alight.
          const evalXfer = (ctx, boardStop, hopMin, pj, j1b, j2b, di) => {
            const B = M.patterns[pj];
            if (!B || ctx.pi === pj || ctx.A.r === B.r) return true;
            const da = dNear[di];
            const key = ctx.A.r + "|" + B.r + "|" + boardStop
                      + "|" + ctx.oa.sid + "|" + da.sid;
            if (seenX.has(key)) return true;
            // CPU guard: counted before the expensive dB sweep
            if (++evaluated > 400) return false;
            let dB = 0;
            for (let m = j1b + 1; m <= j2b; m++) {
              const t0 = M.stops[B.s[m - 1]], t1 = M.stops[B.s[m]];
              if (t0 && t1) dB += haversineM(t0[0], t0[1], t1[0], t1[1]);
            }
            if (dB > 2 * maxLegM) return true;
            const walkOMin = ctx.oa.d * 1.3 / 80;
            const walkDMin = da.d * 1.3 / 80;
            const waitA = ctx.waitA, rideA = ctx.rideA;
            // hour at which leg B actually boards, negative-safe (H1)
            const thour = (((Math.floor((depEpoch
              + (walkOMin + waitA + rideA + 2 + hopMin) * 60
              + 19800) / 3600) % 24) + 24) % 24);
            const waitB = waitAt(B.r, thour);
            if (waitB == null) return true;   // B4: B not running at transfer hour
            const xferMin = 2 + hopMin + waitB;   // buffer + hop (C5) + wait
            const infoB = speedInfoOf(B.r, thour);
            const rideB = dB * detourOf(B.r) * 3.6 / infoB.sp / 60;
            const total = walkOMin + waitA + rideA + xferMin
                        + rideB + walkDMin;

            const loA = [], loB = [];
            for (let k2 = ctx.i; k2 <= ctx.k; k2++) {
              const s = M.stops[ctx.A.s[k2]]; if (s) loA.push([s[0], s[1]]);
            }
            for (let m = j1b; m <= j2b; m++) {
              const s = M.stops[B.s[m]]; if (s) loB.push([s[0], s[1]]);
            }
            const bs = M.stops[boardStop];
            const al = M.stops[ctx.tstop];
            const rA = M.route_names[ctx.A.r] || ctx.A.r;
            const rB = M.route_names[B.r] || B.r;
            const xferLeg = { type: "transfer",
              minutes: Math.round(xferMin * 10) / 10, at: boardStop,
              walk_min: Math.round(hopMin * 10) / 10 };
            if (hopMin > 0 && al && bs)
              xferLeg.coords = [[al[0], al[1]], [bs[0], bs[1]]];

            options.push({
              transfers: 1,
              total_min: Math.round(total * 10) / 10,
              walk_o: Math.round(walkOMin * 10) / 10,
              walk_d: Math.round(walkDMin * 10) / 10,
              wait_min: Math.round((waitA + xferMin) * 10) / 10,
              ride_min: Math.round((rideA + rideB) * 10) / 10,
              route_id: ctx.A.r + "+" + B.r,
              route_code: rA + "+" + rB,
              route_name: rA + " + " + rB,
              board: ctx.oa.sid, board_name: ctx.oa.name,
              alight: da.sid, alight_name: da.name,
              transfer_stop: boardStop,
              transfer_name: bs ? bs[2] : boardStop,
              transfer_coord: bs ? [bs[0], bs[1]] : null,
              transfer_walk_min: Math.round(hopMin * 10) / 10,
              transfer_via: hopMin > 0 ? [ctx.tstop] : null,
              board_at: new Date((depEpoch + (walkOMin + waitA) * 60) * 1000).toISOString(),
              arrive: new Date((depEpoch + total * 60) * 1000).toISOString(),
              legs: [
                { type: "walk", minutes: Math.round(walkOMin * 10) / 10,
                  coords: [[origin.lat, origin.lng], [ctx.oa.lat, ctx.oa.lng]] },
                { type: "bus", minutes: Math.round(rideA * 10) / 10,
                  route: rA, stops: loA,
                  source: ctx.infoA.src, n: ctx.infoA.n },   // C6 provenance
                xferLeg,
                { type: "bus", minutes: Math.round(rideB * 10) / 10,
                  route: rB, stops: loB, source: infoB.src, n: infoB.n },
                { type: "walk", minutes: Math.round(walkDMin * 10) / 10,
                  coords: [[da.lat, da.lng], [dest.lat, dest.lng]] },
              ],
              eta_p50_min: Math.round(total * 10) / 10,
              // A3: per-leg band ratios from the trained quantile cells
              // (B7-calibrated; walks/waits are exact, so the band stays
              // anchored to the same base as p50: p10 <= p50 <= p90)
              eta_p10_min: Math.round((total
                - rideA * (1 - bandOf(ctx.A.r, hour0)[0])
                - rideB * (1 - bandOf(B.r, thour)[0])) * 10) / 10,
              eta_p90_min: Math.round((total
                + rideA * (bandOf(ctx.A.r, hour0)[1] - 1)
                + rideB * (bandOf(B.r, thour)[1] - 1)) * 10) / 10,
              confidence: "edge-transfer-route-hour",
            });
            // A5: usual time — both legs re-timed at their quieter hours
            {
              const uhA = M.usual_hour?.[ctx.A.r], uhB = M.usual_hour?.[B.r];
              if (uhA != null && uhB != null) {
                const wAU = usualWaitOf(ctx.A.r, uhA);
                const wBU = usualWaitOf(B.r, uhB);
                if (wAU != null && wBU != null) {
                  const rAU = ctx.dA * detourOf(ctx.A.r) * 3.6
                    / speedInfoOf(ctx.A.r, uhA).sp / 60;
                  const rBU = dB * detourOf(B.r) * 3.6
                    / speedInfoOf(B.r, uhB).sp / 60;
                  const up50 = walkOMin + wAU + rAU + 2 + hopMin + wBU
                             + rBU + walkDMin;
                  const last = options[options.length - 1];
                  last.usual_p50_min = Math.round(up50 * 10) / 10;
                  last.usual_hour_ist = Math.min(uhA, uhB);
                  last.rush_effect_min =
                    Math.round((total - up50) * 10) / 10;
                }
              }
            }
            return true;
          };

          // A legs walk the oPats union (FULL origin-side membership, so
          // trunk first legs survive at hubs). Each pattern is walked ONCE
          // from its earliest origin-side position — that window covers
          // every downstream stop any later boarding could reach — and
          // each dest-side hit prices the nearer boarding variants itself.
          scan:
          for (const [pi, oList] of oPats) {
            const A = M.patterns[pi];
            if (!A) continue;
            const waitA = waitAt(A.r, hour0);
            if (waitA == null) continue;     // B4: pattern A not running
            const infoA = speedInfoOf(A.r, hour0);
            const cum = cumOf(pi);
            const i0 = oList[0][0];
            const tailPos = oList[oList.length - 1][0];
            if (i0 >= A.s.length - 1) continue;
            const lim = Math.min(A.s.length - 1,
                                 i0 + PATTERN_WALK_WINDOW);
            for (let k = i0 + 1; k <= lim; k++) {
              const tstop = A.s[k];
              // zigzag containment: once even the last boarding variant's
              // ride (the shortest possible one) exceeds maxLegM, no later
              // stop on this pattern is a plausible transfer — cum only
              // grows; variants before tailPos may still activate.
              if (k > tailPos && cum[k] - cum[tailPos] > maxLegM) break;
              const cands = destMap.get(tstop);
              if (!cands) continue;
              for (const [pos, oi] of oList) {
                if (pos >= k) continue;       // board must precede the alight
                const oa = oNear[oi];
                const dA = cum[k] - cum[pos];  // exact chord span of this ride
                const rideA = dA * detourOf(A.r) * 3.6 / infoA.sp / 60;
                if (rideA > maxLegM) continue;   // per-variant zigzag prune
                const ctx = { pi, A, oa, i: pos, k, tstop, waitA, infoA,
                              dA, rideA };
                // same-stop transfer (hop 0)
                for (const [pj, j1, j2, di] of cands) {
                  if (!evalXfer(ctx, tstop, 0, pj, j1, j2, di)) break scan;
                  if (options.length >= 60) break scan;
                }
                // C5 walk transfers: a <=350 m hop between nearby stops
                // opens second-leg patterns that do NOT serve the alight
                // stop. At most XFER_STOP_CAP unique transfer stops per
                // request are expanded (memo count in xferWalk); the
                // neighbour lists themselves are memoised per isolate.
                if (xferWalk.size < XFER_STOP_CAP || xferWalk.has(tstop)) {
                  xferWalk.add(tstop);
                  for (const [t2, hopMin] of xferNeighborsOf(M, tstop)) {
                    const cands2 = destMap.get(t2);
                    if (!cands2) continue;
                    for (const [pj2, j1b, j2b, dib] of cands2) {
                      if (!evalXfer(ctx, t2, hopMin, pj2, j1b, j2b, dib))
                        break scan;
                      if (options.length >= 60) break scan;
                    }
                  }
                }
              }
            }
          }
        }
        } catch (e) {
          console.error("transfer search failed:", e && e.message || String(e));
          /* transfer search must never break direct results */
        }

        // C1: keep the best option per route sequence — the same itinerary
        // via a nearby board/alight pair is not a different option
        const bySeq = new Map();
        for (const o of options) {
          const k = (o.transfers || 0) + "|" + o.route_id;
          const prev = bySeq.get(k);
          if (!prev || o.total_min < prev.total_min) bySeq.set(k, o);
        }
        const ranked = [...bySeq.values()];
        ranked.sort((a, b) => a.total_min - b.total_min);
        ranked.forEach(o => delete o._key);

        const asOfNote = asOfT
          ? "asOf time-travel replay is served by the full local API; the edge serves the pooled full-month model"
          : null;
        return j({ options: ranked.slice(0, Math.min(maxO, 6)),
                   note: ranked.length ? asOfNote
                     : (asOfNote || "no route found within 1 transfer") },
          200, depCacheHeaders);
      }

      // ---- lookups ----
      if (p === "/api/lookup" || p === "/lookup") {
        const route = url.searchParams.get("route") || "";
        const hRaw = parseInt(url.searchParams.get("hour") || "9", 10);
        // H1: clamp the hour to 0..23 (NaN/huge values leaked into
        // undefined lookups before) and read table keys as own-properties
        // only — route=constructor used to ride the prototype chain into
        // garbage speeds.
        const hour = Number.isFinite(hRaw)
          ? Math.min(23, Math.max(0, hRaw)) : 9;
        const M2 = await getModel(env);
        const has = (o, k) => Object.hasOwn(o, k);
        const rh = has(M2.speeds, route)
          && Number.isFinite(M2.speeds[route][hour]);
        const ra = has(M2.route_avg, route)
          && Number.isFinite(M2.route_avg[route]);
        const hwEntry = has(M2.headway, route) ? M2.headway[route][hour] : null;
        const hw = Number.isFinite(hwEntry) ? hwEntry : null;
        return j({
          route, hour,
          route_hour_kmh: rh ? M2.speeds[route][hour] : null,
          route_avg_kmh: ra ? M2.route_avg[route] : null,
          expected_wait_min: hw ? Math.min(15, Math.max(0.75, hw/120)) : null,
          city_hour_kmh: M2.city_hour[hour] ?? 18,
          source: rh ? "route-hour" : ra ? "route-avg" : "city",
        });
      }

      // ---- /status (for the planner UI) ----
      if (p === "/status" || p === "/api/status") {
        const M2 = await getModel(env);
        return j({
          ok: true,
          routes: Object.keys(M2.route_avg).length,
          patterns: M2.patterns.length,
          edges: M2.edge_count ?? 0,   // trained edge cells shipped in the model asset
          live: { vehicles: 0, url: false, n_fetch: 0 },
          note: "edge deployment"
        });
      }

      // ---- root redirect to planner page ----
      if (p === "/" || p === "/api") {
        return j({ service: "Delhi Pulse Edge",
                   endpoints: ["/api/health", "/api/journeys?from=&to=",
                              "/api/stops/nearby?lat=&lng=",
                              "/api/stops/search?q=",
                              "/api/places/autocomplete?input=",
                               "/api/places/resolve?place_id=",
                               "/api/gmaps-drive?from=&to=&depart=",
                               "/api/pulse", "/api/geo?hour=&limit=&stride=",
                               "/api/lookup?route=&hour=", "/api/status"],
                   playground: "/playground",
                   docs: "github.com/crystalcoderz/delhi-pulse" });
      }

      return j({ error: "not found" }, 404);
    } catch (e) {
      console.error("worker error:", e && e.stack || String(e), url.pathname);
      return j({ error: "internal error",
                 ref: Math.random().toString(36).slice(2, 10) }, 500);
    }
  },
};
