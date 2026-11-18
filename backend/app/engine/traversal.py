"""时变路网最短路引擎。

核心规则（对应需求条文）：
1. 遍历一条边时，检查 *整个通行时段* [进入时刻, 离开时刻) 是否与封路事件
   相交，而不是只在出发时刻检查开放状态；分时限速同样按整段取最严格限速。
2. 仅 allow_wait 的节点可等待。等待只用于“等道路恢复”，等待成本逐段记录；
   等待后到达仍超出总时限的标签直接剪枝，不计入覆盖。
3. 安置点是否开放按 *实际到达时刻* 判断；到达时关闭且节点允许等待时，
   可等到下一开放区间的起点，等待计入总时长并受总时限约束。
4. 所有标签携带见证路径（节点序列 + 边序列 + 等待段），供前端下钻。

FIFO 说明：道路事件只施加“更慢/封闭”约束，最早到达时间关于进入时刻单调
不减，时间依赖 Dijkstra（按到达时刻取标签）可得到正确的最早到达。
"""
from __future__ import annotations

import heapq
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Optional

from .models import Edge, GraphSnapshot, NodeLabel, Shelter, WaitSegment

_EPS = timedelta(seconds=1)


def _intersects(
    interval_from: datetime,
    interval_to: datetime,
    ev_from: datetime,
    ev_to: Optional[datetime],
) -> bool:
    """半开区间 [interval_from, interval_to) 与 [ev_from, ev_to) 是否相交。"""
    if ev_to is None:
        return interval_to > ev_from
    return interval_from < ev_to and interval_to > ev_from


def _travel_duration_seconds(edge: Edge, t_entry: datetime) -> tuple[float, datetime]:
    """按覆盖整段通行时段的最严格限速计算（定点细化，最多 3 次）。"""
    base_sec = edge.length_m / 1000.0 / edge.base_speed_kmh * 3600.0
    t_exit = t_entry + timedelta(seconds=base_sec)
    speed = edge.base_speed_kmh
    for _ in range(3):
        new_speed = edge.base_speed_kmh
        for ev in edge.events:
            if ev.event_type != "speed_limit":
                continue
            if _intersects(t_entry, t_exit, ev.effective_from, ev.effective_to):
                new_speed = min(new_speed, ev.speed_limit_kmh)
        new_exit = t_entry + timedelta(
            seconds=edge.length_m / 1000.0 / new_speed * 3600.0
        )
        if new_speed == speed and abs((new_exit - t_exit).total_seconds()) < 1e-6:
            break
        speed, t_exit = new_speed, new_exit
    sec = (t_exit - t_entry).total_seconds()
    return sec, t_exit


def _blocked_by_closure(edge: Edge, t_entry: datetime, t_exit: datetime) -> bool:
    for ev in edge.events:
        if ev.event_type != "closure":
            continue
        if _intersects(t_entry, t_exit, ev.effective_from, ev.effective_to):
            return True
    return False


def earliest_feasible_entry(
    edge: Edge, t_entry: datetime, deadline: datetime
) -> Optional[tuple[datetime, datetime, float]]:
    """返回最早可行进入时刻、离开时刻与行驶秒数；不可行返回 None。

    候选进入时刻 = 当前时刻 + 各封路事件的恢复时刻。若封路没有恢复时间
    （effective_to 为空），该边不存在可行候选。
    """
    candidates: set[datetime] = {t_entry}
    for ev in edge.events:
        if ev.event_type != "closure":
            continue
        if ev.effective_to is None:
            continue  # 尚未恢复：不会产生候选
        if ev.effective_to > t_entry:
            candidates.add(ev.effective_to)

    for t_enter in sorted(candidates):
        dur, t_exit = _travel_duration_seconds(edge, t_enter)
        if t_exit > deadline:
            # 候选按时间递增，之后的候选只会更晚，可直接结束
            return None
        if _blocked_by_closure(edge, t_enter, t_exit):
            continue
        return t_enter, t_exit, dur
    return None


def solve(
    snapshot: GraphSnapshot,
    origin_node: str,
    departure_time: datetime,
    deadline: datetime,
) -> dict[str, NodeLabel]:
    """从单一起点出发的时间依赖 Dijkstra，返回全部未超时限节点的最早到达标签。"""
    if origin_node not in snapshot.nodes:
        return {}

    labels: dict[str, NodeLabel] = {
        origin_node: NodeLabel(
            node_id=origin_node,
            arrival_time=departure_time,
            total_wait_seconds=0.0,
            node_path=(origin_node,),
            edge_path=(),
            waits=(),
        )
    }
    queue: list[tuple[datetime, str]] = [(departure_time, origin_node)]

    while queue:
        t_arrive, u = heapq.heappop(queue)
        current = labels.get(u)
        if current is None or t_arrive != current.arrival_time:
            continue  # 过期堆条目
        if t_arrive > deadline:
            continue

        for edge in snapshot.adjacency.get(u, ()):  # 只沿有向边行进
            feasible = earliest_feasible_entry(edge, t_arrive, deadline)
            if feasible is None:
                continue
            t_enter, t_exit, _dur = feasible
            v = edge.to_node

            wait_sec = (t_enter - t_arrive).total_seconds()
            if wait_sec > 0 and not snapshot.nodes[u].allow_wait:
                continue  # 节点不允许等待：不能在此等道路恢复
            old = labels.get(v)
            if old is not None and t_exit >= old.arrival_time:
                continue
            waits = current.waits
            if wait_sec > 0:
                waits = waits + (
                    WaitSegment(
                        node_id=u,
                        waited_from=t_arrive,
                        waited_until=t_enter,
                        reason="closure",
                    ),
                )
            labels[v] = NodeLabel(
                node_id=v,
                arrival_time=t_exit,
                total_wait_seconds=current.total_wait_seconds + wait_sec,
                node_path=current.node_path + (v,),
                edge_path=current.edge_path + (edge.edge_id,),
                waits=waits,
            )
            heapq.heappush(queue, (t_exit, v))

    return labels


def is_shelter_open(shelter: Shelter, moment: datetime) -> bool:
    return any(
        w.open_from <= moment and (w.open_to is None or moment < w.open_to)
        for w in shelter.windows
    )


def shelter_arrival(
    shelter: Shelter,
    node_allows_wait: bool,
    label: NodeLabel,
    deadline: datetime,
) -> Optional[tuple[datetime, float]]:
    """判断安置点在总时限内是否可在开放状态到达。

    返回 (实际到达时刻, 为等安置点开放产生的等待秒数)；不可达返回 None。
    """
    t = label.arrival_time
    if is_shelter_open(shelter, t):
        return t, 0.0
    if not node_allows_wait:
        return None
    # 到达时关闭：只允许等到未来开放区间的起点
    future_starts = sorted(
        w.open_from for w in shelter.windows if w.open_from >= t and w.open_from <= deadline
    )
    for open_from in future_starts:
        if any(
            w.open_from == open_from and (w.open_to is None or open_from < w.open_to)
            for w in shelter.windows
        ):
            return open_from, (open_from - t).total_seconds()
    return None


def label_with_shelter_wait(label: NodeLabel, wait_sec: float, shelter: Shelter) -> NodeLabel:
    """在见证标签上追加“等安置点开放”的等待段（用于结果留痕）。"""
    if wait_sec <= 0:
        return label
    segment = WaitSegment(
        node_id=label.node_id,
        waited_from=label.arrival_time,
        waited_until=label.arrival_time + timedelta(seconds=wait_sec),
        reason="shelter_closed",
    )
    return replace(
        label,
        arrival_time=label.arrival_time + timedelta(seconds=wait_sec),
        total_wait_seconds=label.total_wait_seconds + wait_sec,
        waits=label.waits + (segment,),
    )
