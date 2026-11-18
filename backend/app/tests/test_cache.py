"""混合计算测试：签名缓存、查看者引用、临时事件即时生效、失效与恢复重算、版本防覆。"""
from datetime import date, timedelta

from app.seed import C1, C2, T0, apply_c2_revision
from app.services.compute import manager

AS_OF = date(2026, 6, 1)


def _run(db, *, name, cutoff=C1, minutes=30, viewer="pytest-a", departure=T0):
    return manager.get_or_create(
        db, name=name, population_as_of=AS_OF, departure_time=departure,
        max_trip_minutes=minutes, road_cutoff=cutoff, viewer_id=viewer,
    )


def test_same_signature_concurrent_requests_reuse_one_task(db):
    first = _run(db, name="复用-A", viewer="v1")
    second = _run(db, name="复用-A", viewer="v2")
    assert first["task_id"] == second["task_id"]
    assert second["cache_hit"] is True
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM compute_tasks WHERE signature=%s",
                    (_sig(db, first["task_id"]),))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT ref_count FROM compute_tasks WHERE task_id=%s", (first["task_id"],))
        assert cur.fetchone()["ref_count"] >= 2


def _sig(db, task_id):
    with db.cursor() as cur:
        cur.execute("SELECT signature FROM compute_tasks WHERE task_id=%s", (task_id,))
        return cur.fetchone()["signature"]


def test_different_departure_never_shares_cache_even_on_hit(db):
    r1 = _run(db, name="出发0800", departure=T0)
    r2 = _run(db, name="出发0810", departure=T0 + timedelta(minutes=10))
    assert r1["task_id"] != r2["task_id"]
    assert r2["cache_hit"] is False
    with db.cursor() as cur:
        cur.execute("SELECT count(DISTINCT signature) AS n FROM coverage_cache_index "
                    "WHERE task_id IN (%s,%s)", (r1["task_id"], r2["task_id"]))
        assert cur.fetchone()["n"] == 2


def test_release_one_viewer_does_not_cancel_shared_task(db):
    r = _run(db, name="共享任务", viewer="v10")
    _run(db, name="共享任务", viewer="v11")
    out = manager.release_viewer(db, r["task_id"], "v10")
    assert out["cancelled"] is False
    assert out["remaining_viewers"] >= 1
    with db.cursor() as cur:
        cur.execute("SELECT status FROM compute_tasks WHERE task_id=%s", (r["task_id"],))
        assert cur.fetchone()["status"] == "done"


def test_temporary_event_immediately_participates_and_invalidates(fresh_db):
    db = fresh_db
    before = _run(db, name="临时事件前")
    with db.cursor() as cur:
        # 登记一条 E6（N6→N4）即时封路，收到时间为现在（事件多修订行结构）
        cur.execute(
            """INSERT INTO source_documents
                   (dataset_type,title,unit_note,source_occurred_at,received_at,cutoff_at,raw_text)
               VALUES ('road_event','即时封路 E6','时间：UTC', now(), now(), now(),'临时测试')
               RETURNING doc_id"""
        )
        doc_id = cur.fetchone()["doc_id"]
        cur.execute(
            "INSERT INTO road_event_registry (event_id, edge_id, event_type) "
            "VALUES ('EVT1','E6','closure') ON CONFLICT (event_id) DO NOTHING"
        )
        cur.execute(
            """INSERT INTO road_events
                   (event_id,revision_no,event_type,edge_id,effective_from,effective_to,
                    status,source_doc_id,received_at)
               VALUES ('EVT1',1,'closure','E6', now(), now() + interval '2 hours',
                       'active',%s,now())""",
            (doc_id,),
        )
        db.commit()
    invalidation = manager.invalidate_for_edge(db, "E6", "即时封路测试")
    assert any(str(t) == str(before["task_id"]) for t in invalidation["affected_task_ids"])
    with db.cursor() as cur:
        cur.execute("SELECT stale FROM compute_tasks WHERE task_id=%s", (before["task_id"],))
        assert cur.fetchone()["stale"] is True
        cur.execute("SELECT stale FROM coverage_cache_index WHERE task_id=%s", (before["task_id"],))
        assert cur.fetchone()["stale"] is True


def test_reopening_recovers_previously_unreachable_grid(fresh_db):
    db = fresh_db
    before = _run(db, name="重开前", cutoff=C1, minutes=60)
    g8_before = _grid_status(db, before["task_id"], "G8")
    assert g8_before == "uncovered"
    apply_c2_revision(db)
    after = _run(db, name="重开后", cutoff=C2, minutes=60)
    assert _grid_status(db, after["task_id"], "G8") == "covered"
    # 旧任务保留且仍标记其旧证据截止时间，没有被新结果覆盖
    with db.cursor() as cur:
        cur.execute("SELECT evidence_cutoff_at FROM compute_tasks WHERE task_id=%s",
                    (before["task_id"],))
        assert cur.fetchone()["evidence_cutoff_at"] == C1
        cur.execute("SELECT count(*) AS n FROM road_network_versions")
        assert cur.fetchone()["n"] >= 2


def test_old_task_cannot_overwrite_new_version_index(fresh_db):
    db = fresh_db
    old = _run(db, name="旧版本任务", cutoff=C1)
    apply_c2_revision(db)
    new = _run(db, name="新版本任务", cutoff=C2)
    with db.cursor() as cur:
        cur.execute("SELECT graph_version_id FROM coverage_cache_index WHERE task_id=%s",
                    (old["task_id"],))
        old_v = cur.fetchone()["graph_version_id"]
        cur.execute("SELECT graph_version_id FROM coverage_cache_index WHERE task_id=%s",
                    (new["task_id"],))
        new_v = cur.fetchone()["graph_version_id"]
    assert old_v != new_v
    # 两条缓存行签名不同，各自指向自己的版本，不存在同签名跨版本覆写
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM coverage_cache_index WHERE signature IN "
                    "(SELECT signature FROM compute_tasks WHERE task_id IN (%s,%s))",
                    (old["task_id"], new["task_id"]))
        assert cur.fetchone()["n"] == 2


def test_rebuild_stale_precomputed_uses_new_cutoff(fresh_db):
    db = fresh_db
    pre = manager.precompute_windows(
        db, population_as_of=AS_OF, base_day=T0, deadline_minutes=30,
        cutoff=C1, offsets_minutes=[0, 30],
    )
    assert len(pre) == 2
    apply_c2_revision(db)
    # 新封路 EV4 落在 E2：先显式失效
    manager.invalidate_for_edge(db, "E2", "C2 新封路")
    rebuilt = manager.rebuild_stale_precomputed(db, C2)
    assert rebuilt["rebuilt_count"] == 2
    for item in rebuilt["rebuilt"]:
        assert item["evidence_cutoff_at"] == C2


def test_old_cutoff_snapshot_remains_reproducible_after_later_revision(fresh_db):
    """修订只能追加，不能改写旧截止点快照：C1 在 C2 修订录入后仍视为未恢复封路。"""
    from app.engine import solve
    from app.services.snapshot import get_or_create_network_version, load_snapshot

    db = fresh_db
    v_before = get_or_create_network_version(db, C1)
    snap_before = load_snapshot(db, v_before["version_id"])
    e7_before = next(
        ev for e in snap_before.edges if e.edge_id == "E7" for ev in e.events
    )
    assert e7_before.effective_to is None

    apply_c2_revision(db)  # 录入 EV3 r2（08:40 重开）与 EV4

    v_after = get_or_create_network_version(db, C1)
    snap_after = load_snapshot(db, v_after["version_id"])
    e7_after = next(
        ev for e in snap_after.edges if e.edge_id == "E7" for ev in e.events
    )
    # 旧截止点看到的仍是 r1：effective_to 必须保持 None
    assert e7_after.effective_to is None
    labels = solve(snap_after, "N4", T0, T0 + timedelta(minutes=60))
    assert "N7" not in labels

    # 新截止点 C2 才能看到重开
    v2 = get_or_create_network_version(db, C2)
    snap_c2 = load_snapshot(db, v2["version_id"])
    labels_c2 = solve(snap_c2, "N4", T0, T0 + timedelta(minutes=60))
    assert labels_c2["N7"].arrival_time == T0 + timedelta(minutes=41)


def _grid_status(db, task_id, grid_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT status FROM grid_share_results WHERE task_id=%s AND grid_id=%s "
            "ORDER BY share_index LIMIT 1",
            (task_id, grid_id),
        )
        row = cur.fetchone()
        return row["status"] if row else None
