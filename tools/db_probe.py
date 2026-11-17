import sqlite3

PATHS = [
    (r"E:\情感开发\_审核\companion-e2e-v2.db", "副本"),
    (r"E:\情感开发\companion\data\companion.db", "线上"),
]
TABLES = ("facts", "neurons", "synapses", "activations", "lessons", "strategies")

for path, label in PATHS:
    conn = sqlite3.connect(path)
    names = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
    print(f"[{label}] {path}")
    print("  新表：", [t for t in ("neurons", "synapses", "activations", "strategies") if t in names] or "无")
    for tb in TABLES:
        if tb in names:
            print(f"  {tb} = {conn.execute('select count(*) from ' + tb).fetchone()[0]}")
    if "facts" in names:
        cols = [r[1] for r in conn.execute("pragma table_info(facts)")]
        print("  facts 新列：", [c for c in ("kind", "importance", "confidence", "activation") if c in cols] or "无")
        if "kind" in cols:
            print("  类型分布：", conn.execute("select kind, count(*) from facts group by kind").fetchall())
    conn.close()
    print()
