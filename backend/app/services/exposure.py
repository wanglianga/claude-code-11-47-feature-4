"""临时暴露人口区间计算（网格 × 时段）。

模块定位（与冻结基线的衔接）：
  人口基线(pop_grids) → 临时信号接入(三张来源表) → 覆盖率/重叠校准(本服务+规则表)
  → 区间计算(exposure_grid_periods) → 情景发布(引用 estimate_id/version)。

两条估计路径，均必须产出真实人数区间，不得把未观测量记为零：

1. 校准路径（移动信号）：仅当运营商覆盖率稳定且经过现场核验、且存在同口径分层
   校准系数时，才按系数把采样人数扩展为在场人数，并给出抽样误差区间；代表性或
   覆盖率稳定性不满足时，退回保守处理（只承认已核实采样人数为下界，上界不可估）。

2. 保守路径（场所/转移）：已核实人数作下界；容量（场所）约束上界；转移登记为
   已核实确定值。没有上限约束的部分上界留空（NULL），折算为“不可估计范围”，
   而不是比例外推。

重叠纪律（Fréchet 界）：
  - 互斥/包含/已知重叠人数等“可证明”关系才收窄并集区间；
  - 跨来源重叠无法识别时，下界取 max（至少这么多人），上界取各上界之和（理论
    包络），绝不把和当作确定点估计；该并集宽度整体计入结构性未知；
  - 隐私抑制、覆盖率下降只扩大未知范围，不产生任何确定数。

版本纪律：隐私阈值/覆盖率/来源数据修订只生成新 estimate_version；旧版本结果行
不原地改写，旧预案保留当时分母。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Optional

from psycopg.types.json import Json

# 来源类别常量
MOBILE = "mobile"
TRANSFER = "transfer"

_EPS = 1e-9


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 3)


class Interval:
    """人数区间 [lower, upper]；upper=None 表示上界不可估计（开放上界）。"""

    __slots__ = ("lower", "upper", "sampling_margin")

    def __init__(self, lower: float, upper: Optional[float], sampling_margin: float = 0.0):
        self.lower = float(lower)
        self.upper = None if upper is None else float(upper)
        self.sampling_margin = float(sampling_margin)

    @staticmethod
    def exact(n: float) -> "Interval":
        return Interval(n, n)

    def __repr__(self) -> str:
        return f"[{_r(self.lower)}, {'∞' if self.upper is None else _r(self.upper)}]"


# ----------------------------------------------------------------------------
# 输入快照：按证据截止点取数，记录采用的来源行，保证旧版本可精确复现
# ----------------------------------------------------------------------------
def _load_inputs(conn, *, window_start: datetime, window_end: datetime,
                 cutoff: datetime, boundary_id: Optional[str]) -> dict[str, list]:
    """时间窗对齐：选取与分析窗口相交的来源行（received_at<=截止点）。

    完全相等的窗口为“精确对齐”，可采用紧界；部分相交窗口只作宽松约束，
    不做时间比例外推（见各 _*_interval 的 partial 处理）。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.*, g.boundary_id
            FROM mobile_signal_samples s
            JOIN pop_grids g ON g.grid_id = s.grid_id
            WHERE s.window_start < %s AND s.window_end > %s
              AND s.received_at <= %s
              AND (%s::text IS NULL OR g.boundary_id = %s::text)
            ORDER BY s.sample_id
            """,
            (window_end, window_start, cutoff, boundary_id, boundary_id),
        )
        mobile = cur.fetchall()

        cur.execute(
            """
            SELECT v.*, g.boundary_id
            FROM venue_presence_records v
            JOIN pop_grids g ON g.grid_id = v.grid_id
            WHERE v.window_start < %s AND v.window_end > %s
              AND v.received_at <= %s
              AND (%s::text IS NULL OR g.boundary_id = %s::text)
            ORDER BY v.presence_id
            """,
            (window_end, window_start, cutoff, boundary_id, boundary_id),
        )
        venues = cur.fetchall()

        cur.execute(
            """
            SELECT c.* FROM venue_capacity c
            JOIN pop_grids g ON g.grid_id = c.grid_id
            WHERE c.effective_from <= %s
              AND (c.effective_to IS NULL OR c.effective_to > %s)
              AND (%s::text IS NULL OR g.boundary_id = %s::text)
            ORDER BY c.capacity_id
            """,
            (window_end, window_start, boundary_id, boundary_id),
        )
        capacities = cur.fetchall()

        cur.execute(
            """
            SELECT t.*, g.boundary_id
            FROM transfer_registrations t
            JOIN pop_grids g ON g.grid_id = t.grid_id
            WHERE t.window_start < %s AND t.window_end > %s
              AND t.received_at <= %s
              AND (%s::text IS NULL OR g.boundary_id = %s::text)
            ORDER BY t.transfer_id
            """,
            (window_end, window_start, cutoff, boundary_id, boundary_id),
        )
        transfers = cur.fetchall()

        cur.execute(
            """
            SELECT r.* FROM exposure_overlap_rules r
            JOIN pop_grids g ON g.grid_id = r.grid_id
            WHERE (%s::text IS NULL OR g.boundary_id = %s::text)
            ORDER BY r.rule_id
            """,
            (boundary_id, boundary_id),
        )
        rules = cur.fetchall()

        cur.execute(
            """
            SELECT c.* FROM mobile_signal_calibration c
            WHERE c.effective_from <= %s
              AND (c.effective_to IS NULL OR c.effective_to > %s)
            ORDER BY c.calibration_id
            """,
            (window_end, window_start),
        )
        calibrations = cur.fetchall()

    return {
        "mobile": mobile, "venues": venues, "capacities": capacities,
        "transfers": transfers, "rules": rules, "calibrations": calibrations,
    }


def _is_exact(row, ws, we) -> bool:
    return row["window_start"] == ws and row["window_end"] == we


def _create_snapshot(conn, *, window_start, window_end, cutoff, privacy_threshold,
                     boundary_id, inputs) -> str:
    snapshot_id = uuid.uuid4()
    mobile_ids = [r["sample_id"] for r in inputs["mobile"]]
    venue_ids = [r["presence_id"] for r in inputs["venues"]]
    transfer_ids = [r["transfer_id"] for r in inputs["transfers"]]
    rule_ids = [r["rule_id"] for r in inputs["rules"]]
    calibration_ids = [r["calibration_id"] for r in inputs["calibrations"]]
    fp_payload = json.dumps(
        [mobile_ids, venue_ids, transfer_ids, rule_ids, calibration_ids],
        sort_keys=True, default=str,
    )
    fingerprint = hashlib.sha256(fp_payload.encode("utf-8")).hexdigest()[:24]
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO exposure_input_snapshots
                   (snapshot_id, window_start, window_end, evidence_cutoff_at,
                    privacy_threshold, boundary_id, source_row_fingerprint,
                    included_mobile_ids, included_venue_ids, included_transfer_ids,
                    included_rule_ids, calibration_ids)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (snapshot_id, window_start, window_end, cutoff, privacy_threshold,
             boundary_id, fingerprint, Json(mobile_ids), Json(venue_ids),
             Json(transfer_ids), Json(rule_ids), Json(calibration_ids)),
        )
        conn.commit()
    return snapshot_id


# ----------------------------------------------------------------------------
# 单来源区间构造
# ----------------------------------------------------------------------------
def _mobile_interval(row, calibration, privacy_threshold, evidence, grid_id,
                     exact_window) -> Optional[Interval]:
    """移动信号 → 区间。不满足校准条件时退回保守下界；抑制/无观测不产确定数。"""
    sample_id = row["sample_id"]
    ref = f"sample#{sample_id}"

    # 部分时间窗对齐：不做时间比例外推；已知有人但无法给紧界 → 开放上界
    if not exact_window:
        evidence.append(_evidence(grid_id, MOBILE, ref, row["sample_count"], 0.0, None,
                                  bool(row["suppressed"]), row["operator_coverage"], False,
                                  "partial_window: 信号窗口与分析窗口仅部分相交，"
                                  "禁止按时间比例外推，不计紧下界，上界不可估计"))
        return Interval(0.0, None)

    # 隐私小样本抑制：观测不可见。不记零，只留未知证据。
    if row["suppressed"] or row["sample_count"] is None:
        evidence.append(_evidence(grid_id, MOBILE, ref, None, 0.0, None, True,
                                  row["operator_coverage"], False,
                                  "privacy_suppressed: 小样本被隐私抑制，观测不可见，仅扩大未知范围"))
        return None

    sample = float(row["sample_count"])
    # 低于隐私阈值的可见样本不允许外推为确定数（防御性处理）
    if sample < privacy_threshold:
        evidence.append(_evidence(grid_id, MOBILE, ref, sample, sample, None, False,
                                  row["operator_coverage"], False,
                                  f"below_privacy_threshold: 样本 {sample:g} < 隐私阈值 {privacy_threshold}，"
                                  "仅承认已观测人数为下界，禁止比例外推，上界不可估计"))
        return Interval(sample, None)

    eligible = (
        calibration is not None
        and bool(row["coverage_stable"])
        and bool(row["field_verified"])
        and bool(calibration["field_verified"])
        and abs(float(calibration["coverage_basis"]) - float(row["operator_coverage"])) <= _EPS
    )
    if not eligible:
        # 覆盖率不稳定/未核验/无同口径系数：保守路径，采样人数即已核实下界
        evidence.append(_evidence(grid_id, MOBILE, ref, sample, sample, None, False,
                                  row["operator_coverage"], False,
                                  "conservative: 覆盖率不稳定或未现场核验/缺同口径校准系数，"
                                  "采样人数仅作下界，不做比例外推，上界不可估计"))
        return Interval(sample, None)

    factor = float(calibration["calibration_factor"])
    rel_err = float(calibration["sampling_rel_error"])
    point = sample * factor
    margin = point * rel_err
    lower = max(point - margin, sample)  # 扩展值不应低于实际观测
    upper = point + margin
    evidence.append(_evidence(grid_id, MOBILE, ref, sample, lower, upper, False,
                              row["operator_coverage"], True,
                              f"calibrated: 采样 {sample:g} × 分层系数 {factor:g} = {_r(point)}，"
                              f"抽样相对误差 ±{rel_err:.0%}（半宽 {_r(margin)} 人）"))
    return Interval(lower, upper, sampling_margin=margin)


def _venue_interval(row, capacity, grid_id, evidence, exact_window) -> Optional[Interval]:
    """场所在场记录 → 保守区间：已核实人数下界，容量约束上界。"""
    ref = f"presence#{row['presence_id']}"
    kind = row["presence_kind"]

    if not exact_window:
        # 部分相交：签到人数不可作紧下界；容量仍可作上界约束，无容量则开放上界
        evidence.append(_evidence(grid_id, kind, ref, row["verified_count"], 0.0, capacity,
                                  bool(row["privacy_suppressed"]), None, False,
                                  "partial_window: 场所窗口与分析窗口仅部分相交，签到数不作紧下界；"
                                  + ("仅容量约束上界" if capacity is not None else "无容量约束，上界不可估计")))
        return Interval(0.0, capacity)

    if row["privacy_suppressed"] or row["verified_count"] is None:
        upper = capacity  # 抑制时容量仍可作上界约束；无容量则完全未知
        evidence.append(_evidence(grid_id, kind, ref, None, 0.0, upper, True, None, False,
                                  "privacy_suppressed: 场所签到/点名小样本抑制，"
                                  + ("仅容量约束上界" if capacity is not None else "无容量约束，上界不可估计")))
        return None if capacity is None else Interval(0.0, capacity)

    verified = float(row["verified_count"])
    upper = max(capacity, verified) if capacity is not None else None
    evidence.append(_evidence(grid_id, kind, ref, verified, verified, upper, False,
                              None, False,
                              "conservative: 已核实在场人数作下界"
                              + ("，场所容量约束上界" if capacity is not None else "，无容量上限约束，上界不可估计")))
    return Interval(verified, upper)


def _evidence(grid_id, source_kind, source_ref, observed, lower, upper, suppressed,
              coverage_value, calibrated, note) -> dict:
    return {
        "grid_id": grid_id, "source_kind": source_kind, "source_ref": source_ref,
        "observed_count": None if observed is None else float(observed),
        "adopted_lower": _r(lower),
        "adopted_upper": _r(upper),
        "suppressed": bool(suppressed),
        "coverage_value": None if coverage_value is None else float(coverage_value),
        "calibrated": bool(calibrated), "note": note,
    }


# ----------------------------------------------------------------------------
# 跨来源重叠处理（Fréchet 界）
# ----------------------------------------------------------------------------
def _rule_for(rules, grid_id: str, a: str, b: str) -> Optional[dict]:
    for r in rules:
        if r["grid_id"] != grid_id:
            continue
        if {r["source_a"], r["source_b"]} == {a, b}:
            return r
    return None


def _union_with_rule(x: Interval, y: Interval, rule: Optional[dict]) -> Interval:
    """两个来源“人数集合”的并集区间。

    无规则（重叠不可识别）：下界 max（至少这么多人），上界为各上界之和（理论
    包络）；任一侧上界缺失则并集上界不可估计。绝不把和作为确定点估计——并集
    宽度整体作为结构性未知呈现。
    """
    margin = x.sampling_margin + y.sampling_margin
    if rule is None:
        lower = max(x.lower, y.lower)
        upper = None if (x.upper is None or y.upper is None) else x.upper + y.upper
        return Interval(lower, upper, sampling_margin=margin)

    relation = rule["relation"]
    if relation == "disjoint":
        # 互斥可证明 → 允许相加；任一侧上界缺失则并集上界缺失
        lower = x.lower + y.lower
        upper = None if (x.upper is None or y.upper is None) else x.upper + y.upper
        return Interval(lower, upper, sampling_margin=margin)

    if relation == "subset_of":
        # 包含关系：并集 = 较大集合
        lower = max(x.lower, y.lower)
        upper = None if (x.upper is None or y.upper is None) else max(x.upper, y.upper)
        return Interval(lower, upper, sampling_margin=margin)

    # overlap_known：已知重叠人数区间 [o_min, o_max]
    o_min = float(rule["min_overlap_persons"])
    o_max = (min(x.lower, y.lower) if rule["max_overlap_persons"] is None
             else float(rule["max_overlap_persons"]))
    # |A∪B| = |A|+|B|-|A∩B|；下界在重叠最大时取得，上界在重叠最小时取得
    lower = max(x.lower + y.lower - o_max, max(x.lower, y.lower), 0.0)
    upper = None if (x.upper is None or y.upper is None) else x.upper + y.upper - o_min
    if upper is not None:
        upper = max(upper, lower)
    return Interval(lower, upper, sampling_margin=margin)


def _merge_components(components, rules, grid_id) -> Interval:
    """按重叠规则把多个增加来源合并为一个区间；无规则组合用并集包络。"""
    if not components:
        return Interval(0.0, 0.0)
    merged = components[0][1]
    merged_kinds = {components[0][0]}
    for kind, iv in components[1:]:
        rule = None
        for existing in merged_kinds:  # 与任一已并入来源存在可证明关系即采用
            rule = _rule_for(rules, grid_id, existing, kind)
            if rule is not None:
                break
        merged = _union_with_rule(merged, iv, rule)
        merged_kinds.add(kind)
    return merged


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def build_exposure_estimate(
    conn,
    *,
    window_start: datetime,
    window_end: datetime,
    evidence_cutoff: datetime,
    privacy_threshold: int = 10,
    boundary_id: Optional[str] = None,
    baseline_as_of=None,
    created_by: str = "analyst",
    reachable_task_id: Optional[str] = None,
) -> dict[str, Any]:
    """计算一个网格×时段临时暴露人口估计版本，落库并返回版本详情。"""
    inputs = _load_inputs(
        conn, window_start=window_start, window_end=window_end,
        cutoff=evidence_cutoff, boundary_id=boundary_id,
    )

    # 基线人口（冻结分母）
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.grid_id, g.boundary_id, p.population
            FROM pop_grids g
            JOIN LATERAL (
                SELECT * FROM pop_grid_revisions r
                WHERE r.grid_id = g.grid_id AND r.as_of_date <= %s
                ORDER BY r.as_of_date DESC, r.revision_no DESC LIMIT 1
            ) p ON TRUE
            WHERE %s::text IS NULL OR g.boundary_id = %s::text
            ORDER BY g.grid_id
            """,
            (baseline_as_of, boundary_id, boundary_id),
        )
        baselines = {r["grid_id"]: float(r["population"]) for r in cur.fetchall()}

    grids: dict[str, dict] = {
        gid: {"mobile": [], "venues": [], "transfers": []} for gid in baselines
    }
    for r in inputs["mobile"]:
        if r["grid_id"] in grids:
            grids[r["grid_id"]]["mobile"].append(r)
    for r in inputs["venues"]:
        if r["grid_id"] in grids:
            grids[r["grid_id"]]["venues"].append(r)
    for r in inputs["transfers"]:
        if r["grid_id"] in grids:
            grids[r["grid_id"]]["transfers"].append(r)

    capacity_by_venue = {c["venue_id"]: float(c["capacity_persons"])
                         for c in inputs["capacities"]}
    calib_index = {(c["operator_id"], c["grid_id"], c["stratum"]): c
                   for c in inputs["calibrations"]}

    snapshot_id = _create_snapshot(
        conn, window_start=window_start, window_end=window_end, cutoff=evidence_cutoff,
        privacy_threshold=privacy_threshold, boundary_id=boundary_id, inputs=inputs,
    )

    evidence_all: list[dict] = []
    period_rows: list[dict] = []
    calibrated_grids = 0
    conservative_grids = 0

    for grid_id in sorted(grids):
        bundle = grids[grid_id]
        evidence: list[dict] = []
        method_modes: set[str] = set()

        # ---- 增加侧：移动信号（运营商×分层分别成区间，再按重叠规则合并）----
        components: list[tuple[str, Interval]] = []
        for row in bundle["mobile"]:
            calib = calib_index.get((row["operator_id"], row["grid_id"], row["stratum"]))
            iv = _mobile_interval(
                row, calib, privacy_threshold, evidence, grid_id,
                _is_exact(row, window_start, window_end),
            )
            if iv is not None:
                components.append((MOBILE, iv))
                if iv.sampling_margin > 0:
                    method_modes.add("calibrated")
                else:
                    method_modes.add("conservative")

        # ---- 增加侧：场所（景区流入 / 寄宿学校在校）----
        for row in bundle["venues"]:
            iv = _venue_interval(
                row, capacity_by_venue.get(row["venue_id"]), grid_id, evidence,
                _is_exact(row, window_start, window_end),
            )
            if iv is not None:
                components.append((row["presence_kind"], iv))
                method_modes.add("conservative")

        increase_iv = _merge_components(components, inputs["rules"], grid_id)
        sampling_margin_total = sum(iv.sampling_margin for _, iv in components)

        # ---- 减少侧：转移登记（已核实登记人数即确定值，不向未登记人口外推）----
        decrease_lower = sum(float(t["registered_persons"]) for t in bundle["transfers"])
        for t in bundle["transfers"]:
            n = float(t["registered_persons"])
            tag = "（晚到登记标记）" if t["late_arrival"] else ""
            evidence.append(_evidence(
                grid_id, TRANSFER, f"transfer#{t['transfer_id']}", n, n, n, False,
                None, False,
                f"verified: 转移登记 {n:g} 人{tag}，已核实人数即确定区间，不向未登记人口外推",
            ))
        decrease_iv = Interval.exact(decrease_lower)

        # ---- 未知范围分解 ----
        point_unknown = float(sum(1 for e in evidence if e["suppressed"]))
        # 结构性未知：区间宽度中不属于抽样误差的部分（容量包络/重叠不可识别/覆盖缺口）
        if increase_iv.upper is None:
            interval_unknown = None  # 上界不可估计 → 未知范围无法数值化
        else:
            interval_unknown = max(
                (increase_iv.upper - increase_iv.lower) - sampling_margin_total, 0.0
            )

        # ---- 净变化区间 = 增加 ∪ 区间 − 减少确定值 ----
        delta_lower = increase_iv.lower - decrease_iv.upper
        delta_upper = (None if increase_iv.upper is None
                       else increase_iv.upper - decrease_iv.lower)

        baseline = baselines[grid_id]
        period_rows.append({
            "grid_id": grid_id,
            "baseline_population": _r(baseline),
            "increase_lower": _r(increase_iv.lower),
            "increase_upper": _r(increase_iv.upper),
            "decrease_lower": _r(decrease_iv.lower),
            "decrease_upper": _r(decrease_iv.upper),
            "delta_lower": _r(delta_lower),
            "delta_upper": _r(delta_upper),
            "point_unknown": _r(point_unknown),
            "interval_unknown": _r(interval_unknown),
            "sampling_margin": _r(sampling_margin_total),
            "method_mode": _mode_of(method_modes),
            "_evidence": evidence,
        })
        if method_modes == {"calibrated"}:
            calibrated_grids += 1
        elif method_modes:
            conservative_grids += 1
        evidence_all.extend(evidence)

    # ---- 版本号：同窗口+边界已存在则递增；修订只增版本，不改旧行 ----
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(MAX(estimate_version), 0) + 1 AS next_version
               FROM exposure_estimates
               WHERE window_start=%s AND window_end=%s
                 AND COALESCE(boundary_id, '') = COALESCE(%s, '')""",
            (window_start, window_end, boundary_id),
        )
        next_version = cur.fetchone()["next_version"]
        estimate_id = uuid.uuid4()
        cur.execute(
            """INSERT INTO exposure_estimates
                   (estimate_id, estimate_version, snapshot_id, window_start, window_end,
                    evidence_cutoff_at, privacy_threshold, boundary_id, baseline_pop_as_of,
                    method_note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (estimate_id, next_version, snapshot_id, window_start, window_end,
             evidence_cutoff, privacy_threshold, boundary_id, baseline_as_of,
             _method_note(calibrated_grids, conservative_grids), created_by),
        )

        reachable = _reachable_baseline(conn, reachable_task_id)
        cur.executemany(
            """INSERT INTO exposure_grid_periods
                   (estimate_id, grid_id, window_start, window_end, baseline_population,
                    increase_lower, increase_upper, decrease_lower, decrease_upper,
                    delta_lower, delta_upper, point_unknown, interval_unknown,
                    sampling_margin, method_mode, reachable_baseline, demand_lower,
                    demand_upper)
               VALUES (%(estimate_id)s,%(grid_id)s,%(ws)s,%(we)s,%(baseline_population)s,
                       %(increase_lower)s,%(increase_upper)s,%(decrease_lower)s,
                       %(decrease_upper)s,%(delta_lower)s,%(delta_upper)s,
                       %(point_unknown)s,%(interval_unknown)s,%(sampling_margin)s,
                       %(method_mode)s,%(reachable_baseline)s,%(demand_lower)s,
                       %(demand_upper)s)""",
            [
                {
                    "estimate_id": estimate_id, "ws": window_start, "we": window_end,
                    "reachable_baseline": reachable.get(r["grid_id"]),
                    # 可达需求 = 基线可达 + 净变化区间；人数下界非负（delta 本身允许为负）
                    "demand_lower": None if reachable.get(r["grid_id"]) is None
                        else _r(max(0.0, reachable[r["grid_id"]] + r["delta_lower"])),
                    "demand_upper": None if (reachable.get(r["grid_id"]) is None
                                             or r["delta_upper"] is None)
                        else _r(max(0.0, reachable[r["grid_id"]] + r["delta_upper"])),
                    **{k: v for k, v in r.items() if k != "_evidence"},
                }
                for r in period_rows
            ],
        )
        cur.executemany(
            """INSERT INTO exposure_evidence_items
                   (estimate_id, grid_id, source_kind, source_ref, observed_count,
                    adopted_lower, adopted_upper, suppressed, coverage_value,
                    calibrated, note)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [
                (estimate_id, e["grid_id"], e["source_kind"], e["source_ref"],
                 e["observed_count"], e["adopted_lower"], e["adopted_upper"],
                 e["suppressed"], e["coverage_value"], e["calibrated"], e["note"])
                for e in evidence_all
            ],
        )
        conn.commit()

    return get_estimate(conn, estimate_id)


def _reachable_baseline(conn, task_id: Optional[str]) -> dict[str, float]:
    """可选：取覆盖任务的网格级基线可达人口，用于区间叠加后的可达需求。"""
    if not task_id:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT grid_id, SUM(share_population) FILTER (WHERE status='covered') AS reachable
               FROM grid_share_results WHERE task_id=%s GROUP BY grid_id""",
            (task_id,),
        )
        return {r["grid_id"]: float(r["reachable"] or 0.0) for r in cur.fetchall()}


def _mode_of(modes: set[str]) -> str:
    if "calibrated" in modes and "conservative" in modes:
        return "mixed"
    if "calibrated" in modes:
        return "calibrated"
    return "conservative"


def _method_note(calibrated_grids: int, conservative_grids: int) -> str:
    return (
        f"校准网格 {calibrated_grids} 个（覆盖率稳定+现场核验+分层系数，含抽样误差区间）；"
        f"保守网格 {conservative_grids} 个（已核实人数下界、容量/登记上限约束）；"
        "跨来源重叠无法识别时取并集包络不相加；隐私抑制与覆盖下降只扩大未知范围。"
    )


# ----------------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------------
def get_estimate(conn, estimate_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM exposure_estimates WHERE estimate_id=%s", (estimate_id,))
        header = cur.fetchone()
        if header is None:
            raise KeyError(f"估计版本不存在: {estimate_id}")
        cur.execute(
            "SELECT * FROM exposure_grid_periods WHERE estimate_id=%s ORDER BY grid_id",
            (estimate_id,),
        )
        periods = cur.fetchall()
        cur.execute(
            "SELECT * FROM exposure_evidence_items WHERE estimate_id=%s "
            "ORDER BY grid_id, evidence_id",
            (estimate_id,),
        )
        evidence = cur.fetchall()
        cur.execute(
            "SELECT * FROM exposure_input_snapshots WHERE snapshot_id=%s",
            (header["snapshot_id"],),
        )
        snapshot = cur.fetchone()

    any_open = any(p["increase_upper"] is None for p in periods)
    totals = {
        "baseline_total": _r(sum(p["baseline_population"] for p in periods)),
        "increase_lower_total": _r(sum(p["increase_lower"] for p in periods)),
        "increase_upper_total": None if any_open else _r(
            sum(p["increase_upper"] for p in periods)),
        "decrease_lower_total": _r(sum(p["decrease_lower"] for p in periods)),
        "decrease_upper_total": _r(sum(p["decrease_upper"] for p in periods)),
        "delta_lower_total": _r(sum(p["delta_lower"] for p in periods)),
        "delta_upper_total": None if any_open else _r(
            sum(p["delta_upper"] for p in periods)),
        "point_unknown_sources": int(sum(p["point_unknown"] for p in periods)),
        "open_upper_grid_count": int(sum(1 for p in periods if p["increase_upper"] is None)),
        "grid_count": len(periods),
    }
    return {
        "estimate": header,
        "snapshot": snapshot,
        "periods": periods,
        "evidence": evidence,
        "totals": totals,
    }


def list_estimates(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.*,
                      (SELECT count(*) FROM exposure_grid_periods p
                       WHERE p.estimate_id=e.estimate_id) AS grid_count
               FROM exposure_estimates e
               ORDER BY e.window_start, e.estimate_version DESC"""
        )
        return cur.fetchall()
