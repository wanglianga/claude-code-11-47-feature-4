"""混合计算管理：常规窗口预计算 + 临时事件即时计算。

关键语义：
- 复用以 *完整情景签名* 为键（人口基准日+出发时刻+时限+路网版本），不同出发
  时刻或开放条件的签名不同，缓存命中也绝不复用他者的覆盖面；
- 每个查看者在 compute_task_viewers 留痕，ref_count 计数；单个查看者取消只减
  引用，仍有其他查看者时任务继续运行；
- 封路修订沿 task_edge_dependencies 定位受影响任务，并把“同版本含该边但旧路径
  未经过该边”的任务列为恢复候选（此前不可达区域可能因重开而恢复），一并失效；
- 任务完成时若已被标记 stale，则只留痕结果、不写缓存索引；新版本路网拥有新签名，
  旧任务物理上无法覆盖新版本索引行（签名为主键）。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

import redis

from ..config import settings
from .coverage import CoverageEngine
from .snapshot import (
    get_or_create_network_version,
    load_snapshot,
    scenario_signature,
)

# 计算在请求线程内同步执行：psycopg 连接不跨线程共享，同步模型让任务状态、
# 缓存写入与失效标记在同一事务序列内完成，避免并发写覆盖。
_snapshot_cache: dict[int, Any] = {}
_snapshot_lock = threading.Lock()
_signature_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _redis_client() -> Optional[redis.Redis]:
    try:
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        client.ping()
        return client
    except Exception:
        return None  # Redis 不可用时降级为纯库内协调，不阻断计算


def _load_cached_snapshot(conn, version_id: int):
    with _snapshot_lock:
        snap = _snapshot_cache.get(version_id)
    if snap is None:
        snap = load_snapshot(conn, version_id)
        with _snapshot_lock:
            _snapshot_cache[version_id] = snap
    return snap


def _signature_lock(signature: str) -> threading.Lock:
    with _registry_lock:
        lock = _signature_locks.get(signature)
        if lock is None:
            lock = threading.Lock()
            _signature_locks[signature] = lock
        return lock


class ComputeManager:
    # -------------------------------------------------- 情景与任务复用 --
    def get_or_create(
        self,
        conn,
        *,
        name: str,
        population_as_of,
        departure_time: datetime,
        max_trip_minutes: int,
        road_cutoff: datetime,
        viewer_id: str,
        kind: str = "on_demand",
        note: Optional[str] = None,
        boundary_id: Optional[str] = None,
    ) -> dict[str, Any]:
        version = get_or_create_network_version(conn, road_cutoff)
        signature = scenario_signature(
            population_as_of, departure_time, max_trip_minutes,
            version["version_id"], boundary_id,
        )
        lock = _signature_lock(signature)
        with lock:  # 同签名并发请求串行化，第二个请求直接复用任务
            return self._get_or_create_locked(
                conn,
                name=name,
                population_as_of=population_as_of,
                departure_time=departure_time,
                max_trip_minutes=max_trip_minutes,
                road_cutoff=road_cutoff,
                version=version,
                signature=signature,
                viewer_id=viewer_id,
                kind=kind,
                note=note,
                boundary_id=boundary_id,
            )

    def _get_or_create_locked(
        self, conn, *, name, population_as_of, departure_time, max_trip_minutes,
        road_cutoff, version, signature, viewer_id, kind, note, boundary_id,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scenarios WHERE signature=%s", (signature,))
            scenario = cur.fetchone()
            if scenario is None:
                scenario_id = uuid.uuid4()
                cur.execute(
                    """INSERT INTO scenarios
                           (scenario_id, name, population_as_of, departure_time,
                            max_trip_minutes, road_cutoff, boundary_id, graph_version_id,
                            signature, evidence_cutoff_at, created_by, note)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'analyst',%s) RETURNING *""",
                    (scenario_id, name, population_as_of, departure_time, max_trip_minutes,
                     road_cutoff, boundary_id, version["version_id"], signature,
                     road_cutoff, note),
                )
                scenario = cur.fetchone()

            # 1) 已完成且未失效的缓存行：直接命中（行内自带证据截止时间）
            cur.execute(
                """SELECT c.*, t.status AS task_status, t.stale AS task_stale
                   FROM coverage_cache_index c
                   JOIN compute_tasks t ON t.task_id = c.task_id
                   WHERE c.signature=%s AND c.stale = FALSE AND t.status='done'""",
                (signature,),
            )
            cached = cur.fetchone()
            if cached is not None:
                self._attach_viewer(cur, cached["task_id"], viewer_id)
                conn.commit()
                return {
                    "scenario": scenario,
                    "task_id": cached["task_id"],
                    "status": "done",
                    "cache_hit": True,
                    "evidence_cutoff_at": cached["evidence_cutoff_at"],
                    "message": "缓存命中：签名完全一致（含出发时刻/时限/路网版本）",
                }

            # 2) 已有进行中任务：加入查看者，复用同一计算
            cur.execute(
                "SELECT * FROM compute_tasks WHERE signature=%s AND status IN ('queued','running') "
                "ORDER BY created_at DESC LIMIT 1",
                (signature,),
            )
            running = cur.fetchone()
            if running is not None:
                self._attach_viewer(cur, running["task_id"], viewer_id)
                conn.commit()
                return {
                    "scenario": scenario,
                    "task_id": running["task_id"],
                    "status": running["status"],
                    "cache_hit": False,
                    "evidence_cutoff_at": running["evidence_cutoff_at"],
                    "message": "复用进行中的同签名计算任务",
                }

            # 3) 新建任务并即时计算
            task_id = uuid.uuid4()
            cur.execute(
                """INSERT INTO compute_tasks
                       (task_id, scenario_id, signature, graph_version_id, kind,
                        status, ref_count, evidence_cutoff_at)
                   VALUES (%s,%s,%s,%s,%s,'queued',1,%s)""",
                (task_id, scenario["scenario_id"], signature, version["version_id"],
                 kind, road_cutoff),
            )
            cur.execute(
                "INSERT INTO compute_task_viewers (task_id, viewer_id) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (task_id, viewer_id),
            )
            conn.commit()

        self._run_task(conn, task_id, version["version_id"], departure_time,
                       max_trip_minutes, population_as_of, road_cutoff, boundary_id)
        return {
            "scenario": scenario,
            "task_id": task_id,
            "status": "done",
            "cache_hit": False,
            "evidence_cutoff_at": road_cutoff,
            "message": "即时计算完成" if kind == "on_demand" else "预计算完成",
        }

    def _attach_viewer(self, cur, task_id, viewer_id) -> None:
        cur.execute(
            "INSERT INTO compute_task_viewers (task_id, viewer_id) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (task_id, viewer_id),
        )
        cur.execute(
            "UPDATE compute_tasks SET ref_count = (SELECT count(*) FROM compute_task_viewers "
            "WHERE task_id=%s) WHERE task_id=%s",
            (task_id, task_id),
        )

    def release_viewer(self, conn, task_id, viewer_id) -> dict:
        """单个查看者离开：只减引用；仍有其他查看者时任务不取消。"""
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM compute_task_viewers WHERE task_id=%s AND viewer_id=%s",
                (task_id, viewer_id),
            )
            cur.execute(
                "UPDATE compute_tasks SET ref_count = (SELECT count(*) FROM compute_task_viewers "
                "WHERE task_id=%s) WHERE task_id=%s RETURNING ref_count, status, kind",
                (task_id, task_id),
            )
            row = cur.fetchone()
            conn.commit()
        return {
            "task_id": str(task_id),
            "viewer_id": viewer_id,
            "remaining_viewers": row["ref_count"],
            "cancelled": False,  # 计算结果属于签名缓存，不随查看者离开而取消
            "note": "仍有其他查看者，任务继续" if row["ref_count"] > 0 else "无查看者，结果保留于缓存供后续签名复用",
        }

    # -------------------------------------------------------- 任务执行 --
    def _run_task(self, conn, task_id, version_id, departure_time,
                  max_trip_minutes, population_as_of, evidence_cutoff,
                  boundary_id=None) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE compute_tasks SET status='running', started_at=now() "
                "WHERE task_id=%s AND status='queued'",
                (task_id,),
            )
            conn.commit()
        try:
            snapshot = _load_cached_snapshot(conn, version_id)
            deadline = departure_time + timedelta(minutes=max_trip_minutes)
            task = {
                "task_id": task_id,
                "departure_time": departure_time,
                "deadline": deadline,
                "evidence_cutoff_at": evidence_cutoff,
            }
            summary = CoverageEngine(
                conn, snapshot, task, population_as_of, boundary_id=boundary_id
            ).run()

            with conn.cursor() as cur:
                # CAS 防覆：仅当任务仍归属于该版本且未失效时才写缓存索引
                cur.execute(
                    "SELECT stale, graph_version_id FROM compute_tasks WHERE task_id=%s",
                    (task_id,),
                )
                guard = cur.fetchone()
                cur.execute(
                    "UPDATE compute_tasks SET status='done', finished_at=now() WHERE task_id=%s",
                    (task_id,),
                )
                if not guard["stale"]:
                    cur.execute(
                        """INSERT INTO coverage_cache_index
                               (signature, task_id, graph_version_id, departure_time,
                                max_trip_minutes, evidence_cutoff_at, stale)
                           SELECT signature, task_id, graph_version_id, %s, %s, %s, FALSE
                           FROM compute_tasks WHERE task_id=%s
                           ON CONFLICT (signature) DO UPDATE SET
                               task_id=EXCLUDED.task_id,
                               graph_version_id=EXCLUDED.graph_version_id,
                               evidence_cutoff_at=EXCLUDED.evidence_cutoff_at,
                               stale=FALSE, computed_at=now()""",
                        (departure_time, max_trip_minutes, evidence_cutoff, task_id),
                    )
                conn.commit()

            client = _redis_client()
            if client is not None:
                try:
                    client.set(
                        f"flood:task:{task_id}",
                        json.dumps({"status": "done", "evidence_cutoff_at": str(evidence_cutoff)}),
                        ex=86400,
                    )
                    client.publish("flood:invalidate", json.dumps({"task_id": str(task_id), "event": "done"}))
                except Exception:
                    pass
        except Exception as exc:  # 失败留痕，不吞异常信息
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE compute_tasks SET status='failed', error=%s, finished_at=now() "
                    "WHERE task_id=%s",
                    (repr(exc), task_id),
                )
                conn.commit()
            raise

    # ------------------------------------------------ 事件驱动的失效 --
    def invalidate_for_edge(self, conn, edge_id: str, reason: str) -> dict:
        """封路/重开修订后定位受影响任务，并显式筛查恢复候选。"""
        with conn.cursor() as cur:
            # 受影响：旧见证路径实际经过该边的任务
            cur.execute(
                "SELECT DISTINCT task_id FROM task_edge_dependencies WHERE edge_id=%s",
                (edge_id,),
            )
            dependent = {r["task_id"] for r in cur.fetchall()}

            # 恢复候选：路网版本含该边、但旧路径未经过它的已完成任务。
            # 重开可能让此前不可达区域获得新路径，不能只重算旧结果里出现过的路径。
            cur.execute(
                """
                SELECT DISTINCT t.task_id
                FROM compute_tasks t
                JOIN road_edge_revisions r
                  ON r.edge_id = %s
                 AND r.valid_from <= (SELECT road_cutoff FROM scenarios WHERE scenario_id = t.scenario_id)
                 AND (r.valid_to IS NULL OR r.valid_to >
                      (SELECT road_cutoff FROM scenarios WHERE scenario_id = t.scenario_id))
                WHERE t.status='done'
                """,
                (edge_id,),
            )
            contains_edge = {r["task_id"] for r in cur.fetchall()}
            recovery_candidates = contains_edge - dependent
            affected = dependent | recovery_candidates
            if affected:
                affected_list = list(affected)
                cur.execute(
                    "UPDATE compute_tasks SET stale=TRUE WHERE task_id = ANY(%s)",
                    (affected_list,),
                )
                cur.execute(
                    "UPDATE coverage_cache_index SET stale=TRUE WHERE task_id = ANY(%s)",
                    (affected_list,),
                )
            conn.commit()

        client = _redis_client()
        if client is not None:
            try:
                client.publish(
                    "flood:invalidate",
                    json.dumps({
                        "edge_id": edge_id,
                        "reason": reason,
                        "affected": len(dependent),
                        "recovery_candidates": len(recovery_candidates),
                    }),
                )
            except Exception:
                pass

        return {
            "edge_id": edge_id,
            "reason": reason,
            "affected_task_ids": [str(x) for x in sorted(dependent)],
            "recovery_candidate_task_ids": [str(x) for x in sorted(recovery_candidates)],
            "staled_total": len(affected),
        }

    # ------------------------------------------------------- 窗口预计算 --
    def precompute_windows(
        self, conn, *, population_as_of, base_day: datetime, deadline_minutes: int,
        cutoff: datetime, offsets_minutes: list[int], viewer_id: str = "precompute",
    ) -> list[dict]:
        results = []
        for offset in offsets_minutes:
            departure = base_day + timedelta(minutes=offset)
            results.append(
                self.get_or_create(
                    conn,
                    name=f"常规窗口 T+{offset:03d}min（预计算）",
                    population_as_of=population_as_of,
                    departure_time=departure,
                    max_trip_minutes=deadline_minutes,
                    road_cutoff=cutoff,
                    viewer_id=viewer_id,
                    kind="precomputed",
                    note=f"预计算窗口 offset={offset}min",
                )
            )
        return results

    def rebuild_stale_precomputed(self, conn, new_cutoff: datetime) -> dict:
        """路网更新后重建失效的预计算窗口（同窗口参数、新截止点 → 新签名新版本）。"""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT s.population_as_of, s.departure_time, s.max_trip_minutes,
                       s.boundary_id
                FROM compute_tasks t
                JOIN scenarios s ON s.scenario_id = t.scenario_id
                WHERE t.kind='precomputed' AND t.stale = TRUE
                """
            )
            windows = cur.fetchall()
        rebuilt = []
        for w in windows:
            rebuilt.append(
                self.get_or_create(
                    conn,
                    name=f"常规窗口重建（截止 {new_cutoff.isoformat()}）",
                    population_as_of=w["population_as_of"],
                    departure_time=w["departure_time"],
                    max_trip_minutes=w["max_trip_minutes"],
                    road_cutoff=new_cutoff,
                    viewer_id="precompute-rebuild",
                    kind="precomputed",
                    note="失效窗口重建",
                    boundary_id=w["boundary_id"],
                )
            )
        return {"new_cutoff": new_cutoff, "rebuilt_count": len(rebuilt), "rebuilt": rebuilt}


manager = ComputeManager()
