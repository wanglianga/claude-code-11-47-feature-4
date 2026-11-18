#!/bin/sh
# 容器入口：建表（幂等）；仅当库内尚无路网节点时写入样例，避免重启清空用户数据。
set -e

python - <<'PY'
from app.db import get_conn, init_db
init_db()
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM road_nodes")
        empty = cur.fetchone()["n"] == 0
if empty:
    from app.seed import reseed, seed_large
    with get_conn() as conn:
        reseed(conn)
        seed_large(conn)
    print("empty database detected -> sample data seeded")
else:
    print("database already contains data -> seed skipped")
PY

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
