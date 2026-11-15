/**
 * Delhi Pulse edge worker — the model on Cloudflare's network.
 * Serves the real speed lookups (D1) + city curve + replay window streaks.
 * The full planner port lands here once stop-level GTFS data exists.
 */
const JSONH = { "content-type": "application/json" };
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,OPTIONS",
  "access-control-allow-headers": "*",
};

const j = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { ...JSONH, ...CORS } });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const db = env.delhi_pulse;
    const p = url.pathname;

    try {
      if (p === "/health") {
        const n = await db.prepare(
          "SELECT COUNT(*) n FROM route_hour_real").first();
        return j({ ok: true, route_hour_cells: n?.n ?? 0 });
      }

      if (p === "/lookup") {
        const route = url.searchParams.get("route");
        const hour = parseInt(url.searchParams.get("hour") ?? "-1", 10);
        if (!route) return j({ error: "route required" }, 400);
        const rh = await db.prepare(
          "SELECT speed_kmh, n FROM route_hour_real WHERE route_id=? AND hour_ist=?")
          .bind(route, hour).first();
        const rs = await db.prepare(
          "SELECT speed_kmh, days FROM route_speed_real WHERE route_id=?")
          .bind(route).first();
        const city = await db.prepare(
          "SELECT speed_kmh FROM city_hour_real WHERE hour_ist=?")
          .bind(hour).first();
        return j({
          route, hour,
          route_hour_kmh: rh?.speed_kmh ?? null,
          route_hour_n: rh?.n ?? null,
          route_avg_kmh: rs?.speed_kmh ?? null,
          city_hour_kmh: city?.speed_kmh ?? null,
          source: rh ? "route-hour" : (rs ? "route-avg" : "city"),
        });
      }

      if (p === "/pulse") {
        const { results } = await db.prepare(
          "SELECT hour_ist, speed_kmh FROM city_hour_real ORDER BY 1").all();
        return j({
          city_hour: Object.fromEntries(results.map(r => [r.hour_ist, r.speed_kmh])),
          source: "real-ping-pairs @ edge",
        });
      }

      if (p === "/meta") {
        const { results } = await db.prepare("SELECT k, v FROM meta").all();
        return j(Object.fromEntries(results.map(r => [r.k, r.v])));
      }

      return j({ error: "not found", routes: ["/health", "/lookup", "/pulse", "/meta"] }, 404);
    } catch (e) {
      return j({ error: String(e) }, 500);
    }
  },
};
