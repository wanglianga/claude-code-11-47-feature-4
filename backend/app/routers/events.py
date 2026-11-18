"""道路事件录入、即时失效、预计算与重建管理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..schemas_api import EventCreate, InvalidateRequest, PrecomputeRequest, RebuildRequest
from ..seed import _add_doc
from ..services.compute import manager
from .deps import get_db, jsonable
from psycopg.types.json import Json

router = APIRouter(tags=["事件与缓存"])


@router.post("/events")
def create_event(payload: EventCreate, conn=Depends(get_db)):
    """临时封路/限速登记：立即成为后续即时计算的输入（received_at=now）。

    登记本身不删除旧结果，而是把受影响签名标记 stale；下一次同窗口查询按新
    截止点产生新签名与新路网版本，旧任务无法覆盖新版本索引。
    """
    with conn.cursor() as cur:
        doc_id = _add_doc(
            cur, "road_event", payload.source_title, "时间：UTC；速度：km/h",
            payload.effective_from, payload.effective_from,
            raw=payload.raw_text or f"{payload.event_type} on {payload.edge_id}",
        )
        cur.execute(
            "INSERT INTO road_event_registry (event_id, edge_id, event_type) "
            "VALUES (%s,%s,%s) ON CONFLICT (event_id) DO NOTHING",
            (payload.event_id, payload.edge_id, payload.event_type),
        )
        cur.execute(
            "SELECT COALESCE(MAX(revision_no),0)+1 AS next_rev FROM road_events WHERE event_id=%s",
            (payload.event_id,),
        )
        next_rev = cur.fetchone()["next_rev"]
        cur.execute(
            """INSERT INTO road_events
                   (event_id, revision_no, event_type, edge_id, effective_from, effective_to,
                    speed_limit_kmh, supersedes_event_id, status, source_doc_id, received_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,
                       CASE WHEN %s > 1 THEN %s END, 'active', %s, now())""",
            (payload.event_id, next_rev, payload.event_type, payload.edge_id,
             payload.effective_from, payload.effective_to, payload.speed_limit_kmh,
             next_rev, payload.event_id, doc_id),
        )
        cur.execute(
            """INSERT INTO road_event_revisions
                   (event_id, revision_no, change_summary, change_json, source_doc_id)
               VALUES (%s,%s,'临时事件即时登记',%s,%s)""",
            (payload.event_id, next_rev,
             Json({"edge_id": payload.edge_id, "type": payload.event_type,
                   "from": payload.effective_from.isoformat(),
                   "to": payload.effective_to.isoformat() if payload.effective_to else None}),
             doc_id),
        )
        conn.commit()
    invalidation = manager.invalidate_for_edge(conn, payload.edge_id, "临时事件登记")
    return jsonable({"event_id": payload.event_id, "invalidation": invalidation})


@router.post("/cache/invalidate")
def invalidate(payload: InvalidateRequest, conn=Depends(get_db)):
    """按边手工触发失效（依赖边定位 + 恢复候选筛查）。"""
    return jsonable(manager.invalidate_for_edge(conn, payload.edge_id, payload.reason))


@router.post("/cache/precompute")
def precompute(payload: PrecomputeRequest, conn=Depends(get_db)):
    """常规出发窗口预计算。"""
    results = manager.precompute_windows(
        conn,
        population_as_of=payload.population_as_of,
        base_day=payload.base_day,
        deadline_minutes=payload.deadline_minutes,
        cutoff=payload.cutoff,
        offsets_minutes=payload.offsets_minutes,
        viewer_id="precompute-api",
    )
    return jsonable({"precomputed": results})


@router.post("/cache/rebuild")
def rebuild(payload: RebuildRequest, conn=Depends(get_db)):
    """路网更新后重建失效的预计算窗口（新版本、新签名）。"""
    return jsonable(manager.rebuild_stale_precomputed(conn, payload.new_cutoff))


@router.get("/cache/index")
def cache_index(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT c.*, t.status, t.kind, t.stale AS task_stale, s.name AS scenario_name
               FROM coverage_cache_index c
               JOIN compute_tasks t ON t.task_id=c.task_id
               JOIN scenarios s ON s.scenario_id=t.scenario_id
               ORDER BY c.computed_at DESC"""
        )
        rows = cur.fetchall()
    return jsonable({"cache_index": rows})


@router.get("/network-versions")
def network_versions(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM road_network_versions ORDER BY version_id")
        rows = cur.fetchall()
    return jsonable({"versions": rows})
