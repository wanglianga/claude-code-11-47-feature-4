"""临时暴露人口 HTTP 端到端：来源登记、估计版本、下钻、校验与版本叠加。"""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.seed import EX_CUTOFF_1, EX_CUTOFF_2, W1_END, W1_START

client = TestClient(app)
NOW_ISO = lambda: datetime.now(timezone.utc).isoformat()


def _estimate_payload(cutoff=EX_CUTOFF_1, **overrides):
    payload = {
        "window_start": W1_START.isoformat(),
        "window_end": W1_END.isoformat(),
        "evidence_cutoff": cutoff.isoformat(),
        "privacy_threshold": 10,
        "baseline_pop_as_of": "2026-06-01",
        "created_by": "e2e",
    }
    payload.update(overrides)
    return payload


def test_exposure_sources_and_estimate_lifecycle(fresh_db):
    sources = client.get("/exposure/sources").json()
    assert len(sources["mobile_signals"]) >= 4
    assert len(sources["calibrations"]) >= 3
    assert any(t["late_arrival"] for t in sources["transfers"])

    r = client.post("/exposure/estimates", json=_estimate_payload())
    assert r.status_code == 200
    est = r.json()
    assert est["estimate"]["estimate_version"] == 1
    assert est["totals"]["open_upper_grid_count"] == 1  # G8 覆盖下降
    eid = est["estimate"]["estimate_id"]

    detail = client.get(f"/exposure/estimates/{eid}").json()
    assert len(detail["periods"]) == est["totals"]["grid_count"]

    drill = client.get(f"/exposure/estimates/{eid}/grid/G1").json()
    assert drill["period"]["increase_upper"] == 1340.0
    assert any(e["calibrated"] for e in drill["evidence"])


def test_suppressed_registration_validation(fresh_db):
    # 抑制采样不得携带人数（防止把抑制值伪装成确定数）
    r = client.post("/exposure/mobile-signals", json={
        "operator_id": "OPX", "grid_id": "G1",
        "window_start": W1_START.isoformat(), "window_end": W1_END.isoformat(),
        "sample_count": 5, "suppressed": True, "operator_coverage": 0.5,
    })
    assert r.status_code == 400
    # 非抑制采样必须给人数
    r2 = client.post("/exposure/mobile-signals", json={
        "operator_id": "OPX", "grid_id": "G1",
        "window_start": W1_START.isoformat(), "window_end": W1_END.isoformat(),
        "suppressed": False, "operator_coverage": 0.5,
    })
    assert r2.status_code == 400


def test_late_transfer_versioning_over_http(fresh_db):
    v1 = client.post("/exposure/estimates",
                     json=_estimate_payload(EX_CUTOFF_1)).json()
    g8_v1 = next(p for p in v1["periods"] if p["grid_id"] == "G8")
    assert g8_v1["decrease_lower"] == 80.0

    v2 = client.post("/exposure/estimates",
                     json=_estimate_payload(EX_CUTOFF_2)).json()
    assert v2["estimate"]["estimate_version"] == 2
    g8_v2 = next(p for p in v2["periods"] if p["grid_id"] == "G8")
    assert g8_v2["decrease_lower"] == 130.0

    # 旧版本仍可访问，分母不变
    old = client.get(f"/exposure/estimates/{v1['estimate']['estimate_id']}").json()
    old_g8 = next(p for p in old["periods"] if p["grid_id"] == "G8")
    assert old_g8["decrease_lower"] == 80.0
    listing = client.get("/exposure/estimates").json()["estimates"]
    assert {e["estimate_version"] for e in listing} == {1, 2}


def test_register_new_source_then_reestimate_widens_unknown(fresh_db):
    # 登记一笔覆盖率不稳定的新移动信号到 G4（原本只有转移减量）
    r = client.post("/exposure/mobile-signals", json={
        "operator_id": "OP2", "grid_id": "G4",
        "window_start": W1_START.isoformat(), "window_end": W1_END.isoformat(),
        "sample_count": 45, "suppressed": False, "operator_coverage": 0.25,
        "coverage_stable": False, "field_verified": False,
    })
    assert r.status_code == 200
    # 新登记 received_at=now，须用不早于当前的证据截止点才能取到该来源
    est = client.post("/exposure/estimates",
                      json=_estimate_payload(datetime.now(timezone.utc))).json()
    g4 = next(p for p in est["periods"] if p["grid_id"] == "G4")
    # 仅承认 45 人为下界，上界不可估计（不做比例外推）
    assert g4["increase_lower"] == 45.0
    assert g4["increase_upper"] is None


def test_overlap_rule_and_partial_window_endpoints(fresh_db):
    r = client.post("/exposure/overlap-rules", json={
        "grid_id": "G4", "source_a": "mobile", "source_b": "transfer",
        "relation": "disjoint", "evidence_note": "转移人群与在场信号互斥（核验）",
    })
    assert r.status_code == 200 and r.json()["rule_id"] > 0

    # 部分时间窗来源：窗口只与分析窗口相交，不作时间比例外推
    r2 = client.post("/exposure/venue-presence", json={
        "venue_id": "V-PARTIAL", "venue_name": "半窗场所", "presence_kind": "scenic",
        "grid_id": "G5",
        "window_start": (W1_START - __import__("datetime").timedelta(minutes=30)).isoformat(),
        "window_end": (W1_START + __import__("datetime").timedelta(minutes=15)).isoformat(),
        "verified_count": 70, "privacy_suppressed": False,
    })
    assert r2.status_code == 200
    est = client.post("/exposure/estimates",
                      json=_estimate_payload(datetime.now(timezone.utc))).json()
    g5 = next(p for p in est["periods"] if p["grid_id"] == "G5")
    # 半窗签到不作紧下界；无容量 → 不产生确定人数
    assert g5["increase_lower"] == 0.0 and g5["increase_upper"] is None
    notes = [e["note"] for e in est["evidence"] if e["grid_id"] == "G5"]
    assert any("partial_window" in n for n in notes)


def test_publication_links_exposure_estimate_snapshot(fresh_db):
    # 先生成一个临时暴露估计版本
    est = client.post("/exposure/estimates",
                      json=_estimate_payload(EX_CUTOFF_1)).json()
    eid = est["estimate"]["estimate_id"]
    # 再建两个覆盖任务并发布，发布清单关联估计版本
    a = client.post("/scenarios", json={
        "name": "发布A", "population_as_of": "2026-06-01",
        "departure_time": "2026-07-15T08:00:00+00:00", "max_trip_minutes": 30,
        "road_cutoff": EX_CUTOFF_1.isoformat(), "viewer_id": "e2e",
    }).json()
    b = client.post("/scenarios", json={
        "name": "发布B", "population_as_of": "2026-06-01",
        "departure_time": "2026-07-15T08:00:00+00:00", "max_trip_minutes": 60,
        "road_cutoff": EX_CUTOFF_1.isoformat(), "viewer_id": "e2e",
    }).json()
    pub = client.post("/publications", json={
        "task_a_id": a["task_id"], "task_b_id": b["task_id"],
        "title": "含临时暴露区间的预案", "created_by": "analyst",
        "exposure_estimate_id": eid,
    }).json()
    linked = pub["input_manifest"]["exposure_estimate"]
    assert linked["estimate_id"] == eid
    assert linked["estimate_version"] == 1
    assert linked["source_row_fingerprint"] == est["snapshot"]["source_row_fingerprint"]
    # 关联不存在的估计版本应被拒绝
    bad = client.post("/publications", json={
        "task_a_id": a["task_id"], "task_b_id": b["task_id"], "title": "x",
        "exposure_estimate_id": "00000000-0000-0000-0000-000000000000",
    })
    assert bad.status_code == 404
