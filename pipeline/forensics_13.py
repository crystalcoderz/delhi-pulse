"""Forensics on real day-13 pings: id formats, counts, quality stats."""
import duckdb

con = duckdb.connect()
src = "data/clean/pings_2026_10_13.parquet"
print("---- trip_id samples ----")
print(con.execute(f"""
    SELECT DISTINCT trip_id FROM read_parquet('{src}') LIMIT 8
""").df().to_string(index=False))
print("---- vehicle_id samples ----")
print(con.execute(f"""
    SELECT DISTINCT vehicle_id FROM read_parquet('{src}') LIMIT 8
""").df().to_string(index=False))
print("---- route_id samples + trip counts ----")
print(con.execute(f"""
    SELECT route_id, COUNT(DISTINCT trip_id) trips, COUNT(*) pings
    FROM read_parquet('{src}') GROUP BY 1 ORDER BY pings DESC LIMIT 12
""").df().to_string(index=False))
print("---- hourly distribution (rush check) ----")
print(con.execute(f"""
    SELECT hour_ist, COUNT(*) n, round(avg(speed),1) avg_speed_field
    FROM read_parquet('{src}') GROUP BY 1 ORDER BY 1
""").df().to_string(index=False))
print("---- % rows with route_id ----")
print(con.execute(f"""
    SELECT round(100.0*sum(CASE WHEN route_id IS NOT NULL THEN 1 ELSE 0 END)/COUNT(*),1) pct,
           round(100.0*sum(CASE WHEN speed IS NOT NULL AND speed>0 THEN 1 ELSE 0 END)/COUNT(*),1) pct_speed
    FROM read_parquet('{src}')
""").df().to_string(index=False))
print("---- pings per trip distribution ----")
print(con.execute(f"""
    WITH t AS (SELECT trip_id, COUNT(*) n FROM read_parquet('{src}') GROUP BY 1)
    SELECT min(n) mn, approx_quantile(n, 0.5) med, max(n) mx, count(*) trips FROM t
""").df().to_string(index=False))
print("---- trips per day duration (first->last ping) ----")
print(con.execute(f"""
    WITH t AS (SELECT trip_id, min(ts_utc) a, max(ts_utc) b FROM read_parquet('{src}') GROUP BY 1)
    SELECT round(avg((b-a))/60.0,1) avg_min, round(max((b-a))/60.0,1) max_min FROM t
""").df().to_string(index=False))
