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
 *   /api/geo?hour=&limit=                    → route shapes colored by speed
 *   /api/health                              → system status
 *
 * Google key lives as the Worker secret GOOGLE_MAPS_KEY and is never
 * shipped to clients.
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
      // Key = env.GOOGLE_MAPS_KEY (Cloudflare secret). Never sent to clients.
      const GKEY = env.GOOGLE_MAPS_KEY || null;
      // The Referer lets the key carry an HTTP-referrer restriction that also
      // covers these server-side calls.
      const gHead = (extra) => ({ "content-type": "application/json",
                                  "x-goog-api-key": GKEY,
                                  referer: "https://delhi-pulse.prayaas.workers.dev/",
                                  ...extra });

      // Browser config for the Maps JavaScript API basemap.
      if (p === "/api/maps-config" || p === "/maps-config") {
        if (!GKEY) return j({ key: null, note: "maps not configured" }, 503);
        return new Response(JSON.stringify({
          key: GKEY, mapId: env.GOOGLE_MAPS_MAP_ID || "DEMO_MAP_ID" }), {
          headers: { ...JSONH, ...CORS, "cache-control": "public, max-age=300" } });
      }
      // Delhi NCR bounding box used to scope every Places lookup
      const DELHI_BOX = { rectangle: {
        low: { latitude: 28.15, longitude: 76.60 },
        high: { latitude: 29.00, longitude: 77.65 } } };

      // typing suggestions for the planner search box
      if (p === "/api/places/autocomplete" || p === "/places/autocomplete") {
        const input = (url.searchParams.get("input")
                     || url.searchParams.get("q") || "").slice(0, 120);
        if (!input) return j({ suggestions: [] }, 400);
        if (!GKEY) return j({ suggestions: [],
                              note: "places search not configured" });
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
        if (!r.ok) return j({ suggestions: [], note: `google ${r.status}` }, 502);
        const d = await r.json();
        const sugg = (d.suggestions || [])
          .map(s => { const pr = s.placePrediction || {};
            return { id: pr.placeId, text: (pr.text || {}).text,
                     types: (pr.types || []).slice(0, 3) }; })
          .filter(x => x.id && x.text);
        return j({ suggestions: sugg.slice(0, 6),
                   source: "google-places-new" });
      }

      // place id → coordinates (for journey search after suggestion click)
      if (p === "/api/places/resolve" || p === "/places/resolve") {
        const pidRaw = url.searchParams.get("place_id") || "";
        const pid = pidRaw.replace(/^places\//, "");
        if (!/^[A-Za-z0-9_-]{10,200}$/.test(pid))
          return j({ error: "valid place_id required" }, 400);
        if (!GKEY) return j({ error: "places not configured" }, 503);
        const r = await fetch(
          `https://places.googleapis.com/v1/places/${encodeURIComponent(pid)}`
          + "?$fields=id,location,displayName,formattedAddress,types", {
            headers: gHead(),
          });
        if (!r.ok) return j({ error: `google ${r.status}` }, 502);
        const d = await r.json();
        const loc = d.location || {};
        return j({ place_id: d.id || pid,
                   name: (d.displayName || {}).text || "",
                   address: d.formattedAddress || null,
                   lat: loc.latitude ?? null, lng: loc.longitude ?? null,
                   types: (d.types || []).slice(0, 3),
                   source: "google-places-new" });
      }

      // live google traffic-aware DRIVING time for the UI comparison strip
      if (p === "/api/gmaps-drive" || p === "/gmaps-drive") {
        const from = url.searchParams.get("from") || "";
        const to = url.searchParams.get("to") || "";
        const departT = url.searchParams.get("depart") || "";
        const m1 = from.match(/^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$/);
        const m2 = to.match(/^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$/);
        if (!m1 || !m2) return j({ error: "from,to must be lat,lng" }, 400);
        if (!GKEY) return j({ drive_min: null, note: "not configured" });
        const body = {
          origin: { location: { latLng: {
            latitude: parseFloat(m1[1]), longitude: parseFloat(m1[2]) } } },
          destination: { location: { latLng: {
            latitude: parseFloat(m2[1]), longitude: parseFloat(m2[2]) } } },
          travelMode: "DRIVE",
          routingPreference: "TRAFFIC_AWARE",
        };
        if (departT) {
          // Google requires departureTime within [now, now + 7 days]; outside
          // that window it 400s, so let it default to live traffic instead
          const t = new Date(departT).getTime();
          const now = Date.now();
          if (t > now && t < now + 7 * 24 * 3600 * 1000)
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
          return j({ drive_min: null, note: `google ${r.status}` });
        const d = await r.json();
        if (!d.routes || !d.routes.length) return j({ drive_min: null });
        const dur = parseFloat(d.routes[0].duration);
        return j({
          drive_min: dur ? Math.round(dur / 60 * 10) / 10 : null,
          drive_km: Math.round((d.routes[0].distanceMeters || 0) / 100) / 10,
          source: "google-routes-v2-traffic-aware",
        });
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

        const G = getRouteGeo(M);
        const feats = [];
        const add = (g, sp, source) => feats.push(
          '{"type":"Feature","properties":' + JSON.stringify({
            route: g.route, name: g.name,
            speed_kmh: Math.round(sp * 10) / 10,
            color: speedColor(sp), source }) + "," + g.geom + "}");
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
          + '],"hour":' + hour + ',"day":null}',
          { headers: { ...JSONH, ...CORS,
                       "cache-control": "public, max-age=3600" } });
      }

      // ---- JOURNEYS: full planner (inlined for same scoping pattern) ----
      if (p === "/api/journeys" || p === "/journeys") {
        const fromT = url.searchParams.get("from");
        const toT = url.searchParams.get("to");
        const departT = url.searchParams.get("depart");
        const maxO = parseInt(url.searchParams.get("max") || "3", 10);

        if (!fromT || !toT) return j({ error: "from, to required" }, 400);

        // resolve places
        function resolve(text) {
          text = (text||"").trim();
          const mc = text.match(/^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$/);
          if (mc) return { lat: parseFloat(mc[1]), lng: parseFloat(mc[2]) };
          if (text.startsWith("stop:") && M.stops[text.slice(5)]) {
            const s = M.stops[text.slice(5)];
            return { lat: s[0], lng: s[1], stop_id: text.slice(5) };
          }
          const lower = text.toLowerCase();
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

        const depEpoch = departT ? new Date(departT).getTime() / 1000 : Date.now() / 1000;
        const hour0 = Math.floor((depEpoch + 19800) / 3600 % 24);

        // Always include nearby stops, plus the exact stop if stop: given
        let oNear, dNear;
        const mkNearby = (lat, lng, k=5, limit=2500) =>
          Object.keys(M.stops)
            .map(sid => { const s = M.stops[sid];
              return { sid, d: haversineM(lat, lng, s[0], s[1]),
                       name: s[2], lat: s[0], lng: s[1] }; })
            .filter(x => (M.membership[x.sid] || []).length > 0)
            .sort((a,b) => a.d - b.d).slice(0, k)
            .filter(x => x.d <= limit);

        oNear = mkNearby(origin.lat, origin.lng, 12)          .filter(x => (M.membership[x.sid] || []).length > 0);
        if (origin.stop_id && M.stops[origin.stop_id]) {
          const s = M.stops[origin.stop_id];
          const exact = { sid: origin.stop_id, name: s[2],
                          lat: s[0], lng: s[1], d: 0, walk_min: 0 };
          oNear = [exact, ...oNear.filter(n => n.sid !== origin.stop_id)];
        }
        dNear = mkNearby(dest.lat, dest.lng, 12)
          .filter(x => (M.membership[x.sid] || []).length > 0);
        if (dest.stop_id && M.stops[dest.stop_id]) {
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
          return j({ options: [], note: "no bus stops within walking distance" });
        }

        // direct journey search
        const options = [];
        const seen = new Set();
        for (const oa of oNear) {
          for (const da of dNear) {
            const oPat = M.membership[oa.sid] || [];
            const dPat = M.membership[da.sid] || [];
            for (const [pi, i] of oPat) {
              for (const [pj, j2] of dPat) {
                if (pi !== pj || j2 <= i) continue;
                const pat = M.patterns[pi];
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

                const sp = M.speeds[route]?.[hour0]
                  ?? M.route_avg[route]
                  ?? M.city_hour[hour0] ?? 18;
                const rideS = distM * 3.6 / sp;
                const hw = M.headway[route]?.[hour0];
                const waitMin = hw ? Math.min(15, Math.max(0.75, hw/120)) : 5;
                const walkOMin = oa.d * 1.3 / 80;
                const walkDMin = da.d * 1.3 / 80;
                const total = walkOMin + waitMin + rideS/60 + walkDMin;

                const key = route + "|" + oa.sid + "|" + da.sid;
                if (seen.has(key)) continue;
                seen.add(key);

                options.push({
                  transfers: 0,
                  total_min: Math.round(total * 10) / 10,
                  walk_o: Math.round(walkOMin * 10) / 10,
                  walk_d: Math.round(walkDMin * 10) / 10,
                  wait_min: Math.round(waitMin * 10) / 10,
                  ride_min: Math.round(rideS / 60 * 10) / 10,
                  route_id: route,
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
                  eta_p10_min: Math.round((walkOMin + waitMin + rideS*0.85/60) * 10) / 10,
                  eta_p90_min: Math.round((walkOMin + waitMin + rideS*1.35/60) * 10) / 10,
                  confidence: "edge-route-hour",
                });
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
        try {
          const crowFlyM = haversineM(origin.lat, origin.lng, dest.lat, dest.lng);
          const maxLegM = crowFlyM * 1.8 + 6000;   // prune far-out zigzags

          // dest side: stop -> [[pj, j1, j2, d-index]] for every stop before alight
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

          const waitOf = (route, hour) => {
            const h = M.headway[route]?.[hour];
            return h ? Math.min(15, Math.max(0.75, h / 120)) : 5;
          };
          const speedOf = (route, hour) =>
            M.speeds[route]?.[hour] ?? M.route_avg[route]
            ?? M.city_hour[hour] ?? 18;

          const seenX = new Set();
          let evaluated = 0;
          scan:
          for (const oa of oNear) {
            for (const [pi, i] of (M.membership[oa.sid] || [])) {
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

                  let dB = 0;
                  for (let m = j1 + 1; m <= j2; m++) {
                    const t0 = M.stops[B.s[m - 1]], t1 = M.stops[B.s[m]];
                    if (t0 && t1) dB += haversineM(t0[0], t0[1], t1[0], t1[1]);
                  }
                  if (dB > 2 * maxLegM) continue;

                  if (++evaluated > 200) break scan;   // CPU guard

                  const walkOMin = oa.d * 1.3 / 80;
                  const walkDMin = da.d * 1.3 / 80;
                  const waitA = waitOf(A.r, hour0);
                  const rideA = dA * 3.6 / speedOf(A.r, hour0) / 60;
                  const thour = Math.floor(
                    (depEpoch + (walkOMin + waitA + rideA) * 60 + 19800)
                    / 3600 % 24);
                  const waitB = waitOf(B.r, thour);
                  const xferMin = 2 + waitB;            // 2-min transfer buffer
                  const rideB = dB * 3.6 / speedOf(B.r, thour) / 60;
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
                    eta_p10_min: Math.round((total - 0.15 * rideMin) * 10) / 10,
                    eta_p90_min: Math.round((total + 0.35 * rideMin) * 10) / 10,
                    confidence: "edge-transfer-route-hour",
                  });
                  if (options.length >= 60) break scan;
                }
              }
            }
          }
        } catch (e) { /* transfer search must never break direct results */ }

        options.sort((a, b) => a.total_min - b.total_min);
        options.forEach(o => delete o._key);

        return j({ options: options.slice(0, Math.min(maxO, 6)),
                   note: options.length ? null : "no route found within 1 transfer" });
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
          edges: 0,
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
                               "/api/pulse", "/api/geo?hour=&limit=",
                               "/api/lookup?route=&hour="],
                   docs: "github.com/crystalcoderz/delhi-pulse" });
      }

      return j({ error: "not found" }, 404);
    } catch (e) {
      return j({ error: String(e) }, 500);
    }
  },
};
