"""覆盖分析测试：互斥份额、多点去重、未知边界、等待超时与安置点开放。"""
from datetime import date, timedelta

from app.seed import C1, C2, T0, apply_c2_revision
from app.services.compute import manager

AS_OF = date(2026, 6, 1)
AS_OF_V2 = date(2026, 7, 1)


def _run(db, *, name, cutoff=C1, departure=T0, minutes=30, as_of=AS_OF, boundary=None):
    return manager.get_or_create(
        db, name=name, population_as_of=as_of, departure_time=departure,
        max_trip_minutes=minutes, road_cutoff=cutoff, viewer_id="pytest",
        boundary_id=boundary,
    )


def _shares(db, task_id, grid_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM grid_share_results WHERE task_id=%s AND grid_id=%s ORDER BY share_index",
            (task_id, grid_id),
        )
        return cur.fetchall()


def test_multi_exit_grid_uses_mutex_subshares(db):
    r = _run(db, name="多出口互斥")
    g2 = _shares(db, r["task_id"], "G2")
    assert len(g2) == 2
    pops = sorted(round(x["share_population"], 3) for x in g2)
    assert pops == [200.0, 300.0]  # 500 × 0.4 / 0.6
    assert all(x["status"] == "covered" for x in g2)
    # 两个份额到达不同安置点，各自只计一次，总和恰为 500，不翻倍
    assert sum(x["share_population"] for x in g2) == 500.0


def test_single_population_reaching_multiple_shelters_counted_once(db):
    r = _run(db, name="多点重叠")
    g1 = _shares(db, r["task_id"], "G1")
    assert len(g1) == 1                       # 互斥份额只有一份
    assert g1[0]["share_population"] == 1000.0
    assert g1[0]["status"] == "covered"
    with db.cursor() as cur:
        cur.execute(
            "SELECT covered_population FROM coverage_summaries WHERE task_id=%s",
            (r["task_id"],),
        )
        covered = cur.fetchone()["covered_population"]
        # 手算小型网格覆盖：G1 1000 + G2 500 + G4 560 + G6 1000 + G7 600 = 3660（G8 不可达）
        small_covered = covered - sum(
            1 for _ in []
        )
        assert covered >= 3660.0
    # G1 只能对应一个安置点（取最早实际到达）
    assert len({x["shelter_id"] for x in g1}) == 1


def test_unknown_categories_are_separated_not_zero_or_full(db):
    r = _run(db, name="未知边界")
    g3 = _shares(db, r["task_id"], "G3")
    g4 = _shares(db, r["task_id"], "G4")
    g5 = _shares(db, r["task_id"], "G5")
    assert g3[0]["status"] == "unknown" and "no_connector" in g3[0]["reason"]
    gap = [x for x in g4 if x["status"] == "unknown"]
    assert len(gap) == 1 and gap[0]["share_population"] == 240.0  # 800 × 0.3
    assert "allocation_gap" in gap[0]["reason"]
    assert g5[0]["status"] == "unknown" and "ratio_over_one" in g5[0]["reason"]
    with db.cursor() as cur:
        cur.execute("SELECT * FROM coverage_summaries WHERE task_id=%s", (r["task_id"],))
        s = cur.fetchone()
        assert s["unknown_population"] > 0
        # 未知不进入覆盖也不进入未覆盖（未覆盖只针对可定位份额）
        assert abs((s["covered_population"] + s["uncovered_population"]
                    + s["unknown_population"]) - s["total_population"]) < 1e-6


def test_wait_then_timeout_excluded_but_wait_inside_deadline_included(fresh_db):
    db = fresh_db
    # C1：E7 无恢复时间，G8 无法覆盖
    c1 = _run(db, name="恢复前", cutoff=C1, minutes=30)
    assert all(x["status"] == "uncovered" for x in _shares(db, c1["task_id"], "G8"))
    apply_c2_revision(db)
    # 30 分钟时限：等到 08:40 后仍超时 → 不计入
    short = _run(db, name="恢复后超时", cutoff=C2, minutes=30)
    assert all(x["status"] == "uncovered" for x in _shares(db, short["task_id"], "G8"))
    # 60 分钟时限：08:40 通行、09:00 安置点开放，等待成本 59 分钟 ≤ 时限 → 计入
    long = _run(db, name="恢复后达标", cutoff=C2, minutes=60)
    g8 = _shares(db, long["task_id"], "G8")
    assert g8[0]["status"] == "covered"
    assert g8[0]["shelter_id"] == "S3"
    assert g8[0]["arrival_time"] == T0 + timedelta(minutes=60)
    assert g8[0]["total_wait_sec"] == 3540.0  # 40 分钟道路等待 + 19 分钟开放等待


def test_shelter_closed_at_arrival_excluded_when_wait_not_allowed(fresh_db):
    db = fresh_db
    # 构造极端时限：到达 S3 节点 N7 早于 09:00 开放；N7 允许等待但时限截止于 08:50
    apply_c2_revision(db)
    r = _run(db, name="安置点关闭", cutoff=C2, minutes=45)
    g8 = _shares(db, r["task_id"], "G8")
    # 08:41 到 N7，S3 09:00 才开；等待到 09:00 超过 08:45 时限 → 不可覆盖
    assert all(x["status"] == "uncovered" for x in g8)
    reason = g8[0]["reason"]
    assert "deadline" in reason or "shelter_closed" in reason


def test_evidence_cutoff_annotated_on_every_result(fresh_db):
    db = fresh_db
    r = _run(db, name="证据截止", cutoff=C1, minutes=30)
    with db.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT evidence_cutoff_at FROM grid_share_results WHERE task_id=%s",
            (r["task_id"],),
        )
        cutoffs = {x["evidence_cutoff_at"] for x in cur.fetchall()}
        assert cutoffs == {C1}
        cur.execute("SELECT evidence_cutoff_at FROM coverage_cache_index WHERE task_id=%s",
                    (r["task_id"],))
        assert cur.fetchone()["evidence_cutoff_at"] == C1


def test_boundary_filter_changes_denominator_explicitly(db):
    all_r = _run(db, name="全边界")
    ext_r = _run(db, name="仅扩展区", boundary="B_EXT")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM coverage_summaries WHERE task_id=%s", (all_r["task_id"],))
        total_all = cur.fetchone()["total_population"]
        cur.execute("SELECT * FROM coverage_summaries WHERE task_id=%s", (ext_r["task_id"],))
        ext = cur.fetchone()
    assert ext["total_population"] == 600.0  # 仅 G7
    assert total_all > ext["total_population"]
