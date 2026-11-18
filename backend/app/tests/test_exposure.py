"""临时暴露人口区间测试：六类验收情形 + 区间数学 + 版本/快照纪律。

六类验收：
  ① 节假日景区流入（G1：移动校准 + 景区签到，overlap_known 收窄）
  ② 寄宿学校放假（G2：subset_of 包含关系）
  ③ 跨来源重复观测（G3：无规则，下界取 max 绝不相加）
  ④ 移动信号覆盖下降（G8：开放上界 NULL，只扩大未知）
  ⑤ 隐私小样本抑制（G6：抑制不记零，仅容量约束上界）
  ⑥ 转移登记晚到（G8：新版本减量 80→130，旧版本保留当时分母）
"""
from datetime import date

import pytest

from app.seed import (
    EX_CUTOFF_1,
    EX_CUTOFF_2,
    PRIVACY_THRESHOLD,
    T0,
    W1_END,
    W1_START,
)
from app.services.compute import manager
from app.services.exposure import (
    Interval,
    build_exposure_estimate,
    get_estimate,
    _union_with_rule,
)

AS_OF = date(2026, 6, 1)


def _estimate(db, cutoff=EX_CUTOFF_1, task_id=None):
    return build_exposure_estimate(
        db,
        window_start=W1_START,
        window_end=W1_END,
        evidence_cutoff=cutoff,
        privacy_threshold=PRIVACY_THRESHOLD,
        baseline_as_of=AS_OF,
        created_by="pytest",
        reachable_task_id=task_id,
    )


def _period(est, grid_id):
    return next(p for p in est["periods"] if p["grid_id"] == grid_id)


def _evidence(est, grid_id, source_kind):
    return [e for e in est["evidence"] if e["grid_id"] == grid_id
            and e["source_kind"] == source_kind]


# ---------------------------------------------------------------- 区间数学 --
def test_union_no_rule_uses_frechet_bounds_not_sum():
    # 重叠不可识别：下界取 max（至少这么多人），上界取和（理论包络）
    x, y = Interval(100, 300), Interval(80, 200)
    u = _union_with_rule(x, y, None)
    assert (u.lower, u.upper) == (100, 500)
    # 任一上界缺失 → 并集上界不可估计
    u_open = _union_with_rule(Interval(60, None), Interval(50, 100), None)
    assert u_open.lower == 60 and u_open.upper is None


def test_union_disjoint_allows_sum_subset_takes_envelope():
    rule_disjoint = {"relation": "disjoint"}
    u = _union_with_rule(Interval(10, 20), Interval(5, 8), rule_disjoint)
    assert (u.lower, u.upper) == (15, 28)
    rule_subset = {"relation": "subset_of"}
    u2 = _union_with_rule(Interval(10, 20), Interval(5, 30), rule_subset)
    assert (u2.lower, u2.upper) == (10, 30)


def test_union_overlap_known_applies_inclusion_exclusion():
    # |A∪B|=|A|+|B|-|A∩B|，重叠 [20,30]
    rule = {"relation": "overlap_known", "min_overlap_persons": 20,
            "max_overlap_persons": 30}
    u = _union_with_rule(Interval(40, 60), Interval(50, 70), rule)
    assert u.lower == 40 + 50 - 30   # 重叠最大时并集最小
    assert u.upper == 60 + 70 - 20   # 重叠最小时并集最大


# ---------------------------------------------------------------- ① 景区流入 --
def test_holiday_scenic_inflow_with_calibration_and_overlap_narrowing(fresh_db):
    est = _estimate(fresh_db)
    g1 = _period(est, "G1")
    # OP1 采样 200 × 系数 2.0 = 400，±10% → [360,440]
    mobile = _evidence(est, "G1", "mobile")[0]
    assert mobile["calibrated"] is True
    assert (mobile["adopted_lower"], mobile["adopted_upper"]) == (360.0, 440.0)
    assert g1["sampling_margin"] == 40.0
    # 景区签到 350 下界、容量 1200 上界；overlap_known[300,400] 收窄并集
    # 下界 = 400+350-400 = 350 → max(.,440) = 360；上界 = 440+1200-300 = 1340
    assert (g1["increase_lower"], g1["increase_upper"]) == (360.0, 1340.0)
    # 反证：若无重叠规则，上界应为 440+1200=1640；规则确实收窄了 300
    assert g1["increase_upper"] < 1640.0
    assert g1["method_mode"] == "mixed"


# ---------------------------------------------------------------- ② 学校放假 --
def test_boarding_school_holiday_subset_relation(fresh_db):
    est = _estimate(fresh_db)
    g2 = _period(est, "G2")
    # 移动 [72,88] 与留校 [60,500] 为 subset_of（移动包含于点名）→ 并集取包络
    assert (g2["increase_lower"], g2["increase_upper"]) == (72.0, 500.0)
    kinds = {e["source_kind"] for e in est["evidence"] if e["grid_id"] == "G2"}
    assert {"mobile", "boarding_school"} <= kinds


# ---------------------------------------------------------------- ③ 重复观测 --
def test_cross_source_duplicate_not_summed_when_overlap_unidentifiable(fresh_db):
    est = _estimate(fresh_db)
    g3 = _period(est, "G3")
    # 景区 [100,300] 与学校 [40,100] 无重叠规则：
    # 下界必须是 max(100,40)=100，而不是 100+40=140
    assert g3["increase_lower"] == 100.0
    assert g3["increase_upper"] == 400.0  # 300+100 理论包络
    assert g3["method_mode"] == "conservative"
    # 结构性未知宽度 = 400-100 = 300（无抽样误差）
    assert g3["interval_unknown"] == 300.0


# ---------------------------------------------------------------- ④ 覆盖下降 --
def test_mobile_coverage_drop_produces_open_upper_not_fake_number(fresh_db):
    est = _estimate(fresh_db)
    g8 = _period(est, "G8")
    # OP2 覆盖率 0.30 且不稳定、未核验 → 采样 60 仅作下界，上界不可估计
    assert g8["increase_lower"] == 60.0
    assert g8["increase_upper"] is None
    assert g8["delta_upper"] is None
    assert g8["interval_unknown"] is None  # 上界不可数值化
    note = " ".join(e["note"] for e in _evidence(est, "G8", "mobile"))
    assert "conservative" in note


# ---------------------------------------------------------------- ⑤ 隐私抑制 --
def test_privacy_suppression_never_recorded_as_zero(fresh_db):
    est = _estimate(fresh_db)
    g6 = _period(est, "G6")
    # 移动信号与景区签到均被抑制：point_unknown 记录 2 条受抑制来源
    assert g6["point_unknown"] == 2.0
    suppressed = [e for e in est["evidence"]
                  if e["grid_id"] == "G6" and e["suppressed"]]
    assert len(suppressed) == 2
    assert all(e["observed_count"] is None for e in suppressed)
    # 没有任何确定增加下界；仅景区容量 200 约束上界
    assert g6["increase_lower"] == 0.0
    assert g6["increase_upper"] == 200.0
    # 抑制绝不被记成“0 人确定值”：上界仍保留容量空间
    assert g6["increase_upper"] > g6["increase_lower"]


# ---------------------------------------------------------------- ⑥ 晚到登记 --
def test_late_transfer_creates_new_version_and_preserves_old_denominator(fresh_db):
    v1 = _estimate(fresh_db, cutoff=EX_CUTOFF_1)
    g8_v1 = _period(v1, "G8")
    assert g8_v1["decrease_lower"] == 80.0  # 晚到的 50 人尚未到达
    assert v1["estimate"]["estimate_version"] == 1

    v2 = _estimate(fresh_db, cutoff=EX_CUTOFF_2)
    g8_v2 = _period(v2, "G8")
    assert v2["estimate"]["estimate_version"] == 2
    assert g8_v2["decrease_lower"] == 130.0  # 80 + 晚到 50
    assert g8_v2["delta_lower"] == 60.0 - 130.0

    # 旧版本物理保留，仍可按旧 estimate_id 取回当时分母
    old = get_estimate(fresh_db, v1["estimate"]["estimate_id"])
    old_g8 = _period(old, "G8")
    assert old_g8["decrease_lower"] == 80.0
    # 两个输入快照采用的转移行不同（v1 不含晚到行）
    assert len(v1["snapshot"]["included_transfer_ids"]) == 2
    assert len(v2["snapshot"]["included_transfer_ids"]) == 3


# ---------------------------------------------------------------- 基线叠加 --
def test_interval_overlay_on_reachable_baseline(fresh_db):
    # 先跑一个覆盖任务，取网格级基线可达人口
    task = manager.get_or_create(
        fresh_db, name="暴露叠加基线", population_as_of=AS_OF,
        departure_time=T0, max_trip_minutes=60, road_cutoff=EX_CUTOFF_1,
        viewer_id="pytest",
    )
    est = _estimate(fresh_db, task_id=task["task_id"])
    g4 = _period(est, "G4")
    # G4 净变化确定为 -120；叠加后可达需求 = 基线可达 - 120
    assert g4["reachable_baseline"] is not None
    assert g4["demand_lower"] == g4["reachable_baseline"] - 120.0
    assert g4["demand_upper"] == g4["reachable_baseline"] - 120.0
    # 开放上界网格（G8）的需求上界同样不可估计，且人数下界不允许为负
    g8 = _period(est, "G8")
    assert g8["demand_upper"] is None
    assert g8["demand_lower"] == 0.0  # 净变化下界 -20 被非负钳制（delta 本身仍为 -20）
    assert g8["delta_lower"] == -20.0


def test_grids_without_any_signal_keep_zero_interval_not_fabricated(fresh_db):
    est = _estimate(fresh_db)
    # G5/G7 没有任何临时来源：区间必须是确定的 [0,0]，且无虚构未知人数
    for gid in ("G5", "G7"):
        p = _period(est, gid)
        assert (p["increase_lower"], p["increase_upper"]) == (0.0, 0.0)
        assert p["point_unknown"] == 0.0 and p["interval_unknown"] == 0.0


def test_bounded_interval_identity_unknown_plus_margin(fresh_db):
    """有界网格：上界-下界 = 抽样误差半宽 + 结构性未知（恒等关系）。"""
    est = _estimate(fresh_db)
    for p in est["periods"]:
        if p["increase_upper"] is None:
            continue
        width = p["increase_upper"] - p["increase_lower"]
        assert width == pytest.approx(
            p["sampling_margin"] + (p["interval_unknown"] or 0.0), abs=1e-6
        )


def test_snapshot_fingerprint_stable_for_same_inputs(fresh_db):
    v1 = _estimate(fresh_db, cutoff=EX_CUTOFF_1)
    v2 = _estimate(fresh_db, cutoff=EX_CUTOFF_1)  # 同输入再算 → 新版本但同指纹
    assert v1["snapshot"]["source_row_fingerprint"] == \
        v2["snapshot"]["source_row_fingerprint"]
    g1_a, g1_b = _period(v1, "G1"), _period(v2, "G1")
    assert g1_a["increase_lower"] == g1_b["increase_lower"]
    assert g1_a["increase_upper"] == g1_b["increase_upper"]
