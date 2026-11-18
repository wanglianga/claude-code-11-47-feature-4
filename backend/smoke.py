"""开发期冒烟脚本：核对小型样例的手工复算预期（非正式测试）。"""
from datetime import date, timedelta, timezone

from app.db import get_conn, init_db
from app.seed import (
    T0, C1, C2, apply_c2_revision,
    W1_START, W1_END, EX_CUTOFF_1, EX_CUTOFF_2,
)
from app.services.compute import manager
from app.services.comparison import build_comparison
from app.services.exposure import build_exposure_estimate
from app.services.snapshot import get_or_create_network_version, load_snapshot
from app.engine import solve

UTC = timezone.utc


def main():
    init_db()
    conn = get_conn()
    as_of = date(2026, 6, 1)

    # ---- 情景 A：C1 截止点、08:00 出发、30 分钟时限 ----
    a = manager.get_or_create(
        conn, name="冒烟A", population_as_of=as_of, departure_time=T0,
        max_trip_minutes=30, road_cutoff=C1, viewer_id="smoke",
    )
    ta = a["task_id"]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM coverage_summaries WHERE task_id=%s", (ta,))
        print("A summary:", cur.fetchone())
        cur.execute(
            "SELECT grid_id, status, share_population, reason, shelter_id, arrival_time "
            "FROM grid_share_results WHERE task_id=%s ORDER BY grid_id, share_index", (ta,))
        for r in cur.fetchall():
            print(" ", r)

    # ---- 情景 B：同口径，C2（重开+新封路），60 分钟时限，验证 G8 恢复 ----
    apply_c2_revision(conn)
    b = manager.get_or_create(
        conn, name="冒烟B", population_as_of=as_of, departure_time=T0,
        max_trip_minutes=60, road_cutoff=C2, viewer_id="smoke",
    )
    tb = b["task_id"]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM coverage_summaries WHERE task_id=%s", (tb,))
        print("B summary:", cur.fetchone())
        cur.execute(
            "SELECT grid_id, status, share_population, reason, shelter_id, arrival_time, total_wait_sec "
            "FROM grid_share_results WHERE task_id=%s AND grid_id IN ('G8','G2','G1') ORDER BY grid_id, share_index",
            (tb,))
        for r in cur.fetchall():
            print(" ", r)

    # ---- 等待后超时：C2 下 30 分钟时限，G8 应仍不可覆盖 ----
    c = manager.get_or_create(
        conn, name="冒烟C-超时", population_as_of=as_of, departure_time=T0,
        max_trip_minutes=30, road_cutoff=C2, viewer_id="smoke",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT grid_id, status, reason FROM grid_share_results "
            "WHERE task_id=%s AND grid_id='G8'", (c["task_id"],))
        print("G8 30min@C2:", cur.fetchall())

    # ---- 引擎级检查：限速整段生效 ----
    version = get_or_create_network_version(conn, C2)
    snap = load_snapshot(conn, version["version_id"])
    labels = solve(snap, "N1", T0 + timedelta(minutes=10), T0 + timedelta(minutes=40))
    print("N2 arrival (expect 08:13, 限速 20km/h 3 分钟):", labels["N2"].arrival_time)
    n4 = labels.get("N4")
    print("N4 label witness:", n4.edge_path if n4 else None,
          "arrival:", n4.arrival_time if n4 else None)

    # ---- 对比（A vs 不同时限仅看接口通畅，正式对比用同口径） ----
    comp = build_comparison(conn, ta, tb)
    print("comparison warnings:", comp["warnings"])
    print("common denominator:", comp["common_denominator"])
    print("newly covered:", [g["grid_id"] for g in comp["attribution"]["newly_covered_grids"]])

    # ---- 缓存视图 ----
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM coverage_cache_index")
        print("cache rows:", cur.fetchone()["n"])
        cur.execute("SELECT version_id, cutoff_at, edge_fingerprint, event_fingerprint FROM road_network_versions ORDER BY version_id")
        for r in cur.fetchall():
            print(" version:", r)

    # ---- 临时暴露人口区间（六类验收情形）----
    print("\n=== 临时暴露人口区间 ===")
    v1 = build_exposure_estimate(
        conn, window_start=W1_START, window_end=W1_END, evidence_cutoff=EX_CUTOFF_1,
        privacy_threshold=10, baseline_as_of=as_of, reachable_task_id=tb,
    )
    print("E1 totals:", v1["totals"])
    for gid in ("G1", "G2", "G3", "G4", "G6", "G8"):
        p = next(x for x in v1["periods"] if x["grid_id"] == gid)
        print(" ", gid, "increase=[%s,%s] decrease=%s delta=[%s,%s] pointUnknown=%s mode=%s"
              % (p["increase_lower"], p["increase_upper"], p["decrease_lower"],
                 p["delta_lower"], p["delta_upper"], p["point_unknown"], p["method_mode"]))
    # 晚到转移登记 → 新版本，旧版本保留
    v2 = build_exposure_estimate(
        conn, window_start=W1_START, window_end=W1_END, evidence_cutoff=EX_CUTOFF_2,
        privacy_threshold=10, baseline_as_of=as_of, reachable_task_id=tb,
    )
    g8_v1 = next(x for x in v1["periods"] if x["grid_id"] == "G8")
    g8_v2 = next(x for x in v2["periods"] if x["grid_id"] == "G8")
    print("late transfer: G8 decrease v1=%s -> v2=%s; old version kept=%s"
          % (g8_v1["decrease_lower"], g8_v2["decrease_lower"],
             v1["estimate"]["estimate_version"] == 1))
    print("G8 overlay demand (non-negative lower):",
          g8_v2["demand_lower"], g8_v2["demand_upper"])
    conn.close()


if __name__ == "__main__":
    main()
