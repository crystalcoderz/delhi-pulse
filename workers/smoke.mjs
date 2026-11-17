#!/usr/bin/env node
/**
 * Delhi Pulse edge smoke test (G1 / fix-plan H).
 *
 * Reads the deployed Worker and asserts the API invariants a judge or a
 * client depends on:
 *
 *   - options are sorted by EXPERIENCE SCORE (total + 5/transfer + walk
 *     penalty — C4) and unique per route sequence (C1)
 *   - eta_p10_min <= eta_p50_min <= eta_p90_min, and p50 == total_min (A3/D2)
 *   - arrive is after board_at, legs are non-empty, transfer options carry
 *     a transfer stop (C5 contract)
 *   - `max` is clamped to 1..6 (D3)
 *   - `asOf` returns the explanatory note ALONGSIDE options, not instead of
 *     them (D7 partial)
 *   - /status, /health, /pulse and /stops/* keep their frozen shapes
 *
 * It is read-only and deliberately small: 9 requests, sequential, 250 ms
 * apart.  Billed Google proxies (/places/*, /gmaps-drive, /maps-config) are
 * refused by assert path guard below — never call them from CI.
 *
 * Usage:
 *   node workers/smoke.mjs                       # deployed Worker
 *   EDGE_URL=http://localhost:8787 node workers/smoke.mjs
 *   node workers/smoke.mjs --base http://localhost:8787
 *
 * Writes tests/golden/edge.json (informational snapshot; CI never diffs it).
 * Exit code 0 = every hard invariant passed.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const GOLDEN = resolve(HERE, "..", "tests", "golden", "edge.json");
const MAX_REQUESTS = 10;

const argBase = (() => {
  const i = process.argv.indexOf("--base");
  return i !== -1 ? process.argv[i + 1] : null;
})();
const BASE = (process.env.EDGE_URL || argBase
  || "https://delhi-pulse.prayaas.workers.dev").replace(/\/+$/, "");

// Coordinates, not place names: the planner's name resolution is exercised
// by a soft probe below, but the hard invariants must not depend on which
// stop a fuzzy name happens to resolve to.  (28.6675,77.2281) is the ISBT
// Kashmere Gate terminal area, (28.5491,77.2533) is Nehru Place.
const FROM = "28.6675,77.2281";
const TO = "28.5491,77.2533";
const FROM_LABEL = "Kashmere Gate ISBT (coords)";
const TO_LABEL = "Nehru Place (coords)";
const DEPART = "2026-11-17T08:30:00+05:30"; // Tuesday 08:30 IST, a real service hour

const checks = [];
const warnings = [];
const requests = [];
const snapshot = {};

const assert = (cond, msg) => { if (!cond) throw new Error(msg); };
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function check(name, fn) {
  try {
    fn();
    checks.push({ name, ok: true });
    console.log(`  ok   ${name}`);
  } catch (e) {
    checks.push({ name, ok: false, error: e.message });
    console.log(`  FAIL ${name}: ${e.message}`);
  }
}

function warn(msg) {
  warnings.push(msg);
  console.log(`  warn ${msg}`);
}

/** The billed Google proxies are out of scope for a smoke run. */
function assertPathAllowed(path) {
  assert(!/places|gmaps|maps-config/i.test(path),
    `refusing to probe billed endpoint: ${path}`);
}

async function probe(path) {
  assertPathAllowed(path);
  assert(requests.length < MAX_REQUESTS,
    `smoke budget exceeded (${MAX_REQUESTS} requests)`);
  const t0 = Date.now();
  const res = await fetch(BASE + path, {
    headers: { accept: "application/json" },
    signal: AbortSignal.timeout(20000),
  });
  const ms = Date.now() - t0;
  const text = await res.text();
  requests.push({ path, status: res.status, ms });
  assert(res.ok, `HTTP ${res.status} from ${path}: ${text.slice(0, 200)}`);
  let body;
  try { body = JSON.parse(text); }
  catch { throw new Error(`${path} did not return JSON: ${text.slice(0, 120)}`); }
  return body;
}

const q = (params) => new URLSearchParams(params).toString();

/** Shared option invariants; `maxAllowed` is the effective clamp. */
function assertOptions(body, label, maxAllowed = 6) {
  assert(Array.isArray(body.options), `${label}: options is not an array`);
  assert(body.options.length <= maxAllowed,
    `${label}: ${body.options.length} options > clamp ${maxAllowed}`);
  let prev = -Infinity;
  const seen = new Set();
  const score = o => o.total_min
    + 5 * (o.transfers || 0)
    + 0.5 * Math.max(0, (o.walk_o + o.walk_d) - 10);   // C4 ranking
  for (const [i, o] of body.options.entries()) {
    const at = `${label} option[${i}]`;
    assert(isNum(o.total_min), `${at}: total_min missing`);
    assert(score(o) >= prev - 1e-9,
      `${at}: score ${score(o)} < previous ${prev} (not score-sorted)`);
    prev = score(o);

    assert(isNum(o.eta_p10_min), `${at}: eta_p10_min missing`);
    assert(isNum(o.eta_p50_min), `${at}: eta_p50_min missing`);
    assert(isNum(o.eta_p90_min), `${at}: eta_p90_min missing`);
    assert(o.eta_p10_min <= o.eta_p50_min,
      `${at}: p10 ${o.eta_p10_min} > p50 ${o.eta_p50_min}`);
    assert(o.eta_p50_min <= o.eta_p90_min,
      `${at}: p50 ${o.eta_p50_min} > p90 ${o.eta_p90_min}`);
    assert(Math.abs(o.eta_p50_min - o.total_min) <= 0.15,
      `${at}: p50 ${o.eta_p50_min} != total ${o.total_min}`);

    assert(typeof o.board_at === "string" && typeof o.arrive === "string",
      `${at}: board_at/arrive missing`);
    assert(Date.parse(o.arrive) >= Date.parse(o.board_at),
      `${at}: arrive before board_at`);
    assert(Array.isArray(o.legs) && o.legs.length > 0, `${at}: legs empty`);
    for (const leg of o.legs) {
      assert(typeof leg.type === "string" && leg.type, `${at}: leg without type`);
      assert(isNum(leg.minutes) && leg.minutes >= 0, `${at}: bad leg minutes`);
    }
    assert(o.transfers === 0 || o.transfers === 1,
      `${at}: transfers ${o.transfers} outside 0..1`);
    if (o.transfers === 1) {
      assert(o.transfer_stop && o.transfer_name,
        `${at}: transfer option without transfer_stop/name`);
    }
    if (o.route_id != null) {
      assert(typeof o.route_code === "string" && o.route_code,
        `${at}: route_code missing on a bus option`);
    }
    if (o.usual_p50_min != null) {
      assert(isNum(o.usual_p50_min), `${at}: usual_p50_min not numeric`);
      assert(Number.isInteger(o.usual_hour_ist)
        && o.usual_hour_ist >= 0 && o.usual_hour_ist <= 23,
        `${at}: usual_hour_ist ${o.usual_hour_ist}`);
      assert(isNum(o.rush_effect_min), `${at}: rush_effect_min missing`);
    }
    const key = `${o.transfers}|${o.route_id}`;
    assert(!seen.has(key), `${at}: duplicate route sequence ${key}`);
    seen.add(key);
  }
}

async function main() {
  console.log(`edge smoke: ${BASE}`);

  // 1. health
  const health = await probe("/api/health");
  check("health: ok/stop/pattern/route counts", () => {
    assert(health.ok === true, "ok is not true");
    assert(health.stops > 5000, `stops ${health.stops}`);
    assert(health.patterns > 2000, `patterns ${health.patterns}`);
    assert(health.routes > 2000, `routes ${health.routes}`);
  });

  // 2. status
  await sleep(250);
  const status = await probe("/api/status");
  check("status: routes/edges/live shape", () => {
    assert(status.ok === true, "ok is not true");
    assert(status.routes > 2000, `routes ${status.routes}`);
    assert(status.edges > 0, `edges ${status.edges} (A2/traversal model)`);
    assert(typeof status.live?.vehicles === "number" && status.live.vehicles >= 0,
      "live.vehicles missing or negative");   // A6: now real, not hardcoded zero
  });

  // 3. pulse
  await sleep(250);
  const pulse = await probe("/api/pulse");
  check("pulse: full 24-hour city curve", () => {
    assert(pulse.city_hour && typeof pulse.city_hour === "object", "no city_hour");
    const hours = Object.keys(pulse.city_hour);
    assert(hours.length === 24, `${hours.length} hours, expected 24`);
    for (const h of hours) {
      assert(isNum(pulse.city_hour[h]) && pulse.city_hour[h] > 0,
        `city_hour[${h}] = ${pulse.city_hour[h]}`);
    }
  });

  // 4. stop search (soft on hits: fuzzy match is A7, still in flight)
  await sleep(250);
  const search = await probe(`/api/stops/search?${q({ q: "Kashmere" })}`);
  check("stops/search: bounded result shape", () => {
    assert(Array.isArray(search), "response is not an array");
    assert(search.length <= 10, `${search.length} results > 10`);
    for (const s of search) {
      assert(s.stop_id && s.name, "stop without stop_id/name");
      assert(isNum(s.lat) && isNum(s.lng), `stop ${s.stop_id}: bad coords`);
    }
  });
  if (search.length === 0) warn('stops/search?q=Kashmere returned no hits (A7 fuzzy match)');

  // 5. nearby stops
  await sleep(250);
  const nearby = await probe(`/api/stops/nearby?${q({
    lat: 28.6315, lng: 77.2197, radius: 1200 })}`);
  check("stops/nearby: sorted by distance within radius", () => {
    assert(Array.isArray(nearby), "response is not an array");
    assert(nearby.length <= 6, `${nearby.length} stops > 6`);
    assert(nearby.length > 0, "no stops near Kashmere Gate");
    let prev = -1;
    for (const s of nearby) {
      assert(isNum(s.distance_m) && s.distance_m >= prev,
        `distance_m not ascending at ${s.stop_id}`);
      prev = s.distance_m;
      assert(s.distance_m <= 1200, `stop ${s.stop_id} outside radius`);
      assert(isNum(s.walk_min) && s.walk_min >= 0, `stop ${s.stop_id}: walk_min`);
    }
  });

  // 6-8. journeys: base, clamp high, clamp negative
  await sleep(250);
  const journeyPath = (extra = {}) => `/api/journeys?${q({
    from: FROM, to: TO, depart: DEPART, ...extra })}`;
  const journeys = await probe(journeyPath({ max: 6 }));
  check("journeys: options sorted, bands ordered, fields frozen", () => {
    assertOptions(journeys, "journeys");
    assert(journeys.options.length > 0, "no options for a known busy pair");
  });
  snapshot.from = FROM_LABEL;
  snapshot.to = TO_LABEL;
  snapshot.depart = DEPART;
  snapshot.options = (journeys.options || []).map((o) => ({
    route_code: o.route_code, transfers: o.transfers,
    total_min: o.total_min, eta_p10_min: o.eta_p10_min,
    eta_p50_min: o.eta_p50_min, eta_p90_min: o.eta_p90_min,
    usual_p50_min: o.usual_p50_min ?? null,
    usual_hour_ist: o.usual_hour_ist ?? null,
  }));

  await sleep(250);
  const maxHigh = await probe(journeyPath({ max: 99 }));
  check("journeys: max=99 clamps to 6 (D3)", () => {
    assertOptions(maxHigh, "max=99");
    assert(maxHigh.options.length <= 6,
      `${maxHigh.options.length} options for max=99`);
  });

  await sleep(250);
  const maxNeg = await probe(journeyPath({ max: -1 }));
  check("journeys: max=-1 clamps to 1 (D3)", () => {
    assertOptions(maxNeg, "max=-1", 1);
    assert(maxNeg.options.length <= 1,
      `${maxNeg.options.length} options for max=-1`);
  });

  // 9. asOf note comes back prepended alongside options, not instead
  await sleep(250);
  const asOf = await probe(journeyPath({
    max: 3, asOf: "2026-11-03T09:00:00+05:30" }));
  check("journeys: asOf note prepended alongside options (D7)", () => {
    assertOptions(asOf, "asOf", 3);
    assert(typeof asOf.note === "string" && asOf.note.length > 0,
      "no note for asOf query");
    assert(asOf.note.toLowerCase().includes("asof"),
      `note does not mention asOf: ${asOf.note}`);
    assert(asOf.options.length > 0,
      "asOf note replaced the options instead of annotating them");
  });

  assert(requests.length <= MAX_REQUESTS, "request budget exceeded");
  for (const r of requests) {
    if (r.ms > 8000) warn(`slow probe: ${r.path} took ${r.ms} ms`);
  }

  // 10. place-name resolution — soft: the resolved stop depends on the
  //    shipped stop table, so report rather than fail (A7 is in flight).
  const named = await probe(`/api/journeys?${q({
    from: "Kashmere Gate", to: "Nehru Place", depart: DEPART, max: 6 })}`);
  if (Array.isArray(named.options) && named.options.length > 0) {
    check("journeys: place-name resolution is invariant-clean", () => {
      assertOptions(named, "named");
    });
  } else {
    warn(`place-name path returned no options for Kashmere Gate -> Nehru Place`
      + ` (${named.note || "no note"})`);
  }
}

const started = new Date().toISOString();
let fatal = null;
try {
  await main();
} catch (e) {
  fatal = e && e.message || String(e);
  console.log(`  FAIL fatal: ${fatal}`);
}

const failed = checks.filter((c) => !c.ok);
const golden = {
  note: "Informational snapshot from workers/smoke.mjs; do not diff in CI.",
  checked_at: started,
  base: BASE,
  requests,
  summary: {
    requests: requests.length,
    checks: checks.length,
    failed: failed.length + (fatal ? 1 : 0),
    warnings: warnings.length,
  },
  checks,
  warnings,
  fatal,
  snapshot,
};
try {
  if (requests.length === 0) {
    console.log("  skip golden: no response was received");
  } else {
    mkdirSync(dirname(GOLDEN), { recursive: true });
    writeFileSync(GOLDEN, JSON.stringify(golden, null, 2) + "\n", "utf8");
    console.log(`  wrote ${GOLDEN}`);
  }
} catch (e) {
  console.log(`  warn could not write golden: ${e.message}`);
}

console.log(fatal || failed.length
  ? `edge smoke FAILED (${failed.length + (fatal ? 1 : 0)} failing, `
    + `${requests.length} requests)`
  : `edge smoke PASSED (${checks.length} checks, ${requests.length} requests)`);
process.exit(fatal || failed.length ? 1 : 0);
