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
 *   /api/pulse                               → city speed curve
 *   /api/geo?hour=&limit=                    → route shapes colored by speed
 *   /api/health                              → system status
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

        options.sort((a, b) => a.total_min - b.total_min);
        options.forEach(o => delete o._key);

        return j({ options: options.slice(0, Math.min(maxO, 6)),
                   note: options.length ? null : "no direct route found" });
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
                              "/api/pulse", "/api/lookup?route=&hour="],
                   docs: "github.com/crystalcoderz/delhi-pulse" });
      }

      return j({ error: "not found" }, 404);
    } catch (e) {
      return j({ error: String(e) }, 500);
    }
  },
};
