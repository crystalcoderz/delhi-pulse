import json

m = json.load(open('workers/public/model.json'))
for sid in ['267', '4109', '4730', '2559', '4971']:
    s = m['stops'].get(sid)
    mem = len(m['membership'].get(sid, []))
    if s:
        print(f'stop {sid}: ({s[0]:.6f}, {s[1]:.6f}) "{s[2]}" membership={mem}')
    else:
        print(f'stop {sid}: NOT IN MODEL')

print()
# Find overlapping patterns between stops
d_ids = [k for k, v in m['stops'].items() if 'nehru place' in v[2].lower()]
print(f'Nehru Place stop_ids ({len(d_ids)}): {d_ids[:10]}')

for origin_id in ['267', '4109', '4730']:
    o_mem = m['membership'].get(origin_id, [])
    o_pats = {x[0] for x in o_mem}
    print(f'\norigin stop {origin_id} ({m["stops"][origin_id][2]}): {len(o_pats)} patterns')
    for d_id in d_ids:
        d_mem = m['membership'].get(d_id, [])
        d_pats = {x[0] for x in d_mem}
        overlap = o_pats & d_pats
        if overlap:
            pats = [m['patterns'][i] for i in overlap]
            for p in pats:
                print(f'  -> stop {d_id}: route {p["r"]}')
