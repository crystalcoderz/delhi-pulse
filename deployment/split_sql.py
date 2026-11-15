"""Split docs/d1_export.sql into parts D1 accepts (exec size limit)."""
from pathlib import Path

src = Path(__file__).resolve().parent.parent / "docs" / "d1_export.sql"
text = src.read_text(encoding="utf-8")
statements, buf = [], []
for line in text.splitlines():
    buf.append(line)
    if line.rstrip().endswith(";"):
        statements.append("\n".join(buf))
        buf = []
if buf:
    statements.append("\n".join(buf))

# bucket statements by table name into parts <= ~4 MB
parts, current, size = [], [], 0
for s in statements:
    if size + len(s) > 4_000_000 and current:
        parts.append("\n".join(current))
        current, size = [], 0
    current.append(s)
    size += len(s)
if current:
    parts.append("\n".join(current))

out = src.parent
for i, p in enumerate(parts, 1):
    f = out / f"d1_part{i}.sql"
    f.write_text(p, encoding="utf-8")
    print(f"{f.name}: {f.stat().st_size/1e6:.2f} MB, {p.count(';')} stmts")
