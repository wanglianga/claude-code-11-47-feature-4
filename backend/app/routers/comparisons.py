"""情景比较与结果发布。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Json

from ..schemas_api import CompareRequest, PublishRequest
from ..services.comparison import build_comparison, publish_comparison
from .deps import get_db, jsonable

router = APIRouter(tags=["比较与发布"])


@router.post("/comparisons")
def compare(payload: CompareRequest, conn=Depends(get_db)):
    """以共同分母比较两个任务：道路效应、人口修订、边界变化分别展示。"""
    try:
        result = build_comparison(conn, payload.task_a_id, payload.task_b_id)
    except TypeError as exc:
        raise HTTPException(404, f"任务不存在或上下文不完整：{exc}")
    return jsonable(result)


@router.post("/publications")
def publish(payload: PublishRequest, conn=Depends(get_db)):
    """发布比较：完整输入清单（修订号/截止点/指纹）随结果落库。

    可选关联临时暴露估计版本：其估计头与输入快照指纹一并固化进发布清单，
    使“区间计算 → 情景发布”以输入快照关联、旧预案保留当时分母。
    """
    result = publish_comparison(
        conn, payload.task_a_id, payload.task_b_id, payload.title, payload.created_by
    )
    if payload.exposure_estimate_id:
        from ..services.exposure import get_estimate
        try:
            est = get_estimate(conn, payload.exposure_estimate_id)
        except KeyError:
            raise HTTPException(404, "关联的临时暴露估计版本不存在")
        result["input_manifest"]["exposure_estimate"] = {
            "estimate_id": str(est["estimate"]["estimate_id"]),
            "estimate_version": est["estimate"]["estimate_version"],
            "snapshot_id": str(est["estimate"]["snapshot_id"]),
            "source_row_fingerprint": est["snapshot"]["source_row_fingerprint"],
            "window_start": str(est["estimate"]["window_start"]),
            "window_end": str(est["estimate"]["window_end"]),
            "evidence_cutoff_at": str(est["estimate"]["evidence_cutoff_at"]),
            "privacy_threshold": est["estimate"]["privacy_threshold"],
            "totals": est["totals"],
        }
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE publications SET input_manifest=%s WHERE publication_id=%s",
                (Json(result["input_manifest"]),
                 result["publication"]["publication_id"]),
            )
            conn.commit()
        result["publication"] = {
            **result["publication"], "input_manifest": result["input_manifest"]
        }
    return jsonable(result)


@router.get("/publications")
def list_publications(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT publication_id, title, scenario_a_id, scenario_b_id, "
            "evidence_cutoff_at, created_by, created_at, input_manifest "
            "FROM publications ORDER BY created_at DESC"
        )
        rows = cur.fetchall()
    return jsonable({"publications": rows})


@router.get("/publications/{publication_id}")
def get_publication(publication_id: str, conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM publications WHERE publication_id=%s", (publication_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "发布记录不存在")
    return jsonable(row)
