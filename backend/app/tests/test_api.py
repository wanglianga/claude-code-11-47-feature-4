"""HTTP 端到端测试：情景创建、缓存命中、下钻、事件失效、比较与发布。"""
from datetime import date

from fastapi.testclient import TestClient

from app.main import app
from app.seed import C1, C2, T0

client = TestClient(app)


def _scenario_payload(name, **overrides):
    payload = {
        "name": name,
        "population_as_of": "2026-06-01",
        "departure_time": T0.isoformat(),
        "max_trip_minutes": 30,
        "road_cutoff": C1.isoformat(),
        "viewer_id": "e2e",
    }
    payload.update(overrides)
    return payload


def test_health_and_sources(fresh_db):
    assert client.get("/health").json()["status"] == "ok"
    docs = client.get("/sources/documents").json()["documents"]
    types = {d["dataset_type"] for d in docs}
    assert {"population", "road_node", "road_edge", "shelter", "road_event", "boundary"} <= types
    events = client.get("/sources/events?include_audit=true").json()
    assert len(events["audits"]) >= 3
    grids = client.get("/sources/grids", params={"as_of": "2026-06-01"}).json()
    assert len(grids["grids"]) >= 8
    assert any(c["allocation_ratio"] == 0.6 for c in grids["connectors"])


def test_scenario_lifecycle_cache_hit_and_drilldown(fresh_db):
    r1 = client.post("/scenarios", json=_scenario_payload("端到端情景")).json()
    assert r1["cache_hit"] is False
    r2 = client.post("/scenarios", json=_scenario_payload("端到端情景")).json()
    assert r2["cache_hit"] is True and r2["task_id"] == r1["task_id"]

    task_id = r1["task_id"]
    summary = client.get(f"/tasks/{task_id}/summary").json()
    assert summary["summary"]["total_population"] > 0
    shares = client.get(f"/tasks/{task_id}/grids").json()["shares"]
    statuses = {s["status"] for s in shares}
    assert {"covered", "uncovered", "unknown"} <= statuses

    witness = client.get(f"/tasks/{task_id}/witness", params={"grid_id": "G2"}).json()
    assert len(witness["shares"]) == 2
    assert witness["task_edge_dependencies"]
    arrivals = client.get(f"/tasks/{task_id}/arrivals").json()["arrivals"]
    assert all("witness_edge_path" in a for a in arrivals)

    # 查看者释放不取消任务
    rel = client.post(f"/tasks/{task_id}/release", json={"viewer_id": "e2e"}).json()
    assert rel["cancelled"] is False


def test_temporary_event_invalidation_flow(fresh_db):
    r = client.post("/scenarios", json=_scenario_payload("失效前")).json()
    event = {
        "event_id": "EVT-HTTP-1",
        "event_type": "closure",
        "edge_id": "E6",
        "effective_from": T0.isoformat(),
        "effective_to": None,
        "source_title": "API 即时封路",
        "raw_text": "API 端到端临时封路",
    }
    out = client.post("/events", json=event).json()
    assert out["invalidation"]["staled_total"] >= 1
    index = client.get("/cache/index").json()["cache_index"]
    mine = next(c for c in index if c["task_id"] == r["task_id"])
    assert mine["stale"] is True


def test_compare_and_publish_flow(fresh_db):
    a = client.post("/scenarios", json=_scenario_payload("HTTP-A", max_trip_minutes=60)).json()
    client.post("/admin/apply-c2-revision")
    b = client.post(
        "/scenarios",
        json=_scenario_payload("HTTP-B", max_trip_minutes=60, road_cutoff=C2.isoformat()),
    ).json()
    comp = client.post("/comparisons", json={"task_a_id": a["task_id"], "task_b_id": b["task_id"]}).json()
    newly = [g["grid_id"] for g in comp["attribution"]["newly_covered_grids"]]
    assert "G8" in newly

    pub = client.post("/publications", json={
        "task_a_id": a["task_id"], "task_b_id": b["task_id"],
        "title": "HTTP 发布", "created_by": "analyst",
    }).json()
    listing = client.get("/publications").json()["publications"]
    assert any(p["publication_id"] == pub["publication"]["publication_id"] for p in listing)
    detail = client.get(f"/publications/{pub['publication']['publication_id']}").json()
    assert detail["input_manifest"]["scenario_b"]["road_cutoff"].startswith("2026-07-15T08:45")


def test_precompute_and_rebuild_endpoints(fresh_db):
    pre = client.post("/cache/precompute", json={
        "population_as_of": "2026-06-01",
        "base_day": T0.isoformat(),
        "cutoff": C1.isoformat(),
        "deadline_minutes": 30,
        "offsets_minutes": [0, 30, 60],
    }).json()
    assert len(pre["precomputed"]) == 3
    versions = client.get("/network-versions").json()["versions"]
    assert len(versions) >= 1
    rebuild = client.post("/cache/rebuild", json={"new_cutoff": C2.isoformat()}).json()
    # 无失效窗口时重建数为 0，接口仍正常返回
    assert "rebuilt_count" in rebuild
