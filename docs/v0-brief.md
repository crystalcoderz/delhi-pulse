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
  origin under `/api/...` (CORS `*`, so an external UI can call it too).

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
    &depart=2026-11-18T09:00:00%2B05:30   (optional ISO; omit = now)
    &max=4                                 (default 3, max 6)
```
Response:
```json
{
  "options": [
    {
      "transfers": 0,
      "total_min": 54.9,
      "walk_o": 4.6, "walk_d": 7.3,
      "wait_min": 4.5, "ride_min": 38.5,
      "route_id": "425UP", "route_name": "425UP",
      "board": "267", "board_name": "ISBT Kashmere Gate Terminal",
      "alight": "2559", "alight_name": "Nehru Place Terminal",
      "board_at": "2026-11-18T03:27:00.000Z",
      "arrive": "2026-11-18T04:21:00.000Z",
      "legs": [
        {"type":"walk","minutes":4.6,"coords":[[28.6673,77.2285],[28.6712,77.2307]]},
        {"type":"bus","minutes":38.5,"route":"425UP","stops":[[28.67,77.23], ["...","[lat,lng] pairs of every stop the bus passes"]]},
        {"type":"walk","minutes":7.3,"coords":[[28.5972,77.2504],[28.5494,77.2514]]}
      ],
      "eta_p50_min": 54.9, "eta_p10_min": 47.1, "eta_p90_min": 63.9,
      "confidence": "edge-route-hour"
    }
  ],
  "note": null
}
```
Field notes: `route_name` for transfer options looks like `"502DOWN + 47ADOWN"`,
plus three extra top-level fields: `transfer_stop`, `transfer_name`,
`transfer_coord`. A transfer option simply has 5 legs —
`walk → bus → {type:"transfer", minutes, at, walk_min} → bus → walk` —
so render the timeline generically over `legs`, not per transfer-type.
`legs[].coords/legs[].stops` are `[lat,lng]` pairs you draw as polylines.

### 3.2 Search: stops + any Delhi landmark (two groups in the dropdown)
```
GET /api/stops/search?q=kashmere            → [{"stop_id":"267","name":"ISBT Kashmere Gate Terminal","lat":28.66,"lng":77.22}, ...]
GET /api/places/autocomplete?input=Nehru+Pla → {"suggestions":[{"id":"ChIJf6g...","text":"Nehru Planetarium, Teen Murti Marg...","types":["establishment","planetarium"]}, ...]}
GET /api/places/resolve?place_id=ChIJf6g...  → {"place_id":"...","name":"Nehru Planetarium","address":"...","lat":28.5945,"lng":77.2100,"types":[...]}
```
Flow: pick a stop → plan with `stop:<id>` (no walk-time error); pick a
place → resolve to coords → plan with `lat,lng`.

### 3.3 Google comparison strip (server-side Google Routes API)
```
GET /api/gmaps-drive?from=28.6673,77.2285&to=28.5494,77.2514&depart=2026-11-18T03:30:00.000Z
→ {"drive_min":46.5,"drive_km":21.9,"source":"google-routes-v2-traffic-aware"}
```
(opens in ~500 ms; fetch it AFTER journeys render and prepend a strip —
never block the main results on it).

### 3.4 Nearby stops, city pulse, route shapes, status
```
GET /api/stops/nearby?lat=28.6428&lng=77.2211&radius=300
→ [{"stop_id":"...","name":"...","lat":...,"lng":...,"distance_m":142,"walk_min":2.3}, ...]

GET /api/pulse
→ {"city_hour":{"0":18.57,"1":19.23,...,"23":19.03},"source":"real-ping-pairs @ edge"}

GET /api/geo?hour=9&limit=800      (City Pulse page data: GeoJSON features,
                                     lines colored by observed speed)

GET /api/status → {"ok":true,"routes":2279,"patterns":3498,...}
GET /api/lookup?route=425UP&hour=9 → per-route speed + expected wait
```

## 4. Current design language (tokens to keep or consciously evolve)

```
--bg: #0b0d14; --panel: #14171f; --card: #1a1e2a; --border: #252b3d;
--ink: #e8eaf2; --dim: #8b90a5; --acc: #3ddc97 (origin/primary);
--acc2: #2ea5f7 (destination/secondary);
font: 'Segoe UI', system-ui, -apple-system, sans-serif
```
Dark, dense, data-forward; rounded-12 cards, MapLibre with CARTO dark-matter
basemap; A marker = `--acc`, B marker = `--acc2`. Leg colors: walk = dashed
dim gray, bus = route polyline in `--acc2`/route color, transfer = small
interchange chip at the stop.

## 5. What the current UI does (map the flows 1:1)

1. **Plan a journey:** from/to inputs (text), datetime-local `when`
   (default now), optional `Time-travel` date to replay October observed
   speeds, Find Journeys button. Map click alternates origin/destination.
2. **Results:** option cards sorted by `total_min`: big ETA + p10–p90
   range, badges (walk-to-stop mins 🚶, route chip(s) 🚌, transfer stop ⇄,
   wait ⏱, walk-from ⏱), "Board X → change at Y → alight Z", arrive clock
   time. Clicking a card draws its legs; first card auto-selected.
3. **Map:** A/B markers, leg polylines, station dots at board/transfer/alight.
4. **Header stats:** routes · 839k quantile cells · trips observed.
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
- API base: `https://delhi-pulse.prayaas.workers.dev` (CORS open) — safe in
  dev and in deploys on other domains (Cloudflare Pages etc.).
- Types: model each response in `types.ts` from §3 — they are exact.
- Map: `react-map-gl` (MapLibre flavor) or plain maplibre-gl — vendored
  copies already ship with the project if porting into the existing page.
