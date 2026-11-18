"""共同分母对比测试：道路恢复、人口修订、边界变化分别归因，发布清单完整。"""
from datetime import date

from app.seed import C1, C2, T0, apply_c2_revision
from app.services.comparison import build_comparison, publish_comparison
from app.services.compute import manager

AS_OF_V1 = date(2026, 6, 1)
AS_OF_V2 = date(2026, 7, 1)


def _run(db, *, name, cutoff=C1, minutes=60, as_of=AS_OF_V1, boundary=None):
    return manager.get_or_create(
        db, name=name, population_as_of=as_of, departure_time=T0,
        max_trip_minutes=minutes, road_cutoff=cutoff, viewer_id="cmp",
        boundary_id=boundary,
    )


def test_road_reopening_attributed_separately_from_population(fresh_db):
    db = fresh_db
    a = _run(db, name="对比A-恢复前", cutoff=C1)
    apply_c2_revision(db)
    b = _run(db, name="对比B-恢复后", cutoff=C2)
    comp = build_comparison(db, a["task_id"], b["task_id"])
    # 同人口、同边界、同时限：共同网格集合一致，差异只能来自道路/开放条件
    assert comp["basis_checks"]["same_population_basis"] is True
    assert comp["basis_checks"]["same_deadline"] is True
    newly = [g["grid_id"] for g in comp["attribution"]["newly_covered_grids"]]
    assert "G8" in newly
    assert comp["attribution"]["road_or_open_window_effect_population"] == 700.0
    assert comp["attribution"]["population_revision_effect"] == []
    assert comp["common_denominator"]["grid_count"] >= 48


def test_population_revision_never_counted_as_road_improvement(fresh_db):
    db = fresh_db
    a = _run(db, name="人口v1", as_of=AS_OF_V1)
    b = _run(db, name="人口v2", as_of=AS_OF_V2)
    comp = build_comparison(db, a["task_id"], b["task_id"])
    g6 = next(g for g in comp["per_grid"] if g["grid_id"] == "G6")
    assert g6["classification"] == "population_revised"
    assert g6["population_a"] == 1000.0 and g6["population_b"] == 1200.0
    revised = {e["grid_id"]: e for e in comp["attribution"]["population_revision_effect"]}
    assert revised["G6"]["population_delta"] == 200.0
    # 道路效应不包含 G6 的分母变化
    road_grids = {g["grid_id"] for g in comp["attribution"]["newly_covered_grids"]}
    assert "G6" not in road_grids
    assert any("人口" in w for w in comp["warnings"])


def test_boundary_change_listed_not_silently_rebased(fresh_db):
    db = fresh_db
    full = _run(db, name="全量边界")
    ext = _run(db, name="扩展边界", boundary="B_EXT")
    comp = build_comparison(db, full["task_id"], ext["task_id"])
    # G7 在两侧都存在（B_EXT 只有 G7），其余主城区网格仅在全量侧
    assert "G7" not in comp["attribution"]["boundary_removed_grids"]
    assert "G1" in comp["attribution"]["boundary_removed_grids"]
    assert any("边界" in w for w in comp["warnings"])
    # 共同分母只含两侧共有的网格
    assert comp["common_denominator"]["grid_count"] == 1


def test_rate_delta_uses_common_denominator_only(fresh_db):
    db = fresh_db
    a = _run(db, name="率A", as_of=AS_OF_V1)
    b = _run(db, name="率B", as_of=AS_OF_V2)
    comp = build_comparison(db, a["task_id"], b["task_id"])
    cd = comp["common_denominator"]
    # 手工关系：率 = 共同覆盖 / 共同分母，绝不引用单侧总量（比率落库保留 6 位小数）
    assert abs(cd["coverage_rate_a"] - cd["covered_a"] / cd["population_a"]) < 1e-6
    assert abs(cd["coverage_rate_b"] - cd["covered_b"] / cd["population_b"]) < 1e-6
    assert abs(cd["rate_delta"] - (cd["coverage_rate_b"] - cd["coverage_rate_a"])) <= 1e-6


def test_publication_records_full_input_manifest(fresh_db):
    db = fresh_db
    a = _run(db, name="发布A", cutoff=C1)
    apply_c2_revision(db)
    b = _run(db, name="发布B", cutoff=C2)
    out = publish_comparison(db, a["task_id"], b["task_id"], "暴雨批次比较", "analyst")
    manifest = out["input_manifest"]
    assert manifest["scenario_a"]["signature"] != manifest["scenario_b"]["signature"]
    assert set(manifest["scenario_a"]) >= {
        "population_as_of", "departure_time", "max_trip_minutes", "road_cutoff",
        "graph_version_id", "edge_fingerprint", "event_fingerprint",
        "grid_populations", "grid_boundaries",
    }
    assert out["publication"]["evidence_cutoff_at"] == C2
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM publications")
        assert cur.fetchone()["n"] == 1
