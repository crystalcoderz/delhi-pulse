# Technical note (draft — final version ships with the submission)

## 1. Linking GPS points to routes and roads

The October feed already assigns each ping a Transport Stack `trip_id`,
which joins to static GTFS `trips.txt -> shape_id`. We therefore do
*targeted* map matching rather than generic: each ping sequence is
projected onto its trip's known shape polyline (nearest-segment,
numpy-vectorized; equirectangular metric local to the shape centroid).
Pings farther than 75 m from their shape are discarded as mismatches.
Progress along the polyline is made monotone (running max) so GPS
back-jitter cannot imply travel backwards.

## 2. Travel time on each stretch of road

Stops of the trip are projected onto the same polyline, giving each stop
an arc-length. The ping progression then interpolates the crossing time
of every stop => "passage events". Consecutive passages give edge
traversal samples `(route, stop_i, stop_j, duration, entry_time)`.

v0 model: per `(route, from_stop, to_stop, hour-of-day)` median seconds,
with fallback chain edge -> route x hour speed -> route speed ->
city hour speed. Calibration (p10/p90) arrives with the LightGBM
quantile pass (v1). Dwell is included in traversal samples and reported
separately from stop-linger detection (v0.5).

## 3. Combining live and historical

One blend layer, two sources: a 15-minute window of recent pings — live
GTFS-RT (when the private feed key is active) or a replay window from
the October archive — blended with the historical median:
`est = a*recent + (1-a)*hist`, `a` growing with live sample count.
Sparse-data policy: no recent observations -> hierarchical fallback +
low-confidence flag surfaced in the API response.

## 4. Known assumptions / caveats

- Leg distances are GTFS shape arc lengths once shapes verified;
  straight-line haversine in early v0.
- Wait time at boarding is a flat 5 min until the headway model
  (from observed stop passages) lands.
- The October archive covers DTC buses only; the live feed includes
  cluster buses.
