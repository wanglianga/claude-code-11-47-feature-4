"""覆盖分析：在一个计算任务内完成全部网格份额的可达判定与去重汇总。

口径（对应需求）：
- 每个网格的连接份额互斥，份额人口 = 网格人口 × 分配比例；
- 同一份额能到多个安置点只计一次（取最早实际到达者）；
- 网格级覆盖按份额合并，同网格人口绝不重复计数；
- 无连接、比例不闭合、连接节点缺失于路网的部分一律单列 unknown，
  既不默认全覆盖也不默认零需求。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from psycopg.types.json import Json

from ..engine import (
    GraphSnapshot,
    NodeLabel,
    label_with_shelter_wait,
    shelter_arrival,
    solve,
)
from .snapshot import load_population

_RATIO_EPS = 1e-9


class CoverageEngine:
    def __init__(self, conn, snapshot: GraphSnapshot, task: dict, population_as_of,
                 boundary_id=None):
        self.conn = conn
        self.snapshot = snapshot
        self.task = task
        self.as_of = population_as_of
        self.deadline = task["deadline"]
        self.departure = task["departure_time"]
        self.evidence_cutoff = task["evidence_cutoff_at"]
        self.boundary_id = boundary_id
        all_grids = load_population(conn, population_as_of)
        if boundary_id:
            self.grids = {gid: g for gid, g in all_grids.items()
                          if g["boundary_id"] == boundary_id}
        else:
            self.grids = all_grids

    # ----------------------------------------------------------- 主流程 --
    def run(self) -> dict[str, Any]:
        label_cache: dict[str, dict[str, NodeLabel]] = {}
        share_rows: list[dict] = []
        edge_deps: set[str] = set()
        node_best: dict[str, NodeLabel] = {}

        for grid_id, grid in sorted(self.grids.items()):
            share_rows.extend(
                self._evaluate_grid(grid_id, grid, label_cache, edge_deps, node_best)
            )

        self._persist_node_arrivals(node_best)
        self._persist_edge_dependencies(edge_deps)
        summary = self._persist_shares_and_summary(share_rows)
        return summary

    # ----------------------------------------------------- 单网格评估 --
    def _evaluate_grid(
        self,
        grid_id: str,
        grid: dict,
        label_cache: dict[str, dict[str, NodeLabel]],
        edge_deps: set[str],
        node_best: dict[str, NodeLabel],
    ) -> list[dict]:
        base = {
            "task_id": self.task["task_id"],
            "grid_id": grid_id,
            "evidence_cutoff_at": self.evidence_cutoff,
        }
        population = grid["population"]

        # 未知口径 1：完全没有连接依据
        if not grid["shares"]:
            return [
                self._unknown_row(
                    base, grid, 0, population, None,
                    "unknown_no_connector", "网格未登记任何路网连接位置，无法定位到路网",
                )
            ]
        # 未知口径 2：比例之和 > 1（数据矛盾，整格列为未知）
        if grid["ratio_sum"] > 1.0 + _RATIO_EPS:
            return [
                self._unknown_row(
                    base, grid, 0, population, None,
                    "unknown_ratio_over_one",
                    f"连接分配比例之和 {grid['ratio_sum']:.4f} > 1，分配依据矛盾",
                )
            ]

        rows: list[dict] = []
        for idx, share in enumerate(grid["shares"]):
            node_id = share["node_id"]
            share_pop = share["share_pop"]
            row = {
                **base,
                "share_index": idx,
                "connector_node": node_id,
                "share_population": share_pop,
            }
            # 未知口径 3：连接节点不在当前路网版本中
            if node_id not in self.snapshot.nodes:
                rows.append(
                    self._unknown_row(
                        row, grid, idx, share_pop, node_id,
                        "unknown_connector_missing",
                        f"连接节点 {node_id} 不存在于截止点路网版本中",
                    )
                )
                continue

            labels = label_cache.get(node_id)
            if labels is None:
                labels = solve(self.snapshot, node_id, self.departure, self.deadline)
                label_cache[node_id] = labels
                for lab in labels.values():
                    edge_deps.update(lab.edge_path)
                    best = node_best.get(lab.node_id)
                    if best is None or lab.arrival_time < best.arrival_time:
                        node_best[lab.node_id] = lab

            best_choice = self._best_shelter_for_share(node_id, labels)
            if best_choice is None:
                rows.append(
                    {
                        **row,
                        "reachable": False,
                        "shelter_id": None,
                        "departure_time": self.departure,
                        "arrival_time": None,
                        "total_wait_sec": 0.0,
                        "witness_edge_path": None,
                        "status": "uncovered",
                        "reason": self._unreachable_reason(node_id, labels),
                    }
                )
            else:
                shelter, final_label, shelter_wait = best_choice
                rows.append(
                    {
                        **row,
                        "reachable": True,
                        "shelter_id": shelter.shelter_id,
                        "departure_time": self.departure,
                        "arrival_time": final_label.arrival_time,
                        "total_wait_sec": round(
                            final_label.total_wait_seconds, 3
                        ),
                        "witness_edge_path": Json(list(final_label.edge_path)),
                        "status": "covered",
                        "reason": None,
                    }
                )

        # 未知口径 4：比例不闭合，未分配的那部分人口单列未知
        gap = 1.0 - grid["ratio_sum"]
        if gap > _RATIO_EPS:
            rows.append(
                self._unknown_row(
                    base, grid, len(grid["shares"]), population * gap, None,
                    "unknown_allocation_gap",
                    f"连接分配比例之和 {grid['ratio_sum']:.4f} < 1，缺少 {gap:.2%} 份额的分配依据",
                )
            )
        return rows

    def _best_shelter_for_share(
        self, origin: str, labels: dict[str, NodeLabel]
    ) -> Optional[tuple]:
        """在总时限内寻找实际到达时刻最早且当时开放的安置点。"""
        candidates = []
        for shelter in self.snapshot.shelters:
            label = labels.get(shelter.node_id)
            if label is None:
                continue
            node = self.snapshot.nodes.get(shelter.node_id)
            decision = shelter_arrival(shelter, bool(node and node.allow_wait), label, self.deadline)
            if decision is None:
                continue
            arrival, shelter_wait = decision
            if arrival > self.deadline:
                continue  # 等道路恢复/等开放后超出总时限，不计入覆盖
            final_label = label_with_shelter_wait(label, shelter_wait, shelter)
            if final_label.arrival_time > self.deadline:
                continue
            candidates.append((arrival, shelter, final_label, shelter_wait))
        if not candidates:
            return None
        arrival, shelter, final_label, shelter_wait = min(candidates, key=lambda c: c[0])
        return shelter, final_label, shelter_wait

    def _unreachable_reason(self, origin: str, labels: dict[str, NodeLabel]) -> str:
        shelter_nodes = {s.node_id: s for s in self.snapshot.shelters}
        reached = [(n, shelter_nodes[n]) for n in shelter_nodes if n in labels]
        if not reached:
            return "no_shelter_node_within_deadline: 时限内无任何安置点节点可达（含封路/单向边限制）"
        detail = []
        for node_id, shelter in reached:
            if shelter_arrival(shelter, bool(self.snapshot.nodes[node_id].allow_wait),
                               labels[node_id], self.deadline) is None:
                node = self.snapshot.nodes[node_id]
                why = ("节点不允许等待" if not node.allow_wait
                       else "等待至下一开放区间将超出总时限")
                detail.append(f"{shelter.shelter_id}@{node_id} 到达时未开放（{why}）")
        return "shelter_closed_at_arrival: " + "；".join(detail)

    # ------------------------------------------------------------- 落库 --
    def _persist_node_arrivals(self, node_best: dict[str, NodeLabel]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO node_arrivals
                       (task_id, node_id, arrival_time, wait_cost_sec,
                        witness_node_path, witness_edge_path)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (task_id, node_id) DO UPDATE SET
                       arrival_time = LEAST(node_arrivals.arrival_time, EXCLUDED.arrival_time)""",
                [
                    (
                        self.task["task_id"],
                        lab.node_id,
                        lab.arrival_time,
                        round(lab.total_wait_seconds, 3),
                        Json(list(lab.node_path)),
                        Json(list(lab.edge_path)),
                    )
                    for lab in node_best.values()
                ],
            )

    def _persist_edge_dependencies(self, edge_deps: set[str]) -> None:
        if not edge_deps:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO task_edge_dependencies (task_id, edge_id) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                [(self.task["task_id"], e) for e in sorted(edge_deps)],
            )

    def _persist_shares_and_summary(self, share_rows: list[dict]) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO grid_share_results
                       (task_id, grid_id, connector_node, share_index, share_population,
                        reachable, shelter_id, departure_time, arrival_time, total_wait_sec,
                        witness_edge_path, status, reason, evidence_cutoff_at)
                   VALUES (%(task_id)s,%(grid_id)s,%(connector_node)s,%(share_index)s,
                           %(share_population)s,%(reachable)s,%(shelter_id)s,%(departure_time)s,
                           %(arrival_time)s,%(total_wait_sec)s,%(witness_edge_path)s,
                           %(status)s,%(reason)s,%(evidence_cutoff_at)s)
                   ON CONFLICT (task_id, grid_id, share_index) DO NOTHING""",
                share_rows,
            )

        total = sum(g["population"] for g in self.grids.values())
        located = sum(
            g["population"] * min(g["ratio_sum"], 1.0)
            for g in self.grids.values()
            if g["shares"] and g["ratio_sum"] <= 1.0 + _RATIO_EPS
        )
        unknown = sum(r["share_population"] for r in share_rows if r["status"] == "unknown")
        covered_by_grid: dict[str, float] = {}
        for r in share_rows:
            if r["status"] == "covered":
                covered_by_grid[r["grid_id"]] = (
                    covered_by_grid.get(r["grid_id"], 0.0) + r["share_population"]
                )
        covered = sum(covered_by_grid.values())  # 份额互斥，天然去重
        uncovered = max(located - covered, 0.0)
        covered_grids = len(covered_by_grid)
        unknown_grids = len(
            {r["grid_id"] for r in share_rows if r["status"] == "unknown"}
        )

        summary = {
            "total_population": round(total, 3),
            "located_population": round(located, 3),
            "unknown_population": round(unknown, 3),
            "covered_population": round(covered, 3),
            "uncovered_population": round(uncovered, 3),
            "covered_grid_count": covered_grids,
            "unknown_grid_count": unknown_grids,
        }
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO coverage_summaries
                       (task_id, total_population, located_population, unknown_population,
                        covered_population, uncovered_population, covered_grid_count,
                        unknown_grid_count)
                   VALUES (%(task_id)s,%(total_population)s,%(located_population)s,
                           %(unknown_population)s,%(covered_population)s,
                           %(uncovered_population)s,%(covered_grid_count)s,
                           %(unknown_grid_count)s)
                   ON CONFLICT (task_id) DO UPDATE SET
                       total_population=EXCLUDED.total_population,
                       located_population=EXCLUDED.located_population,
                       unknown_population=EXCLUDED.unknown_population,
                       covered_population=EXCLUDED.covered_population,
                       uncovered_population=EXCLUDED.uncovered_population,
                       covered_grid_count=EXCLUDED.covered_grid_count,
                       unknown_grid_count=EXCLUDED.unknown_grid_count,
                       computed_at=now()""",
                {**summary, "task_id": self.task["task_id"]},
            )
        return summary

    def _unknown_row(
        self,
        base: dict,
        grid: dict,
        share_index: int,
        share_pop: float,
        node_id: Optional[str],
        reason_code: str,
        reason: str,
    ) -> dict:
        return {
            **base,
            "connector_node": node_id or "",
            "share_index": share_index,
            "share_population": round(share_pop, 3),
            "reachable": False,
            "shelter_id": None,
            "departure_time": None,
            "arrival_time": None,
            "total_wait_sec": 0.0,
            "witness_edge_path": None,
            "status": "unknown",
            "reason": f"{reason_code}: {reason}",
        }
