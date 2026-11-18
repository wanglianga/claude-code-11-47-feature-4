"""临时暴露人口：来源登记、区间估计版本与结果查询。

模块边界对应需求：临时信号接入（本路由的登记接口）→ 覆盖率/重叠校准
（exposure 服务 + overlap 规则）→ 区间计算（estimates）→ 情景发布。
所有登记先落来源目录（source_documents）再落业务表，保证可追溯。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..schemas_api import (
    ExposureEstimateCreate,
    MobileSignalCreate,
    OverlapRuleCreate,
    TransferCreate,
    VenuePresenceCreate,
)
from ..seed import _add_doc
from ..services.exposure import (
    build_exposure_estimate,
    get_estimate,
    list_estimates,
)
from .deps import get_db, jsonable

router = APIRouter(prefix="/exposure", tags=["临时暴露人口"])


# ------------------------------------------------------------- 区间估计版本 --
@router.post("/estimates")
def create_estimate(payload: ExposureEstimateCreate, conn=Depends(get_db)):
    """按网格×时段生成临时暴露人口区间（新估计版本，旧版本保留）。"""
    if payload.window_end <= payload.window_start:
        raise HTTPException(400, "时间窗结束必须晚于开始")
    try:
        result = build_exposure_estimate(
            conn,
            window_start=payload.window_start,
            window_end=payload.window_end,
            evidence_cutoff=payload.evidence_cutoff,
            privacy_threshold=payload.privacy_threshold,
            boundary_id=payload.boundary_id,
            baseline_as_of=payload.baseline_pop_as_of,
            created_by=payload.created_by,
            reachable_task_id=payload.reachable_task_id,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return jsonable(result)


@router.get("/estimates")
def get_estimates(conn=Depends(get_db)):
    return jsonable({"estimates": list_estimates(conn)})


@router.get("/estimates/{estimate_id}")
def estimate_detail(estimate_id: str, conn=Depends(get_db)):
    try:
        return jsonable(get_estimate(conn, estimate_id))
    except KeyError:
        raise HTTPException(404, "估计版本不存在")


@router.get("/estimates/{estimate_id}/grid/{grid_id}")
def estimate_grid_drilldown(estimate_id: str, grid_id: str, conn=Depends(get_db)):
    """单网格下钻：区间结果 + 逐来源采用证据。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM exposure_grid_periods WHERE estimate_id=%s AND grid_id=%s",
            (estimate_id, grid_id),
        )
        period = cur.fetchone()
        if period is None:
            raise HTTPException(404, "该估计版本下没有此网格的区间结果")
        cur.execute(
            "SELECT * FROM exposure_evidence_items WHERE estimate_id=%s AND grid_id=%s "
            "ORDER BY evidence_id",
            (estimate_id, grid_id),
        )
        evidence = cur.fetchall()
    return jsonable({"period": period, "evidence": evidence})


# ------------------------------------------------------------- 来源登记 -------
@router.post("/mobile-signals")
def register_mobile(payload: MobileSignalCreate, conn=Depends(get_db)):
    """移动信号采样登记；suppressed=TRUE 时 sample_count 必须为空（隐私抑制不记零）。"""
    if payload.suppressed and payload.sample_count is not None:
        raise HTTPException(400, "隐私抑制的采样不得携带 sample_count（抑制即不可见）")
    if not payload.suppressed and payload.sample_count is None:
        raise HTTPException(400, "非抑制采样必须提供 sample_count")
    with conn.cursor() as cur:
        doc_id = _add_doc(
            cur, "mobile_signal", payload.source_title,
            "单位：人（去重采样）；覆盖率：运营商人群占比",
            payload.window_start, payload.window_end,
            raw=payload.raw_text or f"{payload.operator_id} @ {payload.grid_id}",
            org="运营商信令合作方",
        )
        cur.execute(
            """INSERT INTO mobile_signal_samples
                   (operator_id, grid_id, stratum, window_start, window_end, sample_count,
                    suppressed, operator_coverage, coverage_stable, field_verified,
                    source_doc_id, received_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) RETURNING sample_id""",
            (payload.operator_id, payload.grid_id, payload.stratum,
             payload.window_start, payload.window_end, payload.sample_count,
             payload.suppressed, payload.operator_coverage, payload.coverage_stable,
             payload.field_verified, doc_id),
        )
        sample_id = cur.fetchone()["sample_id"]
        conn.commit()
    return jsonable({"sample_id": sample_id, "doc_id": doc_id,
                     "note": "已登记；覆盖率下降或抑制只会在后续估计中扩大未知范围"})


@router.post("/venue-presence")
def register_venue(payload: VenuePresenceCreate, conn=Depends(get_db)):
    """景区/寄宿学校在场登记；suppressed=TRUE 时 verified_count 必须为空。"""
    if payload.privacy_suppressed and payload.verified_count is not None:
        raise HTTPException(400, "隐私抑制的在场记录不得携带 verified_count")
    if not payload.privacy_suppressed and payload.verified_count is None:
        raise HTTPException(400, "非抑制在场记录必须提供 verified_count")
    if payload.presence_kind not in ("scenic", "boarding_school"):
        raise HTTPException(400, "presence_kind 仅支持 scenic / boarding_school")
    with conn.cursor() as cur:
        doc_id = _add_doc(
            cur, "venue_presence", payload.source_title, "单位：人（签到/点名核实）",
            payload.window_start, payload.window_end,
            raw=payload.raw_text or f"{payload.venue_id} @ {payload.grid_id}",
            org="景区/学校管理方",
        )
        cur.execute(
            """INSERT INTO venue_presence_records
                   (venue_id, venue_name, presence_kind, grid_id, window_start, window_end,
                    verified_count, privacy_suppressed, source_doc_id, received_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) RETURNING presence_id""",
            (payload.venue_id, payload.venue_name, payload.presence_kind, payload.grid_id,
             payload.window_start, payload.window_end, payload.verified_count,
             payload.privacy_suppressed, doc_id),
        )
        presence_id = cur.fetchone()["presence_id"]
        conn.commit()
    return jsonable({"presence_id": presence_id, "doc_id": doc_id})


@router.post("/transfers")
def register_transfer(payload: TransferCreate, conn=Depends(get_db)):
    """临时转移登记；late_arrival=TRUE 标记晚到，只进入新估计版本。"""
    with conn.cursor() as cur:
        doc_id = _add_doc(
            cur, "transfer_registration", payload.source_title, "单位：人（登记核实）",
            payload.window_start, payload.window_end,
            raw=payload.raw_text or f"transfer @ {payload.grid_id}",
            org="街道转移安置工作组",
        )
        cur.execute(
            """INSERT INTO transfer_registrations
                   (grid_id, window_start, window_end, registered_persons,
                    destination_grid_id, late_arrival, source_doc_id, received_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,now()) RETURNING transfer_id""",
            (payload.grid_id, payload.window_start, payload.window_end,
             payload.registered_persons, payload.destination_grid_id,
             payload.late_arrival, doc_id),
        )
        transfer_id = cur.fetchone()["transfer_id"]
        conn.commit()
    return jsonable({"transfer_id": transfer_id, "doc_id": doc_id,
                     "late_arrival": payload.late_arrival})


@router.post("/overlap-rules")
def register_overlap_rule(payload: OverlapRuleCreate, conn=Depends(get_db)):
    """登记可证明的跨来源重叠关系（无制度/现场依据不得登记）。"""
    if payload.max_overlap_persons is not None \
            and payload.max_overlap_persons < payload.min_overlap_persons:
        raise HTTPException(400, "max_overlap_persons 不得小于 min_overlap_persons")
    with conn.cursor() as cur:
        if payload.evidence_cutoff is None:
            cur.execute("SELECT now() AS now")
            cutoff = cur.fetchone()["now"]
        else:
            cutoff = payload.evidence_cutoff
        doc_id = _add_doc(
            cur, "overlap_rule", "跨来源人员重叠关系核验单", "单位：人",
            cutoff, cutoff,
            raw=payload.evidence_note, org="现场核验组",
        )
        cur.execute(
            """INSERT INTO exposure_overlap_rules
                   (grid_id, source_a, source_b, relation, direction,
                    min_overlap_persons, max_overlap_persons, evidence_note, source_doc_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING rule_id""",
            (payload.grid_id, payload.source_a, payload.source_b,
             payload.relation, payload.direction,
             payload.min_overlap_persons, payload.max_overlap_persons,
             payload.evidence_note, doc_id),
        )
        rule_id = cur.fetchone()["rule_id"]
        conn.commit()
    return jsonable({"rule_id": rule_id, "doc_id": doc_id})


# ------------------------------------------------------------- 来源浏览 -------
@router.get("/sources")
def exposure_sources(conn=Depends(get_db)):
    """浏览全部临时来源原文行（含抑制标记、覆盖率、晚到标记与校准系数）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM mobile_signal_samples ORDER BY sample_id")
        mobile = cur.fetchall()
        cur.execute("SELECT * FROM venue_presence_records ORDER BY presence_id")
        venues = cur.fetchall()
        cur.execute("SELECT * FROM venue_capacity ORDER BY capacity_id")
        capacities = cur.fetchall()
        cur.execute("SELECT * FROM transfer_registrations ORDER BY transfer_id")
        transfers = cur.fetchall()
        cur.execute("SELECT * FROM exposure_overlap_rules ORDER BY rule_id")
        rules = cur.fetchall()
        cur.execute("SELECT * FROM mobile_signal_calibration ORDER BY calibration_id")
        calibrations = cur.fetchall()
    return jsonable({
        "mobile_signals": mobile, "venue_presence": venues, "venue_capacity": capacities,
        "transfers": transfers, "overlap_rules": rules, "calibrations": calibrations,
    })
