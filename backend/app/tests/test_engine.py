"""时变路网引擎规则测试：单向边、绕行、分时限速、整段封路判断、等待与时限、安置点开放。"""
from datetime import timedelta

from app.engine.models import NodeLabel, OpenWindow, Shelter
from app.engine.traversal import (
    _blocked_by_closure,
    _travel_duration_seconds,
    earliest_feasible_entry,
    is_shelter_open,
    shelter_arrival,
    solve,
)
from app.seed import C1, C2, T0, apply_c2_revision
from app.services.snapshot import get_or_create_network_version, load_snapshot


def _snapshot(db, cutoff):
    version = get_or_create_network_version(db, cutoff)
    return load_snapshot(db, version["version_id"])


def test_one_way_edge_blocks_reverse_direction(db):
    snap = _snapshot(db, C1)
    # E1 仅 N1→N2：从 N2 出发不可能回到 N1
    labels = solve(snap, "N2", T0, T0 + timedelta(minutes=30))
    assert "N1" not in labels
    forward = solve(snap, "N1", T0, T0 + timedelta(minutes=30))
    assert "N2" in forward


def test_detour_chosen_when_bridge_closed_mid_trip(db):
    """08:05 出发：到达 N3 时 E3 正处于封闭区间，N3 不可等待，绕行 N2→N5→N6→N4。"""
    snap = _snapshot(db, C1)
    labels = solve(snap, "N1", T0 + timedelta(minutes=5), T0 + timedelta(minutes=35))
    n4 = labels["N4"]
    assert n4.edge_path == ("E1", "E4", "E5", "E6")
    assert n4.arrival_time == T0 + timedelta(minutes=9)


def test_closure_checked_over_whole_traversal_interval_not_just_departure(db):
    """进入时开放、行驶中撞上封路区间也必须判不可行（不能只看出发时刻）。"""
    snap = _snapshot(db, C1)
    e3 = next(e for e in snap.edges if e.edge_id == "E3")
    # 08:04:30 进入、预计 08:05:30 离开：与 [08:05,08:20) 相交 → 整段不可通行
    _, exit_mid = _travel_duration_seconds(e3, T0 + timedelta(minutes=4.5))
    assert _blocked_by_closure(e3, T0 + timedelta(minutes=4.5), exit_mid) is True
    # 08:04 进入、08:05 前已离开：半开区间不相交 → 不阻断（边界口径明确）
    _, exit_boundary = _travel_duration_seconds(e3, T0 + timedelta(minutes=4))
    assert _blocked_by_closure(e3, T0 + timedelta(minutes=4), exit_boundary) is False
    # 允许等待时，最早可行候选被推迟到恢复时刻 08:20
    feasible = earliest_feasible_entry(e3, T0 + timedelta(minutes=4.5), T0 + timedelta(minutes=30))
    assert feasible is not None
    t_enter, t_exit, _ = feasible
    assert t_enter == T0 + timedelta(minutes=20)
    assert t_exit == T0 + timedelta(minutes=21)


def test_time_dependent_speed_limit_applies_to_whole_segment(db):
    snap = _snapshot(db, C1)
    e1 = next(e for e in snap.edges if e.edge_id == "E1")
    # 08:10 进入，整段落在 20km/h 限速区间：1000m / 20kmh = 180 秒
    t_enter, t_exit, sec = earliest_feasible_entry(
        e1, T0 + timedelta(minutes=10), T0 + timedelta(minutes=40)
    )
    assert round(sec) == 180
    assert t_exit == T0 + timedelta(minutes=13)
    # 08:09 进入、08:10 前离开：不受限速影响，仍为 60 秒
    _, _, sec2 = earliest_feasible_entry(e1, T0 + timedelta(minutes=9), T0 + timedelta(minutes=40))
    assert round(sec2) == 60


def test_no_wait_node_cannot_wait_for_reopening(db):
    snap = _snapshot(db, C1)
    # N3 不允许等待：08:10 时 E3 封闭，N3 没有任何可行出路
    labels = solve(snap, "N3", T0 + timedelta(minutes=10), T0 + timedelta(minutes=30))
    assert set(labels) == {"N3"}


def test_wait_for_reopening_then_pruned_when_beyond_deadline(fresh_db):
    db = fresh_db
    apply_c2_revision(db)  # E7 修订为 08:40 重开
    snap = _snapshot(db, C2)
    # N4 允许等待，但 30 分钟时限内等不到 08:40 → N7 不可达
    short = solve(snap, "N4", T0, T0 + timedelta(minutes=30))
    assert "N7" not in short
    # 60 分钟时限：08:40 进入 E7，08:41 到 N7，等待段被完整记录
    long = solve(snap, "N4", T0, T0 + timedelta(minutes=60))
    n7 = long["N7"]
    assert n7.arrival_time == T0 + timedelta(minutes=41)
    assert sum(w.wait_seconds for w in n7.waits) == 2400.0
    assert all(w.reason == "closure" for w in n7.waits)


def test_shelter_must_be_open_at_actual_arrival():
    shelter = Shelter(
        shelter_id="SX", name="X", node_id="N7", x=0, y=0,
        windows=(OpenWindow(T0 + timedelta(minutes=60), None),),
    )
    label = NodeLabel(
        node_id="N7", arrival_time=T0 + timedelta(minutes=41),
        total_wait_seconds=2400.0, node_path=("N4", "N7"), edge_path=("E7",), waits=(),
    )
    # 节点不允许等待 → 到达时关闭即不可用
    assert shelter_arrival(shelter, False, label, T0 + timedelta(minutes=90)) is None
    # 允许等待且时限覆盖开放起点 → 等到 09:00，等待 1140 秒
    decision = shelter_arrival(shelter, True, label, T0 + timedelta(minutes=90))
    assert decision is not None
    arrival, wait_sec = decision
    assert arrival == T0 + timedelta(minutes=60)
    assert wait_sec == 1140.0
    # 时限早于开放起点 → 仍不计入覆盖
    assert shelter_arrival(shelter, True, label, T0 + timedelta(minutes=50)) is None
    assert is_shelter_open(shelter, T0 + timedelta(minutes=60)) is True
    assert is_shelter_open(shelter, T0 + timedelta(minutes=30)) is False


def test_witness_path_records_every_edge_and_wait(fresh_db):
    db = fresh_db
    apply_c2_revision(db)
    snap = _snapshot(db, C2)
    labels = solve(snap, "N4", T0, T0 + timedelta(minutes=60))
    n7 = labels["N7"]
    assert n7.edge_path == ("E7",)
    assert n7.node_path == ("N4", "N7")
    assert n7.waits[0].waited_from == T0
    assert n7.waits[0].waited_until == T0 + timedelta(minutes=40)
