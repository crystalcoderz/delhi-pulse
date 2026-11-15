"""Quick look at the trained model tables."""
import duckdb

c = duckdb.connect()
for tbl in ("route_hour", "route_speed", "city_hour"):
    print(f"---- {tbl} ----")
    print(c.execute(f"""
        SELECT * FROM read_parquet('model/{tbl}.parquet')
        ORDER BY 1, 2 LIMIT 24
    """).df().to_string(index=False))

print("---- edge_hour (all cells) ----")
print(c.execute("SELECT * FROM read_parquet('model/edge_hour.parquet')").df().to_string(index=False))

print("---- traversal duration sanity ----")
print(c.execute("""
    SELECT round(min(dur_s),1) mn, round(median(dur_s),1) med, round(max(dur_s),1) mx,
           round(median(dist_m),1) dmed, count(*) n
    FROM read_parquet('data/clean/traversals_2026_10_15.parquet')
""").df().to_string(index=False))
