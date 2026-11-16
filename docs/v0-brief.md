# Delhi Pulse — UI redesign brief for v0

Copy this whole file into v0 as context. It contains everything a redesign
needs: what the product is, the **live, frozen API contract with real example
responses**, the data shapes, design tokens, constraints, and a prioritized
wishlist. No endpoint here is speculative — every example below was captured
from the production deployment.

## 1. What you are redesigning (30 seconds)

**Delhi Pulse** is a real-time bus traffic model and journey planner for
Delhi. 31 days of October 2026 DTC bus GPS (~anonymized hundreds of millions
of pings) were processed into a per-(route, edge, hour) travel-time model,
served from Cloudflare's edge. A user types any Delhi landmark (DTC stop,
mall, hospital, market — Google Places) and receives journey options with
walk → wait → ride legs, transfer guidance, ETA bands (p10/p50/p90) — plus a
live "Google Maps says X min by car with traffic" comparison strip. There is
also a **City Pulse** page that paints every bus route on a dark map colored
by observed speed, with an hour slider.

- Live planner: **https://delhi-pulse.prayaas.workers.dev/**
- City Pulse page: **https://delhi-pulse.prayaas.workers.dev/pulse.html**
- Both are static assets on one Cloudflare Worker; the API lives at the same
  origin under `/api/...`. The read API is CORS `*`, so an external UI can
  call it too; the three billed Google proxies are origin-gated (see §7).

## 2. Non-negotiable constraints

1. **Deployment target is static.** The UI ships as static files on a
   Cloudflare Worker (no Node server, no SSR). Preferred v0 output: a
   client-only React app (Vite is ideal), deployable as static files. If v0
   defaults to Next.js, say "static export, client components only, no
   server actions".
2. **Map library is MapLibre GL JS** (vendored `maplibre-gl.js` + css, no
   build step needed) with the free CARTO dark-matter style:
   `https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json`.
   Do NOT swap in Google Maps JS or Leaflet.

   > **Editorial note (16 Nov 2026, post-redesign):** the *deployed* UI did
   > exactly that swap — it now renders Google Maps JS with a
   > referrer-restricted browser key fetched at runtime from
   > `/api/maps-config` (documented in §3.4). The MapLibre rule above was the
   > pre-redesign assumption and is kept verbatim for history; it is no
   > longer enforced in production. For a fresh v0 build either library is
   > acceptable — but if you keep Google Maps JS, take the key from
   > `/api/maps-config` (never hardcode it), and constraint 3 still applies
   > to every *billed server* call: Places and Drive remain server-side
   > proxies.
3. **Never embed API keys in the client.** All third-party calls go through
   our server-side proxies (`/api/places/*`, `/api/gmaps-drive`). The client
   only ever talks to our own endpoints.
4. **Don't redesign the API.** The contract below is frozen and live.
   Query it with `fetch`; JSON everywhere.
5. Keep the two core UX behaviors that make the demo: (a) typing shows two
   result groups — `🚏 Bus stops` (instant) and `📍 Places · Google`
   (server-proxied autocomplete); (b) after planning, the Google-drive
   comparison strip appears beside our best option.
6. Mobile matters: most judging/browsing happens on phones. Design
   mobile-first, map as the hero, panel as a bottom sheet.

## 3. Frozen API contract (all live, all verified)

Base URL (same origin; use `location.origin` or absolute in dev):
`https://delhi-pulse.prayaas.workers.dev`

### 3.1 Journey search — the heart of the app
```
GET /api/journeys?from={lat,lng | stop:<id> | plain-text}&to={same}
    &depart=2026-11-18T09:00:00%2B05:30   (optional ISO 8601; omit = now.
                                           A literal space works as `+`, naive
                                           local times read as IST, and
                                           unparseable values → 400 with a
                                           hint)
    &max=4                                 (default 3, clamped 1..6)
    &asOf=2026-10-20T09:00:00%2B05:30      (optional time-travel replay of a
                                           past day — LOCAL API ONLY: the edge
                                           accepts it but only answers with
                                           note "asOf time-travel replay is
                                           served by the full local API; the
                                           edge serves the pooled full-month
                                           model")
```
Response (captured live from the production edge on 16 Nov 2026, full-month
retrain model — Kashmere Gate → Nehru Place, 09:00 IST):
```json
{
  "options": [
    {
      "transfers": 0,
      "total_min": 69.9,
      "walk_o": 1.8, "walk_d": 3.9,
      "wait_min": 6.2, "ride_min": 58,
      "route_id": "1920", "route_code": "425UP", "route_name": "425UP",
      "board": "4109", "board_name": "ISBT Kashmere Gate Terminal",
      "alight": "2559", "alight_name": "Nehru Place",
      "board_at": "2026-11-18T03:37:59.931Z",
      "arrive": "2026-11-18T04:39:52.258Z",
      "legs": [
        {"type":"walk","minutes":1.8,"coords":[[28.6673,77.2285],[28.66696,77.227456]]},
        {"type":"bus","minutes":58,"route":"425UP","stops":[[28.66696,77.227456], "... one [lat,lng] pair per stop the bus passes ..."]},
        {"type":"walk","minutes":3.9,"coords":[[28.548576,77.249133],[28.5494,77.2514]]}
      ],
      "eta_p50_min": 69.9, "eta_p10_min": 54.2, "eta_p90_min": 90.2,
      "confidence": "edge-route-hour",
      "usual_p50_min": 78.6, "usual_hour_ist": 11, "rush_effect_min": -8.8
    }
  ],
  "note": null
}
```
Field notes:
- `route_id` is the internal route key (`"1920"`); `route_code` (added
  16 Nov 2026, additive — nothing renamed) is the display code (`"425UP"`);
  `route_name` stays the human label. Transfer options join both legs:
  `route_id`/`route_code` with `"+"`, `route_name` with `" + "`, plus three
  extra top-level fields: `transfer_stop`, `transfer_name`, `transfer_coord`.
  A transfer option simply has 5 legs —
  `walk → bus → {type:"transfer", minutes, at, walk_min} → bus → walk` —
  so render the timeline generically over `legs`, not per transfer-type.
  `legs[].coords/legs[].stops` are `[lat,lng]` pairs you draw as polylines.
- `usual_p50_min` / `usual_hour_ist` / `rush_effect_min` (additive): the same
  journey re-timed at the route's quieter service hour (IST). Note
  `rush_effect_min = total_min − usual_p50_min` can be **negative** (the
  captured example is): bus speeds are nearly flat across the day and
  off-peak headways are sparser, so the "quiet" hour is not always faster.
  Surface it, don't hide it.
- `confidence` by option type on the edge: `"edge-route-hour"` (direct),
  `"edge-transfer-route-hour"` (one transfer), `"walk-only"` (crow-fly
  ≤ 250 m — that response has `route_id: null`, `route_name: "Walk"`, a
  single walk leg, and `note: "walkable trip"`). The local FastAPI still
  returns its legacy labels (`"v0.5-transfers"`, `"v0.6-walk-transfers"`) —
  treat `confidence` as an opaque display string; don't switch on exact
  values across backends.
- `board_at`/`arrive` are UTC ISO instants — render them in IST.
- Empty results keep HTTP 200 with a note:
  `{"options": [], "note": "no route found within 1 transfer"}` (also seen:
  `"cannot find \"<text>\""`, `"no bus stops within walking distance"`).

### 3.2 Search: stops + any Delhi landmark (two groups in the dropdown)
```
GET /api/stops/search?q=kashmere            → [{"stop_id":"267","name":"ISBT Kashmere Gate","lat":28.669372,"lng":77.227137}, ...]
                                                 (min 2 chars; top 10 substring matches)
GET /api/places/autocomplete?input=Nehru+Pla → {"suggestions":[{"id":"ChIJf6g...","text":"Nehru Planetarium, Teen Murti Marg...","types":["establishment","planetarium"]}, ...]}
                                                 (input min 3 chars; every Places lookup
                                                  is scoped to the tight Delhi box,
                                                  28.15–29.00 N, 76.60–77.65 E)
GET /api/places/resolve?place_id=ChIJf6g...  → {"place_id":"...","name":"Nehru Planetarium","address":"...","lat":28.5945,"lng":77.2100,"types":[...]}
```
Flow: pick a stop → plan with `stop:<id>` (no walk-time error); pick a
place → resolve to coords → plan with `lat,lng`. Stop ids are per-platform —
several ids share one station name (§3.1's 425UP boards at `4109` "ISBT
Kashmere Gate Terminal", not `267` "ISBT Kashmere Gate").

### 3.3 Google comparison strip (server-side Google Routes API)
```
GET /api/gmaps-drive?from=28.6673,77.2285&to=28.5494,77.2514&depart=2026-11-18T03:30:00.000Z
→ {"drive_min":46.5,"drive_km":21.9,"source":"google-routes-v2-traffic-aware"}
```
(opens in ~500 ms; fetch it AFTER journeys render and prepend a strip —
never block the main results on it. Server-side cache: responses are keyed
on rounded coords + 15-minute buckets, so identical calls in one bucket are
free — and equally, don't expect minute-scale traffic deltas. Geo-scope:
`from` and `to` must each fall inside a wide north-India guard box
(26.5–30.5 N, 74.5–79.5 E — deliberately wider than the Places Delhi box in
§3.2, so "Delhi-NCR" would be the wrong description), and the pair must be
within 120 km of each other).

### 3.4 Nearby stops, city pulse, route shapes, status, maps config
```
GET /api/stops/nearby?lat=28.6428&lng=77.2211&radius=300
→ [{"stop_id":"...","name":"...","lat":...,"lng":...,"distance_m":142,"walk_min":2.3}, ...]

GET /api/pulse
→ {"city_hour":{"0":18.57,"1":19.23,...,"23":19.03},"source":"real-ping-pairs @ edge"}

GET /api/geo?hour=9&limit=800&stride=1
    (City Pulse page data: a GeoJSON FeatureCollection of LineStrings colored
    by observed speed; coordinates are [lng,lat]; each feature's properties
    are {route, name, speed_kmh, color, source: "route-hour"|"route-avg",
    stride}; the top level adds hour, stride, day:null. stride 1–5 thins
    shape points (stride=2 is ~3× lighter), limit is clamped 10..1500, and
    the response carries `cache-control: public, max-age=3600`.)

GET /api/status
→ {"ok":true,"routes":2245,"patterns":3498,"edges":853480,
    "live":{"vehicles":0,"url":false,"n_fetch":0},"note":"edge deployment"}

GET /api/lookup?route=425UP&hour=9 → per-route speed + expected wait

GET /api/maps-config
→ {"key":"<referrer-restricted browser key>","mapId":"<cloud map id>"}
    (browser key for the Google Maps JS basemap — served with
    `cache-control: private, max-age=300`; a foreign Origin gets
    403 {"key":null,"note":"forbidden"}; 503 {"key":null,
    "note":"maps not configured"} when unset)
```

## 4. Current design language (tokens to keep or consciously evolve)

```
--bg: #0b0d14; --panel: #14171f; --card: #1a1e2a; --border: #252b3d;
--ink: #e8eaf2; --dim: #8b90a5; --acc: #3ddc97 (origin/primary);
--acc2: #2ea5f7 (destination/secondary);
font: 'Segoe UI', system-ui, -apple-system, sans-serif
```
Dark, dense, data-forward; rounded-12 cards; A marker = `--acc`, B marker =
`--acc2`. The old page drew legs on MapLibre with the CARTO dark-matter
basemap (pre-redesign — the deployed v2 UI renders Google Maps JS, see the
note in §2). Leg colors: walk = dashed dim gray, bus = route polyline in
`--acc2`/route color, transfer = small interchange chip at the stop.

## 5. What the current UI does (map the flows 1:1)

1. **Plan a journey:** from/to inputs (text), datetime-local `when`
   (default now), optional `Time-travel` date to replay October observed
   speeds (that field shows only against the local API — the edge hides it
   and serves the pooled full-month model, see `asOf` in §3.1), Find
   Journeys button. Map click alternates origin/destination.
2. **Results:** option cards sorted by `total_min`: big ETA + p10–p90
   range, badges (walk-to-stop mins 🚶, route chip(s) 🚌, transfer stop ⇄,
   wait ⏱, walk-from ⏱), "Board X → change at Y → alight Z", arrive clock
   time. Clicking a card draws its legs; first card auto-selected.
3. **Map:** A/B markers, leg polylines, station dots at board/transfer/alight.
4. **Header stats:** fetched live from `/api/status` — e.g. "2,245 routes ·
    853k quantile cells · October measured speeds" (no hardcoded counts).
5. **City Pulse page:** hour slider recolors every route polyline by speed
   (slow amber-red → teal), city speed curve from `/api/pulse`.

## 6. Redesign wishlist (prioritized)

**P0 — core experience**
- Journey legs as a vertical timeline inside each card (icon per leg type,
  minutes per segment, transfer stop with interchange glyph) — the single
  biggest legibility upgrade.
- Skeleton loaders (search, journeys, google strip) + empty/error states
  with retry; abort stale fetches (AbortController) when the user retypes.
- Mobile: map hero + draggable bottom sheet (collapsed = results list,
  expanded = full detail); inputs collapse to a single floating search bar.
- Departure quick chips: **Now · +15 · +30 · tomorrow 9:00** next to the
  datetime, plus keep the ISO datetime picker for precision.
- Draw the p10–p90 band as a thin range bar under the ETA number.

**P1 — delight & clarity**
- Stop/station markers with hover cards (`/api/stops/nearby` on hover, or
  pointerenter → pause); smooth `fitBounds` animation to the selected
  journey.
- Route chips colored consistently (hash the route name into a 6-color
  palette; the API gives you `route_name`, you own the colors).
- Google comparison strip redesigned as a two-bar micro-chart: 🚗 Google
  46 min vs 🚌 us 55 min + ratio badge ("1.2× bus premium").
- "Copy link" that round-trips the plan: encode `from,to,depart` in the URL
  hash so judges can share an exact query.
- Nearby stops list on the map when idle (300 m radius) — makes the empty
  state useful.

**P2 — polish**
- Accessibility: focus-visible rings, aria-live on results, keyboard
  suggestion navigation (↑↓ Enter Esc) in both search inputs — currently
  mouse-only.
- Micro-animations only (150–250 ms); the app must feel instant — the API
  p50 is ~80 ms server-side.
- Optional Hindi labels for stop types (टर्मिनल, मार्केट) as a stretch —
  Delhi audience.
- Lighthouse targets: Performance 95+, Accessibility 95+ (current page is a
  single HTML file, no build step — a big head start).

## 7. If v0 scaffolds a fresh app

- Framework: **Vite + React + Tailwind + shadcn/ui** is ideal; anything that
  exports static files works.
- API base: `https://delhi-pulse.prayaas.workers.dev`. The **read API**
  (`/api/journeys`, `/api/stops/*`, `/api/pulse`, `/api/geo`, `/api/status`,
  `/api/health`, `/api/lookup`) is CORS `*` — safe in dev and in deploys on
  other domains (Cloudflare Pages etc.). The **billed Google proxies**
  (`/api/places/autocomplete`, `/api/places/resolve`, `/api/gmaps-drive`) are
  origin-gated instead: a browser `Origin` that is not this worker,
  `localhost`/`127.0.0.1` or a `*.vercel.app` host gets
  `403 {"error":"origin not allowed"}` (non-browser callers pass the gate but
  are geo-scoped server-side — see §3.2/§3.3 for the exact boxes and the
  120 km drive cap). `/api/maps-config` is likewise origin-gated. So: call
  the read API cross-origin freely; if your v0 deploy lives on another
  domain, proxy the billed endpoints through your own server or get your
  host added to the allowlist.
- Types: model each response in `types.ts` from §3 — they are exact.
- Map: `react-map-gl` (MapLibre flavor) or plain maplibre-gl — vendored
  copies already ship with the project if porting into the existing page.
  Google Maps JS (the deployed v2 pattern, key via `/api/maps-config`) is
  equally acceptable — see the §2 note.
