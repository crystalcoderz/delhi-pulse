# Delhi Pulse — deployed API spec (D8)

The deployed (edge) API is documented as an **OpenAPI 3.1** document:

- **Spec file:** [`workers/public/openapi.json`](../workers/public/openapi.json)
- **Live (deployed):** <https://delhi-pulse.prayaas.workers.dev/openapi.json>
  (also reachable at <https://delhi-pulse.prayaas.workers.dev/api/openapi.json>)

The spec covers the read API only — `/api/health`, `/api/status`,
`/api/journeys`, `/api/stops/search`, `/api/stops/nearby`, `/api/pulse`,
`/api/geo`, `/api/maps-config` — plus the spec route itself. The three billed
Google proxies (`/api/places/autocomplete`, `/api/places/resolve`,
`/api/gmaps-drive`) are origin-gated and intentionally not part of the public
spec.

Every endpoint carries one example response taken from the frozen contract in
`docs/v0-brief.md` §3.1–§3.4 or from real captured responses (17 Nov 2026,
full-month retrain model; the `/api/journeys` example is the Kashmere Gate →
Nehru Place capture quoted in the brief, including `route_code` and the
additive `usual_p50_min` / `usual_hour_ist` / `rush_effect_min` fields).

The contract remains frozen per `docs/v0-brief.md`: fields may be **added**,
never renamed or removed. When a field is added to a Worker response, update
`workers/public/openapi.json` in the same change.

Maintenance notes:

- `/openapi.json` is served straight from the static-assets layer
  (`workers/public/`); the Worker additionally mirrors it at
  `/api/openapi.json` (`cache-control: public, max-age=3600`).
- After a deploy, a one-line sanity check is:
  `curl -s https://delhi-pulse.prayaas.workers.dev/openapi.json | python -c "import json,sys; json.load(sys.stdin)"`
