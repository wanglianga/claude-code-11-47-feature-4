"""情景对比：共同分母 + 变化归因分解。

防止“看似扩大覆盖”的误读：
1. 只在相同人口、边界、时间口径的网格集合（共同分母）上比较覆盖率；
2. 人口修订带来的分母变化（新增/修订网格、人口增减）单列，不计入道路改善；
3. 道路/事件/开放窗口变化体现为同口径网格覆盖状态的增减；
4. 分析边界变化（网格所属边界集合不同）单独列示；
5. 未知份额变化单列，未知永远不被解释成覆盖或零需求。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from psycopg.types.json import Json


def _task_grid_views(conn, task_id: str) -> dict[str, dict]:
    """把份额行按网格聚合为网格级视图。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT grid_id,
                   SUM(share_population) FILTER (WHERE status='covered')   AS covered_pop,
                   SUM(share_population) FILTER (WHERE status='uncovered') AS uncovered_pop,
                   SUM(share_population) FILTER (WHERE status='unknown')   AS unknown_pop,
                   BOOL_OR(reachable) AS any_reachable,
                   ARRAY_AGG(reason) FILTER (WHERE reason IS NOT NULL)     AS reasons,
                   ARRAY_AGG(DISTINCT shelter_id) FILTER (WHERE shelter_id IS NOT NULL) AS shelters
            FROM grid_share_results
            WHERE task_id=%s
            GROUP BY grid_id
            """,
            (task_id,),
        )
        rows = cur.fetchall()
    return {
        r["grid_id"]: {
            "covered_pop": float(r["covered_pop"] or 0.0),
            "uncovered_pop": float(r["uncovered_pop"] or 0.0),
            "unknown_pop": float(r["unknown_pop"] or 0.0),
            "reasons": [x for x in (r["reasons"] or []) if x],
            "shelters": [x for x in (r["shelters"] or []) if x],
        }
        for r in rows
    }


def _scenario_context(conn, task_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.task_id, t.evidence_cutoff_at, t.graph_version_id, t.kind, t.stale,
                   s.scenario_id, s.name, s.population_as_of, s.departure_time,
                   s.max_trip_minutes, s.road_cutoff, s.signature, s.boundary_id,
                   v.edge_fingerprint, v.event_fingerprint
            FROM compute_tasks t
            JOIN scenarios s ON s.scenario_id = t.scenario_id
            JOIN road_network_versions v ON v.version_id = t.graph_version_id
            WHERE t.task_id=%s
            """,
            (task_id,),
        )
        ctx = cur.fetchone()
        cur.execute("SELECT grid_id, boundary_id, population FROM pop_grids "
                    "JOIN LATERAL (SELECT population FROM pop_grid_revisions r "
                    "WHERE r.grid_id=pop_grids.grid_id AND r.as_of_date <= %s "
                    "ORDER BY as_of_date DESC, revision_no DESC LIMIT 1) p ON TRUE"
                    + (" WHERE pop_grids.boundary_id = %s" if ctx.get("boundary_id") else ""),
                    (ctx["population_as_of"], ctx["boundary_id"]) if ctx.get("boundary_id")
                    else (ctx["population_as_of"],))
        grids = {r["grid_id"]: {"boundary_id": r["boundary_id"], "population": float(r["population"])}
                 for r in cur.fetchall()}
        cur.execute("SELECT * FROM coverage_summaries WHERE task_id=%s", (task_id,))
        summary = cur.fetchone()
    ctx["grids"] = grids
    ctx["summary"] = summary
    return ctx


def build_comparison(conn, task_a_id: str, task_b_id: str) -> dict[str, Any]:
    ctx_a = _scenario_context(conn, task_a_id)
    ctx_b = _scenario_context(conn, task_b_id)
    view_a = _task_grid_views(conn, task_a_id)
    view_b = _task_grid_views(conn, task_b_id)

    grids_a = set(ctx_a["grids"])
    grids_b = set(ctx_b["grids"])
    common = sorted(grids_a & grids_b)
    boundary_only_a = sorted(grids_a - grids_b)
    boundary_only_b = sorted(grids_b - grids_a)

    same_pop_basis = ctx_a["population_as_of"] == ctx_b["population_as_of"]
    same_deadline = ctx_a["max_trip_minutes"] == ctx_b["max_trip_minutes"]

    per_grid: list[dict] = []
    buckets = {
        "newly_covered": [],
        "lost_coverage": [],
        "population_revised": [],
        "unknown_changed": [],
        "stable_covered": [],
        "stable_uncovered": [],
    }

    common_denom_a = common_denom_b = 0.0
    common_covered_a = common_covered_b = 0.0
    common_unknown_a = common_unknown_b = 0.0

    for gid in common:
        va, vb = view_a.get(gid, _empty_view()), view_b.get(gid, _empty_view())
        pop_a = ctx_a["grids"][gid]["population"]
        pop_b = ctx_b["grids"][gid]["population"]
        common_denom_a += pop_a
        common_denom_b += pop_b
        common_covered_a += va["covered_pop"]
        common_covered_b += vb["covered_pop"]
        common_unknown_a += va["unknown_pop"]
        common_unknown_b += vb["unknown_pop"]

        was_covered = va["covered_pop"] > 0
        now_covered = vb["covered_pop"] > 0
        pop_changed = abs(pop_a - pop_b) > 1e-9
        unknown_changed = abs(va["unknown_pop"] - vb["unknown_pop"]) > 1e-9

        entry = {
            "grid_id": gid,
            "boundary_id": ctx_b["grids"][gid]["boundary_id"],
            "population_a": pop_a,
            "population_b": pop_b,
            "covered_pop_a": round(va["covered_pop"], 3),
            "covered_pop_b": round(vb["covered_pop"], 3),
            "unknown_pop_a": round(va["unknown_pop"], 3),
            "unknown_pop_b": round(vb["unknown_pop"], 3),
            "shelters_a": va["shelters"],
            "shelters_b": vb["shelters"],
            "reasons_a": va["reasons"],
            "reasons_b": vb["reasons"],
            "classification": None,
        }

        if pop_changed:
            entry["classification"] = "population_revised"
            buckets["population_revised"].append(entry)
        elif was_covered and now_covered:
            entry["classification"] = "stable_covered"
            buckets["stable_covered"].append(entry)
        elif not was_covered and now_covered:
            entry["classification"] = "newly_covered"  # 同分母下的道路/开放条件改善
            buckets["newly_covered"].append(entry)
        elif was_covered and not now_covered:
            entry["classification"] = "lost_coverage"
            buckets["lost_coverage"].append(entry)
        else:
            entry["classification"] = "stable_uncovered"
            buckets["stable_uncovered"].append(entry)

        if unknown_changed and entry["classification"] not in ("population_revised",):
            buckets["unknown_changed"].append({**entry, "classification": "unknown_changed"})
        per_grid.append(entry)

    rate_a = common_covered_a / common_denom_a if common_denom_a else 0.0
    rate_b = common_covered_b / common_denom_b if common_denom_b else 0.0

    # 道路变化净效应（仅同人口口径网格）：新增覆盖人口 - 失去覆盖人口
    road_effect_pop = sum(e["covered_pop_b"] - e["covered_pop_a"]
                          for e in buckets["newly_covered"] + buckets["lost_coverage"])
    # 人口修订效应：修订网格在 B 口径下的覆盖人口变化（分母变化，不计入道路效应）
    population_effect = [
        {
            "grid_id": e["grid_id"],
            "population_delta": round(e["population_b"] - e["population_a"], 3),
            "covered_a": e["covered_pop_a"],
            "covered_b": e["covered_pop_b"],
        }
        for e in buckets["population_revised"]
    ]

    warnings = []
    if not same_pop_basis:
        warnings.append("两个人口基准日不同：总体覆盖率变化含人口资料修订影响，已在分解中单列，不能直接当作道路改善。")
    if boundary_only_a or boundary_only_b:
        warnings.append("分析边界不同：仅存在于单侧的网格已排除出共同分母，单列展示。")
    if not same_deadline:
        warnings.append("允许行程时长不同，比较口径不一致，请谨慎解读。")

    return {
        "scenario_a": _ctx_brief(ctx_a),
        "scenario_b": _ctx_brief(ctx_b),
        "common_denominator": {
            "grid_count": len(common),
            "population_a": round(common_denom_a, 3),
            "population_b": round(common_denom_b, 3),
            "covered_a": round(common_covered_a, 3),
            "covered_b": round(common_covered_b, 3),
            "unknown_a": round(common_unknown_a, 3),
            "unknown_b": round(common_unknown_b, 3),
            "coverage_rate_a": round(rate_a, 6),
            "coverage_rate_b": round(rate_b, 6),
            "rate_delta": round(rate_b - rate_a, 6),
        },
        "attribution": {
            "road_or_open_window_effect_population": round(road_effect_pop, 3),
            "newly_covered_grids": buckets["newly_covered"],
            "lost_coverage_grids": buckets["lost_coverage"],
            "population_revision_effect": population_effect,
            "boundary_added_grids": boundary_only_b,
            "boundary_removed_grids": boundary_only_a,
            "unknown_changed_grids": buckets["unknown_changed"],
            "stable_covered_count": len(buckets["stable_covered"]),
            "stable_uncovered_count": len(buckets["stable_uncovered"]),
        },
        "per_grid": per_grid,
        "warnings": warnings,
        "basis_checks": {
            "same_population_basis": same_pop_basis,
            "same_deadline": same_deadline,
            "same_road_cutoff": ctx_a["road_cutoff"] == ctx_b["road_cutoff"],
        },
    }


def publish_comparison(conn, task_a_id: str, task_b_id: str, title: str, user: str) -> dict:
    """落库发布记录：完整输入清单 + 比较结果 + 证据截止时间。"""
    comparison = build_comparison(conn, task_a_id, task_b_id)
    ctx_a = _scenario_context(conn, task_a_id)
    ctx_b = _scenario_context(conn, task_b_id)
    manifest = {
        "scenario_a": _full_manifest(ctx_a),
        "scenario_b": _full_manifest(ctx_b),
        "source_cutoffs": {
            "a_evidence_cutoff_at": str(ctx_a["evidence_cutoff_at"]),
            "b_evidence_cutoff_at": str(ctx_b["evidence_cutoff_at"]),
        },
    }
    pub_id = uuid.uuid4()
    cutoff = max(ctx_a["evidence_cutoff_at"], ctx_b["evidence_cutoff_at"])
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO publications
                   (publication_id, title, scenario_a_id, scenario_b_id,
                    input_manifest, comparison_json, evidence_cutoff_at, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (pub_id, title, ctx_a["scenario_id"], ctx_b["scenario_id"],
             Json(manifest),
             Json(json.loads(json.dumps(comparison, default=str))),
             cutoff, user),
        )
        row = cur.fetchone()
        conn.commit()
    return {"publication": row, "comparison": comparison, "input_manifest": manifest}


def _empty_view() -> dict:
    return {"covered_pop": 0.0, "uncovered_pop": 0.0, "unknown_pop": 0.0, "reasons": [], "shelters": []}


def _ctx_brief(ctx: dict) -> dict:
    return {
        "task_id": str(ctx["task_id"]),
        "scenario_id": str(ctx["scenario_id"]),
        "name": ctx["name"],
        "population_as_of": str(ctx["population_as_of"]),
        "departure_time": ctx["departure_time"].isoformat(),
        "max_trip_minutes": ctx["max_trip_minutes"],
        "road_cutoff": ctx["road_cutoff"].isoformat(),
        "graph_version_id": ctx["graph_version_id"],
        "evidence_cutoff_at": ctx["evidence_cutoff_at"].isoformat(),
        "stale": ctx["stale"],
        "summary": ctx["summary"],
    }


def _full_manifest(ctx: dict) -> dict:
    return {
        "scenario_id": str(ctx["scenario_id"]),
        "signature": ctx["signature"],
        "name": ctx["name"],
        "population_as_of": str(ctx["population_as_of"]),
        "departure_time": ctx["departure_time"].isoformat(),
        "max_trip_minutes": ctx["max_trip_minutes"],
        "road_cutoff": ctx["road_cutoff"].isoformat(),
        "graph_version_id": ctx["graph_version_id"],
        "edge_fingerprint": ctx["edge_fingerprint"],
        "event_fingerprint": ctx["event_fingerprint"],
        "grid_populations": {gid: g["population"] for gid, g in ctx["grids"].items()},
        "grid_boundaries": {gid: g["boundary_id"] for gid, g in ctx["grids"].items()},
    }
