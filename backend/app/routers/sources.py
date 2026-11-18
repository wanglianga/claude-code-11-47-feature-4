"""来源目录浏览：原文、单位、发生时间、修订与截止时间全程可查。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .deps import get_db, jsonable

router = APIRouter(prefix="/sources", tags=["来源目录"])


@router.get("/documents")
def list_documents(dataset_type: str | None = None, conn=Depends(get_db)):
    with conn.cursor() as cur:
        if dataset_type:
            cur.execute(
                "SELECT * FROM source_documents WHERE dataset_type=%s ORDER BY doc_id",
                (dataset_type,),
            )
        else:
            cur.execute("SELECT * FROM source_documents ORDER BY doc_id")
        rows = cur.fetchall()
    return jsonable({"documents": rows})


@router.get("/boundaries")
def list_boundaries(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM region_boundaries ORDER BY boundary_id")
        rows = cur.fetchall()
    return jsonable({"boundaries": rows})


@router.get("/nodes")
def list_nodes(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM road_nodes ORDER BY node_id")
        rows = cur.fetchall()
    return jsonable({"nodes": rows})


@router.get("/edges")
def list_edges(conn=Depends(get_db)):
    """返回边的全部修订（版本化原文），不隐藏历史修订。"""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT r.*, d.title AS source_title, d.unit_note
               FROM road_edge_revisions r
               LEFT JOIN source_documents d ON d.doc_id = r.source_doc_id
               ORDER BY r.edge_id, r.revision_no"""
        )
        rows = cur.fetchall()
    return jsonable({"edge_revisions": rows})


@router.get("/events")
def list_events(include_audit: bool = False, conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.*, d.title AS source_title, d.raw_text AS source_raw,
                      d.unit_note, d.source_occurred_at, d.cutoff_at AS doc_cutoff_at
               FROM road_events e
               LEFT JOIN source_documents d ON d.doc_id = e.source_doc_id
               ORDER BY e.edge_id, e.event_id, e.revision_no"""
        )
        events = cur.fetchall()
        audits = []
        if include_audit:
            cur.execute("SELECT * FROM road_event_revisions ORDER BY event_id, revision_no")
            audits = cur.fetchall()
    return jsonable({"events": events, "audits": audits})


@router.get("/shelters")
def list_shelters(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM shelters ORDER BY shelter_id")
        shelters = cur.fetchall()
        cur.execute("SELECT * FROM shelter_open_windows ORDER BY shelter_id, open_from")
        windows = cur.fetchall()
    return jsonable({"shelters": shelters, "open_windows": windows})


@router.get("/grids")
def list_grids(as_of, conn=Depends(get_db)):
    """网格 + 基准日可见人口修订 + 互斥连接份额（含分配依据来源）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.*, p.population, p.revision_no AS pop_revision, p.as_of_date AS pop_as_of,
                   d.title AS pop_source_title
            FROM pop_grids g
            JOIN LATERAL (
                SELECT * FROM pop_grid_revisions r
                WHERE r.grid_id = g.grid_id AND r.as_of_date <= %s
                ORDER BY r.as_of_date DESC, r.revision_no DESC LIMIT 1
            ) p ON TRUE
            LEFT JOIN source_documents d ON d.doc_id =
                (SELECT source_doc_id FROM pop_grid_revisions r
                 WHERE r.grid_id=g.grid_id ORDER BY r.as_of_date DESC, r.revision_no DESC LIMIT 1)
            ORDER BY g.grid_id
            """,
            (as_of,),
        )
        grids = cur.fetchall()
        cur.execute(
            """SELECT c.*, d.title AS source_title
               FROM grid_connectors c
               LEFT JOIN source_documents d ON d.doc_id = c.source_doc_id
               ORDER BY c.grid_id, c.node_id"""
        )
        connectors = cur.fetchall()
    return jsonable({"grids": grids, "connectors": connectors})
