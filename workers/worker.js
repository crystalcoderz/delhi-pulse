/**
 * Delhi Pulse — Edge Worker v3
 *
 * v3: FULL journey planner at the edge. Loads model.json.gz from its own
 * static asset on first request, serves walk/wait/ride/transfers with
 * real Delhi speeds, headways, quantile-style bands.
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
 * Static assets serve: /index.html (planner UI), /model.json.gz
 */
const JSONH = { "content-type": "application/json" };
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,OPTIONS",
  "access-control-allow-headers": "*",
};

const j = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { ...JSONH, ...CORS } });

// E2-hardening: the three billed Google proxies get an ORIGIN ALLOWLIST, not
// CORS *. A present-but-foreign Origin gets 403 (third-party pages always
// send Origin). Same-origin fetches and non-browser clients pass the gate
// and are contained instead by the Delhi geo-scope, normalized cache keys
// and input caps below. TODO(pin): replace *.vercel.app with the exact prod host.
const originBlocked = (request, url) => {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  try {
    const host = new URL(origin).hostname;
    return !(host === url.hostname || host === "localhost"
          || host === "127.0.0.1" || host.endsWith(".vercel.app"));
  } catch { return true; }
};

const jBilled = (obj, status, request, url) => {
  const h = { ...JSONH, "access-control-allow-methods": "GET,OPTIONS", "vary": "Origin" };
  const origin = request.headers.get("origin");
  if (origin && !originBlocked(request, url)) {
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
  try {
    const resp = await env.ASSETS.fetch(
      new Request("https://delhi-pulse.prayaas.workers.dev/model.json"));
    if (resp.ok) {
      const text = await resp.text();
      M = JSON.parse(text);
      return M;
    }
  } catch (e) { /* gzip fallback */ }
  try {
    const resp = await env.ASSETS.fetch(
      new Request("https://delhi-pulse.prayaas.workers.dev/model.json.gz"));
    const gz = await resp.arrayBuffer();
    const ds = new DecompressionStream("gzip");
    const stream = new Blob([gz]).stream().pipeThrough(ds);
    const text = await new Response(stream).text();
    M = JSON.parse(text);
  } catch (e) {
    throw new Error("model load failed: " + String(e));
  }
  return M;
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
        const origin = request.headers.get("origin");
        if (origin) {
          const host = new URL(origin).hostname;
          if (host !== url.hostname && host !== "localhost" && host !== "127.0.0.1")
            return new Response(JSON.stringify({ key: null, note: "forbidden" }), {
              status: 403, headers: JSONH });
        }
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
        if (originBlocked(request, url))
          return jBilled({ error: "origin not allowed" }, 403, request, url);
        const input = (url.searchParams.get("input")
                     || url.searchParams.get("q") || "").slice(0, 60);
        if (input.trim().length < 3)
          return jBilled({ suggestions: [] }, 400, request, url);
        if (!GKEY) return jBilled({ suggestions: [],
                              note: "places search not configured" }, 503, request, url);
        const r = await fetch(
          "https://places.googleapis.com/v1/places:autocomplete"
          + "?$fields=suggestions.placePrediction.placeId"
          + ",suggestions.placePrediction.text"
          + ",suggestions.placePrediction.types", {
            method: "POST",
            headers: gHead(),
            body: JSON.stringify({ input, locationRestriction: DELHI_BOX,
                                   languageCode: "en",
                                   includedRegionCodes: ["in"] }),
          });
        if (!r.ok) return jBilled({ suggestions: [], note: "google error" }, 502, request, url);
        const d = await r.json();
        const sugg = (d.suggestions || [])
          .map(s => { const pr = s.placePrediction || {};
            return { id: pr.placeId, text: (pr.text || {}).text,
                     types: (pr.types || []).slice(0, 3) }; })
          .filter(x => x.id && x.text);
        return jBilled({ suggestions: sugg.slice(0, 6),
                   source: "google-places-new" }, 200, request, url);
      }

      // place id → coordinates (for journey search after suggestion click)
      if (p === "/api/places/resolve" || p === "/places/resolve") {
        if (originBlocked(request, url))
          return jBilled({ error: "origin not allowed" }, 403, request, url);
        const pidRaw = url.searchParams.get("place_id") || "";
        const pid = pidRaw.replace(/^places\//, "");
        if (!/^[A-Za-z0-9_-]{10,200}$/.test(pid))
          return jBilled({ error: "valid place_id required" }, 400, request, url);
        if (!GKEY) return jBilled({ error: "places not configured" }, 503, request, url);
        const r = await fetch(
          `https://places.googleapis.com/v1/places/${encodeURIComponent(pid)}`
          + "?$fields=id,location,displayName,formattedAddress,types", {
            headers: gHead(),
          });
        if (!r.ok) return jBilled({ error: "place not found" }, 502, request, url);
        const d = await r.json();
        const loc = d.location || {};
        // E2: the billed Place-Details proxy stays Delhi-scoped
        if (loc.latitude == null
            || !inNCR(loc.latitude, loc.longitude || 0))
          return jBilled({ error: "place is outside the Delhi region" }, 404, request, url);
        return jBilled({ place_id: d.id || pid,
                   name: (d.displayName || {}).text || "",
                   address: d.formattedAddress || null,
                   lat: loc.latitude ?? null, lng: loc.longitude ?? null,
                   types: (d.types || []).slice(0, 3),
                   source: "google-places-new" }, 200, request, url);
      }

      // live google traffic-aware DRIVING time for the UI comparison strip
      if (p === "/api/gmaps-drive" || p === "/gmaps-drive") {
        const from = url.searchParams.get("from") || "";
        const to = url.searchParams.get("to") || "";
        const departT = url.searchParams.get("depart") || "";
        const m1 = from.match(/^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$/);
        const m2 = to.match(/^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$/);
        if (!m1 || !m2) return jBilled({ error: "from,to must be lat,lng" }, 400, request, url);
        if (!GKEY) return jBilled({ drive_min: null, note: "not configured" }, 503, request, url);
        if (originBlocked(request, url))
          return jBilled({ error: "origin not allowed" }, 403, request, url);
        // E2-geo: the billed Routes proxy is scoped to Delhi-NCR and to
        // plausible city trips (was world-open)
        const olat = parseFloat(m1[1]), olng = parseFloat(m1[2]);
        const dlat = parseFloat(m2[1]), dlng = parseFloat(m2[2]);
        if (!inNCR(olat, olng) || !inNCR(dlat, dlng))
          return jBilled({ error: "outside the Delhi region" }, 400, request, url);
        if (haversineM(olat, olng, dlat, dlng) > 120000)
          return jBilled({ error: "origin and destination are too far apart" }, 400, request, url);
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
        if (cached) return jBilled(cached, 200, request, url);
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
        const r = await fetch(
          "https://routes.googleapis.com/directions/v2:computeRoutes"
          + "?$fields=routes.duration,routes.distanceMeters", {
            method: "POST",
            headers: gHead(),
            body: JSON.stringify(body),
          });
        if (!r.ok)
          return jBilled({ drive_min: null, note: "google error" }, 200, request, url);
        const d = await r.json();
        if (!d.routes || !d.routes.length)
          return jBilled({ drive_min: null }, 200, request, url);
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
        return jBilled(out, 200, request, url);
      }

      // ---- load model for all other endpoints ----
      const M = await getModel(env);

      // ---- nearby stops (inlined, same logic as debug which works) ----
      if (p === "/api/stops/nearby" || p === "/stops/nearby") {
        const lat = parseFloat(url.searchParams.get("lat"));
        const lng = parseFloat(url.searchParams.get("lng"));
        const radius = parseFloat(url.searchParams.get("radius") || "2000") || 2000;
        if (isNaN(lat) || isNaN(lng)) return j({ error: "lat,lng required" }, 400);

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

      // ---- stop search ----
      if (p === "/api/stops/search" || p === "/stops/search") {
        const q = url.searchParams.get("q") || "";
        if (q.trim().length < 2) return j([]);   // D6: no arbitrary matches
        const lower = q.toLowerCase();
        const results = [];
        for (const sid of Object.keys(M.stops)) {
          const s = M.stops[sid];
          if (s[2].toLowerCase().includes(lower)) {
            results.push({ stop_id: sid, name: s[2], lat: s[0], lng: s[1] });
            if (results.length >= 10) break;
          }
        }
        return j(results);
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
          text = (text||"").trim();
          const mc = text.match(/^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$/);
          if (mc) return { lat: parseFloat(mc[1]), lng: parseFloat(mc[2]) };
          if (text.startsWith("stop:")
              && Object.prototype.hasOwnProperty.call(M.stops, text.slice(5))) {
            const s = M.stops[text.slice(5)];
            return { lat: s[0], lng: s[1], stop_id: text.slice(5) };
          }
          const lower = text.toLowerCase().trim();
          if (lower.length < 2) return null;   // 1-char/blank matched everything
          let best = null;
          for (const sid of Object.keys(M.stops)) {
            const s = M.stops[sid];
            if (s[2].toLowerCase().includes(lower)) { best = {lat:s[0],lng:s[1],stop_id:sid}; break; }
          }
          return best;
        }
        const origin = resolve(fromT);
        const dest = resolve(toT);
        if (!origin) return j({ options: [], note: `cannot find "${fromT}"` });
        if (!dest) return j({ options: [], note: `cannot find "${toT}"` });

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
            return j({ error: `depart cannot be parsed: "${departT}"`,
                       hint: "use ISO 8601, e.g. 2026-11-17T08:30:00+05:30" }, 400);
          depEpoch = t / 1000;
        }
        // hour normalized for any epoch (pre-1970 negative-safe)
        const hour0 = (((Math.floor((depEpoch + 19800) / 3600) % 24) + 24) % 24);

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
          return j({ options: [o], note: "walkable trip" });
        }

        // Always include nearby stops, plus the exact stop if stop: given.
        // k=8 + per-stop 24-pattern slice keeps hub stops (some serve 100+
        // patterns) inside the Workers CPU budget — the 12x12x183x183 scan
        // of the first deploy hit Cloudflare 1102 errors under load.
        const PATTERN_SLICE = 24;
        let oNear, dNear;
        const mkNearby = (lat, lng, k=8, limit=2500) =>
          Object.keys(M.stops)
            .map(sid => { const s = M.stops[sid];
              return { sid, d: haversineM(lat, lng, s[0], s[1]),
                        name: s[2], lat: s[0], lng: s[1],
                        pats: (M.membership[sid] || []).slice(0, PATTERN_SLICE) }; })
            .filter(x => x.pats.length > 0)
            .sort((a,b) => a.d - b.d).slice(0, k)
            .filter(x => x.d <= limit);

        const hasStop = (id) =>
          M.stops != null && Object.prototype.hasOwnProperty.call(M.stops, id);
        oNear = mkNearby(origin.lat, origin.lng, 8);
        if (origin.stop_id && hasStop(origin.stop_id)) {
          const s = M.stops[origin.stop_id];
          const exact = { sid: origin.stop_id, name: s[2],
                          lat: s[0], lng: s[1], d: 0, walk_min: 0,
                          pats: M.membership[origin.stop_id] || [] };
          oNear = [exact, ...oNear.filter(n => n.sid !== origin.stop_id)];
        }
        dNear = mkNearby(dest.lat, dest.lng, 8);
        if (dest.stop_id && hasStop(dest.stop_id)) {
          const s = M.stops[dest.stop_id];
          const exact = { sid: dest.stop_id, name: s[2],
                          lat: s[0], lng: s[1], d: 0, walk_min: 0,
                          pats: M.membership[dest.stop_id] || [] };
          dNear = [exact, ...dNear.filter(n => n.sid !== dest.stop_id)];
        }
        oNear = oNear.map(x => x.walk_min !== undefined ? x :
          {...x, walk_min: Math.round(x.d * 1.3 / 80 * 10) / 10});
        dNear = dNear.map(x => x.walk_min !== undefined ? x :
          {...x, walk_min: Math.round(x.d * 1.3 / 80 * 10) / 10});

        if (!oNear.length || !dNear.length) {
          return j({ options: [], note: "no bus stops within walking distance" });
        }

        // direct journey search
        // ride-time helpers: dwelled traversal speeds x per-route detour
        // factor (along-route vs chord calibration, B9)
        const detourOf = (route) => M.detour?.[route] ?? 1.077;
        const bandOf = (route, hour) => M.band?.[route]?.[hour] ?? [0.85, 1.35];
        const usualWaitOf = (route, hour) => {
          const w = M.headway[route]?.[hour];
          if (w == null || w > 3600) return null;
          return Math.min(60, Math.max(0.75, w / 120));
        };
        const options = [];
        const seen = new Set();
        const distinct = new Map();   // C1 push-dedupe: best per route sequence
        for (const oa of oNear) {
          for (const da of dNear) {
            const oPat = oa.pats || [];
            const dPat = da.pats || [];
            for (const [pi, i] of oPat) {
              for (const [pj, j2] of dPat) {
                if (pi !== pj || j2 <= i) continue;
                const pat = M.patterns[pi];
                if (!pat) continue;
                const route = pat.r;
                const rName = M.route_names[route] || route;

                // calculate pattern distance and ride time
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
                const sp = Math.max(3, M.speeds[route]?.[hour0]
                  ?? M.route_avg[route]
                  ?? M.city_hour[hour0] ?? 18);
                const rideS = distM * detourOf(route) * 3.6 / sp;
                // B4: a route with no observed headway in this hour (±1h
                // grace for sparse rows) is not running — offer no bus
                // rather than the old 5-minute default wait.
                let waitMin = null;
                const hourWait = (h) => {
                  const w = M.headway[route]?.[h];
                  // actual observed service only: headways beyond an hour are
                  // schedule arithmetic on dormant routes, not real service
                  if (w == null || w > 3600) return null;
                  return Math.min(60, Math.max(0.75, w / 120));
                };
                for (const dh of [0, -1, 1]) {
                  const h = ((hour0 + dh) % 24 + 24) % 24;
                  waitMin = hourWait(h);
                  if (waitMin != null) break;
                }
                if (waitMin == null) continue;
                const walkOMin = oa.d * 1.3 / 80;
                const walkDMin = da.d * 1.3 / 80;
                const total = walkOMin + waitMin + rideS/60 + walkDMin;
                if (!Number.isFinite(total) || total < 0) continue;

                const key = route + "|" + oa.sid + "|" + da.sid;
                if (seen.has(key)) continue;
                seen.add(key);

                // C1 push-time dedupe: keep only the best candidate per route
                // sequence, so scan-order caps cannot burn slots on duplicates
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
                      route: rName, stops: legCoords },
                    { type: "walk", minutes: Math.round(walkDMin * 10) / 10,
                      coords: [[da.lat, da.lng], [dest.lat, dest.lng]] },
                  ],
                  eta_p50_min: Math.round(total * 10) / 10,
                  // D2: bands cover the whole journey incl. final walk;
                  // A3: ratios derive from the trained quantile cells
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
                if (options.length >= 30) break;
              }
              if (options.length >= 30) break;
            }
            if (options.length >= 30) break;
          }
          if (options.length >= 30) break;
        }

        // ---- one-transfer search (same-stop transfers; mirrors local API) ----
        // Port of api/app.py v0.6 transfer planner: ride pattern A from the
        // origin stop to any downstream stop that pattern B serves before the
        // destination alight point. 2-min transfer buffer + route-hour waits,
        // second-leg speed taken at the hour the transfer actually happens.
        // ---- transfer search only when directs are insufficient ----
        try {
          if (options.length >= Math.max(4, maxO) || crowFlyM < 3000) {
            /* direct answers (or a walkable trip) suffice; skip the scan */
          const maxLegM = crowFlyM * 1.8 + 6000;   // prune far-out zigzags

          // dest side: stop -> [[pj, j1, j2, d-index]] for every stop before alight
          const destMap = new Map();
          dNear.forEach((da, di) => {
            for (const [pj, j2] of (da.pats || [])) {
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

          const waitOf = (route, hour) => {
            // B4: real service only — headway ≤ 1h (beyond that is dormant-
            // route schedule arithmetic, not service); ±1h grace for sparse
            // rows; quoted waits cap at 60 min so sparse routes rank worse
            for (const dh of [0, -1, 1]) {
              const h = ((hour + dh) % 24 + 24) % 24;
              const w = M.headway[route]?.[h];
              if (w != null && w <= 3600)
                return Math.min(60, Math.max(0.75, w / 120));
            }
            return null;
          };
          const speedOf = (route, hour) =>
            M.speeds[route]?.[hour] ?? M.route_avg[route]
            ?? M.city_hour[hour] ?? 18;

          const seenX = new Set();
          let evaluated = 0;
          scan:
          for (const oa of oNear) {
            for (const [pi, i] of (oa.pats || [])) {
              const A = M.patterns[pi];
              if (!A) continue;
              let dA = 0;
              const lim = Math.min(A.s.length - 1, i + 90);
              for (let k = i + 1; k <= lim; k++) {
                const p0 = M.stops[A.s[k - 1]], p1 = M.stops[A.s[k]];
                if (p0 && p1) dA += haversineM(p0[0], p0[1], p1[0], p1[1]);
                if (dA > maxLegM) break;          // dA only grows
                const tstop = A.s[k];
                const cands = destMap.get(tstop);
                if (!cands) continue;
                for (const [pj, j1, j2, di] of cands) {
                  if (pi === pj) continue;
                  const B = M.patterns[pj];
                  if (!B || A.r === B.r) continue;
                  const da = dNear[di];
                  const key = A.r + "|" + B.r + "|" + tstop
                            + "|" + oa.sid + "|" + da.sid;
                  if (seenX.has(key)) continue;
                  seenX.add(key);

                  // CPU guard: counts only real candidates, placed before the
                  // expensive dB sweep (was after it — cheap prunes skipped
                  // counting and hub stops could blow the budget)
                  if (++evaluated > 400) break scan;

                  let dB = 0;
                  for (let m = j1 + 1; m <= j2; m++) {
                    const t0 = M.stops[B.s[m - 1]], t1 = M.stops[B.s[m]];
                    if (t0 && t1) dB += haversineM(t0[0], t0[1], t1[0], t1[1]);
                  }
                  if (dB > 2 * maxLegM) continue;

                  const walkOMin = oa.d * 1.3 / 80;
                  const walkDMin = da.d * 1.3 / 80;
                  const waitA = waitOf(A.r, hour0);
                  if (waitA == null) continue;      // B4: A not running this hour
                  const rideA = dA * detourOf(A.r) * 3.6 / speedOf(A.r, hour0) / 60;
                  const thour = Math.floor(
                    (depEpoch + (walkOMin + waitA + rideA + 2) * 60 + 19800)
                    / 3600 % 24);
                  const waitB = waitOf(B.r, thour);
                  if (waitB == null) continue;      // B4: B not running at transfer hour
                  const xferMin = 2 + waitB;         // 2-min transfer buffer
                  const rideB = dB * detourOf(B.r) * 3.6 / speedOf(B.r, thour) / 60;
                  const total = walkOMin + waitA + rideA + xferMin
                              + rideB + walkDMin;

                  const loA = [], loB = [];
                  for (let k2 = i; k2 <= k; k2++) {
                    const s = M.stops[A.s[k2]]; if (s) loA.push([s[0], s[1]]);
                  }
                  for (let m = j1; m <= j2; m++) {
                    const s = M.stops[B.s[m]]; if (s) loB.push([s[0], s[1]]);
                  }
                  const ts = M.stops[tstop];
                  const rA = M.route_names[A.r] || A.r;
                  const rB = M.route_names[B.r] || B.r;
                  const rideMin = rideA + rideB;

                  options.push({
                    transfers: 1,
                    total_min: Math.round(total * 10) / 10,
                    walk_o: Math.round(walkOMin * 10) / 10,
                    walk_d: Math.round(walkDMin * 10) / 10,
                    wait_min: Math.round((waitA + xferMin) * 10) / 10,
                    ride_min: Math.round(rideMin * 10) / 10,
                    route_id: A.r + "+" + B.r,
                    route_code: rA + "+" + rB,
                    route_name: rA + " + " + rB,
                    board: oa.sid, board_name: oa.name,
                    alight: da.sid, alight_name: da.name,
                    transfer_stop: tstop,
                    transfer_name: ts ? ts[2] : tstop,
                    transfer_coord: ts ? [ts[0], ts[1]] : null,
                    board_at: new Date((depEpoch + (walkOMin + waitA) * 60) * 1000).toISOString(),
                    arrive: new Date((depEpoch + total * 60) * 1000).toISOString(),
                    legs: [
                      { type: "walk", minutes: Math.round(walkOMin * 10) / 10,
                        coords: [[origin.lat, origin.lng], [oa.lat, oa.lng]] },
                      { type: "bus", minutes: Math.round(rideA * 10) / 10,
                        route: rA, stops: loA },
                      { type: "transfer", minutes: Math.round(xferMin * 10) / 10,
                        at: tstop, walk_min: 0 },
                      { type: "bus", minutes: Math.round(rideB * 10) / 10,
                        route: rB, stops: loB },
                      { type: "walk", minutes: Math.round(walkDMin * 10) / 10,
                        coords: [[da.lat, da.lng], [dest.lat, dest.lng]] },
                    ],
                    eta_p50_min: Math.round(total * 10) / 10,
                    // A3: per-leg band ratios from the trained quantile cells
                    eta_p10_min: Math.round((total
                      - rideA * (1 - bandOf(A.r, hour0)[0])
                      - rideB * (1 - bandOf(B.r, thour)[0])) * 10) / 10,
                    eta_p90_min: Math.round((total
                      + rideA * (bandOf(A.r, hour0)[1] - 1)
                      + rideB * (bandOf(B.r, thour)[1] - 1)) * 10) / 10,
                    confidence: "edge-transfer-route-hour",
                  });
                  // A5: usual time — both legs re-timed at their quieter hours
                  {
                    const uhA = M.usual_hour?.[A.r], uhB = M.usual_hour?.[B.r];
                    if (uhA != null && uhB != null) {
                      const wAU = usualWaitOf(A.r, uhA);
                      const wBU = usualWaitOf(B.r, uhB);
                      if (wAU != null && wBU != null) {
                        const rAU = dA * detourOf(A.r) * 3.6
                          / speedOf(A.r, uhA) / 60;
                        const rBU = dB * detourOf(B.r) * 3.6
                          / speedOf(B.r, uhB) / 60;
                        const up50 = walkOMin + wAU + rAU + 2 + wBU
                                   + rBU + walkDMin;
                        const last = options[options.length - 1];
                        last.usual_p50_min = Math.round(up50 * 10) / 10;
                        last.usual_hour_ist = Math.min(uhA, uhB);
                        last.rush_effect_min =
                          Math.round((total - up50) * 10) / 10;
                      }
                    }
                  }
                  if (options.length >= 60) break scan;
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
                     : (asOfNote || "no route found within 1 transfer") });
      }

      // ---- lookups (existing) ----
      if (p === "/api/lookup" || p === "/lookup") {
        // use D1 for detailed lookups
        const route = url.searchParams.get("route");
        const hour = parseInt(url.searchParams.get("hour") || "9");
        const M2 = await getModel(env);
        const sp = M2.speeds[route]?.[hour] ?? M2.route_avg[route]
          ?? M2.city_hour[hour] ?? 18;
        const hw = M2.headway[route]?.[hour];
        const city = M2.city_hour[hour] ?? 18;
        return j({
          route, hour,
          route_hour_kmh: M2.speeds[route]?.[hour] ?? null,
          route_avg_kmh: M2.route_avg[route] ?? null,
          expected_wait_min: hw ? Math.min(15, Math.max(0.75, hw/120)) : null,
          city_hour_kmh: city,
          source: M2.speeds[route]?.[hour] ? "route-hour" :
            (M2.route_avg[route] ? "route-avg" : "city"),
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
                               "/api/lookup?route=&hour="],
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
