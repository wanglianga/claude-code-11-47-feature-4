"""按资料截止点从数据库物化不可变路网快照，以及人口基准日数据加载。"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Optional

from ..engine.models import Edge, GraphSnapshot, Node, OpenWindow, RoadEvent, Shelter


def _fingerprint(rows: list[dict]) -> str:
    payload = json.dumps(rows, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def get_or_create_network_version(conn, cutoff_at: datetime) -> dict:
    """返回截止点对应的路网版本行；不存在则物化为新版本。

    纳入条件体现“证据截止时间”：
      - 边修订：valid_from <= cutoff < valid_to；
      - 事件：平台 received_at <= cutoff 且当前状态 active（已撤销的封路不再阻断，
        但其修订过程保留在 road_event_revisions 审计表中）。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT edge_id, revision_no, from_node, to_node, length_m, base_speed_kmh
            FROM road_edge_revisions
            WHERE valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)
            ORDER BY edge_id, revision_no
            """,
            (cutoff_at, cutoff_at),
        )
        edge_rows = cur.fetchall()

        cur.execute(
            """
            SELECT DISTINCT ON (event_id) event_id, event_type, edge_id,
                   effective_from, effective_to, speed_limit_kmh, revision_no
            FROM road_events
            WHERE received_at <= %s AND status = 'active'
            ORDER BY event_id, revision_no DESC
            """,
            (cutoff_at,),
        )
        event_rows = cur.fetchall()

        edge_fp = _fingerprint(edge_rows)
        event_fp = _fingerprint(event_rows)

        cur.execute(
            "SELECT * FROM road_network_versions WHERE cutoff_at = %s "
            "ORDER BY version_id DESC",
            (cutoff_at,),
        )
        rows = cur.fetchall()
        matching = next(
            (r for r in rows if r["edge_fingerprint"] == edge_fp and r["event_fingerprint"] == event_fp),
            None,
        )
        if matching is not None:
            return matching

        # 同一截止点出现迟到修订：物化为新版本（version_id 变化 → 情景签名变化，
        # 旧版本结果保留且只能通过旧签名访问，不可能覆盖新版本索引）。
        cur.execute(
            """INSERT INTO road_network_versions
                   (cutoff_at, edge_fingerprint, event_fingerprint, note)
               VALUES (%s, %s, %s, %s) RETURNING *""",
            (cutoff_at, edge_fp, event_fp, f"截止点 {cutoff_at.isoformat()} 自动物化"),
        )
        new_row = cur.fetchone()
        if rows:
            old_ids = [r["version_id"] for r in rows]  # list 才能适配为 PG 数组
            cur.execute(
                "UPDATE compute_tasks SET stale=TRUE WHERE graph_version_id = ANY(%s)",
                (old_ids,),
            )
            cur.execute(
                "UPDATE coverage_cache_index SET stale=TRUE WHERE graph_version_id = ANY(%s)",
                (old_ids,),
            )
        conn.commit()
        return new_row


def load_snapshot(conn, version_id: int) -> GraphSnapshot:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM road_network_versions WHERE version_id=%s", (version_id,))
        version = cur.fetchone()
        cutoff = version["cutoff_at"]

        cur.execute("SELECT * FROM road_nodes ORDER BY node_id")
        node_rows = cur.fetchall()
        cur.execute(
            """
            SELECT r.* FROM road_edge_revisions r
            WHERE r.valid_from <= %s AND (r.valid_to IS NULL OR r.valid_to > %s)
            """,
            (cutoff, cutoff),
        )
        edge_rows = cur.fetchall()
        cur.execute(
            """
            SELECT DISTINCT ON (event_id) event_id, event_type, edge_id,
                   effective_from, effective_to, speed_limit_kmh, revision_no
            FROM road_events
            WHERE received_at <= %s AND status='active'
            ORDER BY event_id, revision_no DESC
            """,
            (cutoff,),
        )
        event_rows = cur.fetchall()
        cur.execute("SELECT * FROM shelters WHERE active = TRUE ORDER BY shelter_id")
        shelter_rows = cur.fetchall()
        cur.execute("SELECT * FROM shelter_open_windows ORDER BY shelter_id, open_from")
        window_rows = cur.fetchall()

    nodes = {
        n["node_id"]: Node(
            node_id=n["node_id"],
            name=n["name"] or n["node_id"],
            x=float(n["geom_x"]),
            y=float(n["geom_y"]),
            allow_wait=bool(n["allow_wait"]),
        )
        for n in node_rows
    }

    events_by_edge: dict[str, list[RoadEvent]] = {}
    for e in event_rows:
        events_by_edge.setdefault(e["edge_id"], []).append(
            RoadEvent(
                event_id=e["event_id"],
                event_type=e["event_type"],
                edge_id=e["edge_id"],
                effective_from=e["effective_from"],
                effective_to=e["effective_to"],
                speed_limit_kmh=e["speed_limit_kmh"],
                revision_no=e["revision_no"],
            )
        )

    edges: list[Edge] = []
    adjacency: dict[str, list[Edge]] = {nid: [] for nid in nodes}
    for r in edge_rows:
        edge = Edge(
            edge_id=r["edge_id"],
            from_node=r["from_node"],
            to_node=r["to_node"],
            length_m=float(r["length_m"]),
            base_speed_kmh=float(r["base_speed_kmh"]),
            events=tuple(events_by_edge.get(r["edge_id"], [])),
        )
        edges.append(edge)
        adjacency.setdefault(r["from_node"], []).append(edge)

    windows_by_shelter: dict[str, list[OpenWindow]] = {}
    for w in window_rows:
        windows_by_shelter.setdefault(w["shelter_id"], []).append(
            OpenWindow(open_from=w["open_from"], open_to=w["open_to"])
        )
    shelters = [
        Shelter(
            shelter_id=s["shelter_id"],
            name=s["name"],
            node_id=s["node_id"],
            x=float(s["geom_x"]),
            y=float(s["geom_y"]),
            windows=tuple(windows_by_shelter.get(s["shelter_id"], [])),
        )
        for s in shelter_rows
    ]

    return GraphSnapshot(
        version_id=version_id,
        cutoff_at=cutoff,
        nodes=nodes,
        edges=tuple(edges),
        shelters=tuple(shelters),
        adjacency={k: tuple(v) for k, v in adjacency.items()},
        edge_fingerprint=version["edge_fingerprint"],
        event_fingerprint=version["event_fingerprint"],
    )


def load_population(conn, as_of: date) -> dict[str, dict]:
    """按人口基准日取每网格最新可见修订，并附互斥连接份额。

    返回 {grid_id: {population, geom, shares:[{node_id, ratio, share_pop}], ratio_sum}}。
    无连接或比例不闭合的网格由覆盖服务单列 unknown。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.grid_id, g.name, g.geom_x, g.geom_y, g.boundary_id,
                   p.population, p.revision_no AS pop_revision
            FROM pop_grids g
            JOIN LATERAL (
                SELECT * FROM pop_grid_revisions r
                WHERE r.grid_id = g.grid_id AND r.as_of_date <= %s
                ORDER BY r.as_of_date DESC, r.revision_no DESC
                LIMIT 1
            ) p ON TRUE
            ORDER BY g.grid_id
            """,
            (as_of,),
        )
        grids = cur.fetchall()
        cur.execute(
            """
            SELECT DISTINCT ON (grid_id, node_id) grid_id, node_id, allocation_ratio
            FROM grid_connectors
            ORDER BY grid_id, node_id, revision_no DESC
            """
        )
        connector_rows = cur.fetchall()

    connectors: dict[str, list[dict]] = {}
    for c in connector_rows:
        connectors.setdefault(c["grid_id"], []).append(
            {"node_id": c["node_id"], "ratio": float(c["allocation_ratio"])}
        )

    result: dict[str, dict] = {}
    for g in grids:
        shares = connectors.get(g["grid_id"], [])
        ratio_sum = sum(s["ratio"] for s in shares)
        for s in shares:
            s["share_pop"] = float(g["population"]) * s["ratio"]
        result[g["grid_id"]] = {
            "grid_id": g["grid_id"],
            "name": g["name"],
            "x": float(g["geom_x"]),
            "y": float(g["geom_y"]),
            "boundary_id": g["boundary_id"],
            "population": float(g["population"]),
            "pop_revision": g["pop_revision"],
            "shares": shares,
            "ratio_sum": ratio_sum,
        }
    return result


def scenario_signature(
    population_as_of: date,
    departure_time: datetime,
    max_trip_minutes: int,
    graph_version_id: int,
    boundary_id: Optional[str] = None,
) -> str:
    """完整情景签名：人口基准日 + 出发时刻 + 时限 + 路网版本 + 分析边界，缺一不可。"""
    raw = json.dumps(
        [
            population_as_of.isoformat(),
            departure_time.isoformat(),
            int(max_trip_minutes),
            int(graph_version_id),
            boundary_id or "__ALL__",
        ],
        ensure_ascii=False,
    )
    return "sig_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
