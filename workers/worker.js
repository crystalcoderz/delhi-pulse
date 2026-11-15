/**
 * Delhi Pulse edge worker — the model on Cloudflare's network.
 * v2: real shape geometry (/geo), city curve (/pulse), headway lookups,
 * system readout (/status) + the interactive City Pulse page (assets).
 * Full /journeys planner lands here once stop-level traversal model ships.
 */
const JSONH = { "content-type": "application/json" };
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,OPTIONS",
  "access-control-allow-headers": "*",
};

const j = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { ...JSONH, ...CORS } });

function ramp(kmh, vmax = 36.0) {
  const t = Math.max(0, Math.min(1, kmh / vmax));
  const r = Math.round(240 + (60 - 240) * t);
  const g = Math.round(110 + (210 - 110) * t);
  const b = Math.round(40 + (160 - 40) * t);
  return `#${[r, g, b].map(x => x.toString(16).padStart(2, "0")).join("")}`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const db = env.delhi_pulse;
    const p = url.pathname;

    try {
      // ---- HTML pages + assets come from Workers Assets (wrangler handles) ----

      if (p === "/api/health" || p === "/health") {
        const n = await db.prepare(
          "SELECT COUNT(*) n FROM route_hour_real").first();
        const g = await db.prepare(
          "SELECT COUNT(*) n FROM geo_shapes").first();
        const w = await db.prepare(
          "SELECT COUNT(*) n FROM wait_headway").first();
        return j({ ok: true, route_hour_cells: n?.n ?? 0,
                   geo_routes: g?.n ?? 0, headway_cells: w?.n ?? 0 });
      }

      if (p === "/days" || p === "/api/days") {
        const m = await db.prepare(
          "SELECT v FROM meta WHERE k='days'").first();
        try {
          return j({ days: m ? JSON.parse(m.v) : [] });
        } catch {
          return j({ days: [] });
        }
      }

      if (p === "/api/geo" || p === "/geo") {
        const hour = parseInt(url.searchParams.get("hour") ?? "9", 10) || 9;
        const limit = Math.min(parseInt(url.searchParams.get("limit") ?? "800", 10) || 800, 1500);
        if (!env._geoCache) env._geoCache = {};
        const statics = env._geoCache.shapes ??=
          await db.prepare("SELECT route_id, name, coords FROM geo_shapes").all();
        const speeds = await db.prepare(
          "SELECT route_id, speed_kmh FROM route_hour_real WHERE hour_ist=?").bind(hour).all();
        const aggs = await db.prepare(
          "SELECT route_id, speed_kmh FROM route_speed_real").all();
        const agg_map = new Map(aggs.results.map(r => [r.route_id, r.speed_kmh]));
        const sp_map = new Map(speeds.results.map(r => [r.route_id, r.speed_kmh]));
        const city = await db.prepare(
          "SELECT speed_kmh FROM city_hour_real WHERE hour_ist=?").bind(hour).first();
        const citySp = city?.speed_kmh ?? 18;
        const feats = [];
        for (const r of statics.results.slice(0, limit)) {
          const sp = sp_map.get(r.route_id) ?? agg_map.get(r.route_id) ?? citySp;
          feats.push({
            type: "Feature",
            properties: { route: r.route_id, name: r.name,
                         speed_kmh: Math.round(sp * 10) / 10,
                         color: ramp(sp) },
            geometry: { type: "LineString", coordinates: JSON.parse(r.coords) },
          });
        }
        return j({ type: "FeatureCollection", features: feats, hour });
      }

      if (p === "/api/pulse" || p === "/pulse") {
        const { results } = await db.prepare(
          "SELECT hour_ist, speed_kmh FROM city_hour_real ORDER BY 1").all();
        return j({
          city_hour: Object.fromEntries(results.map(r => [r.hour_ist, r.speed_kmh])),
          source: "real-ping-pairs @ edge",
        });
      }

      if (p === "/api/lookup" || p === "/lookup") {
        const route = url.searchParams.get("route");
        const hour = parseInt(url.searchParams.get("hour") ?? "-1", 10);
        if (!route) return j({ error: "route required" }, 400);
        const rh = await db.prepare(
          "SELECT speed_kmh, n FROM route_hour_real WHERE route_id=? AND hour_ist=?")
          .bind(route, hour).first();
        const rs = await db.prepare(
          "SELECT speed_kmh, days FROM route_speed_real WHERE route_id=?")
          .bind(route).first();
        const wh = await db.prepare(
          "SELECT headway_s, services_per_day FROM wait_headway WHERE route_id=? AND hour_ist=?")
          .bind(route, hour).first();
        const city = await db.prepare(
          "SELECT speed_kmh FROM city_hour_real WHERE hour_ist=?").bind(hour).first();
        return j({
          route, hour,
          route_hour_kmh: rh?.speed_kmh ?? null, route_hour_n: rh?.n ?? null,
          route_avg_kmh: rs?.speed_kmh ?? null,
          services_per_day: wh?.services_per_day ?? null,
          expected_wait_min: wh ? Math.min(15, Math.max(0.75, wh.headway_s / 120)) : null,
          city_hour_kmh: city?.speed_kmh ?? null,
          source: rh ? "route-hour" : (rs ? "route-avg" : "city"),
        });
      }

      if (p === "/api/meta" || p === "/meta") {
        const { results } = await db.prepare("SELECT k, v FROM meta").all();
        return j(Object.fromEntries(results.map(r => [r.k, r.v])));
      }

      return j({ error: "not found",
                 routes: ["/", "/api/health", "/api/geo?hour=", "/api/pulse",
                          "/api/lookup?route=&hour=", "/api/meta"] }, 404);
    } catch (e) {
      return j({ error: String(e) }, 500);
    }
  },
};
