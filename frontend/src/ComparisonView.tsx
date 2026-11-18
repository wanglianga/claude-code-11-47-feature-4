import { useEffect, useState } from "react";
import { api } from "./api";
import type { Comparison, ExposureEstimateListItem, ScenarioListRow } from "./types";

export default function ComparisonView({
  pushToast,
}: {
  pushToast: (t: string, k?: "info" | "error") => void;
}) {
  const [scenarios, setScenarios] = useState<ScenarioListRow[]>([]);
  const [taskA, setTaskA] = useState("");
  const [taskB, setTaskB] = useState("");
  const [comp, setComp] = useState<Comparison | null>(null);
  const [title, setTitle] = useState("暴雨批次比较");
  const [estimates, setEstimates] = useState<ExposureEstimateListItem[]>([]);
  const [estimateId, setEstimateId] = useState("");

  const refresh = () =>
    api.listScenarios().then((r) => setScenarios(r.scenarios));

  useEffect(() => {
    refresh().catch((e) => pushToast(String(e), "error"));
    api.listExposureEstimates().then((r) => setEstimates(r.estimates)).catch(() => undefined);
  }, []);

  const run = async () => {
    if (!taskA || !taskB) {
      pushToast("请先选择两侧情景任务", "error");
      return;
    }
    try {
      setComp(await api.compare(taskA, taskB));
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  const publish = async () => {
    try {
      const out = await api.publish(taskA, taskB, title, estimateId || null);
      pushToast(`已发布 ${out.publication.publication_id}，完整输入清单已落库`);
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  return (
    <div className="full-page">
      <div className="card">
        <h3>建立比较：相同人口、边界与时间口径的共同分母</h3>
        <div className="row">
          <label className="field">
            <span>情景 A（基准）</span>
            <select value={taskA} onChange={(e) => setTaskA(e.target.value)}>
              <option value="">— 选择任务 —</option>
              {scenarios.filter((s) => s.task_id).map((s) => (
                <option key={s.task_id!} value={s.task_id!}>
                  {s.name}｜{s.departure_time.slice(11, 16)} 发｜时限{s.max_trip_minutes}min｜截止{s.road_cutoff.slice(11, 16)}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>情景 B（对照）</span>
            <select value={taskB} onChange={(e) => setTaskB(e.target.value)}>
              <option value="">— 选择任务 —</option>
              {scenarios.filter((s) => s.task_id).map((s) => (
                <option key={s.task_id!} value={s.task_id!}>
                  {s.name}｜{s.departure_time.slice(11, 16)} 发｜时限{s.max_trip_minutes}min｜截止{s.road_cutoff.slice(11, 16)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="row">
          <button className="btn" onClick={run}>按共同分母比较</button>
          <input
            placeholder="发布标题"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <select value={estimateId} onChange={(e) => setEstimateId(e.target.value)}>
            <option value="">不关联临时暴露版本</option>
            {estimates.map((e) => (
              <option key={e.estimate_id} value={e.estimate_id}>
                关联 v{e.estimate_version} 区间（{e.window_start.slice(11, 16)}–
                {e.window_end.slice(11, 16)}，{e.grid_count}格）
              </option>
            ))}
          </select>
          <button className="btn secondary" onClick={publish} disabled={!comp}>
            发布并固化完整输入清单
          </button>
        </div>
        <div className="small muted" style={{ marginTop: 6 }}>
          关联临时暴露估计后，其输入快照指纹与区间汇总会一并固化到发布清单，旧预案保留当时分母。
        </div>
      </div>

      {comp && (
        <>
          {comp.warnings.length > 0 && (
            <div className="card" style={{ borderColor: "var(--uncovered)" }}>
              <h3>口径警告（不能把换分母的覆盖率当作改善）</h3>
              <ul className="tight">
                {comp.warnings.map((w) => <li key={w} className="warn">{w}</li>)}
              </ul>
              <div className="small muted">
                口径检查：同人口基准 {String(comp.basis_checks.same_population_basis)} ｜
                同时限 {String(comp.basis_checks.same_deadline)} ｜
                同道路截止点 {String(comp.basis_checks.same_road_cutoff)}
              </div>
            </div>
          )}

          <div className="card">
            <h3>共同分母与覆盖率（仅双侧共有网格）</h3>
            <div className="stat-grid">
              <div className="stat"><div className="v">{comp.common_denominator.grid_count}</div><div className="k">共同网格数</div></div>
              <div className="stat"><div className="v">{comp.common_denominator.population_a} → {comp.common_denominator.population_b}</div><div className="k">共同分母人口 A → B</div></div>
              <div className="stat"><div className="v">{(comp.common_denominator.coverage_rate_a * 100).toFixed(2)}% → {(comp.common_denominator.coverage_rate_b * 100).toFixed(2)}%</div><div className="k">共同口径覆盖率</div></div>
              <div className="stat"><div className="v">{(comp.common_denominator.rate_delta * 100).toFixed(2)}pp</div><div className="k">覆盖率变化</div></div>
              <div className="stat"><div className="v">{comp.common_denominator.unknown_a} → {comp.common_denominator.unknown_b}</div><div className="k">未知人口 A → B（不解释为零需求）</div></div>
              <div className="stat good"><div className="v">{comp.attribution.road_or_open_window_effect_population}</div><div className="k">道路/开放条件净效应（人）</div></div>
            </div>
          </div>

          <div className="card">
            <h3>变化归因（三类原因分别展示）</h3>
            <table>
              <thead>
                <tr><th>归因桶</th><th>网格</th><th>说明</th></tr>
              </thead>
              <tbody>
                <tr>
                  <td><span className="pill covered">道路恢复/开放改善</span></td>
                  <td>{comp.attribution.newly_covered_grids.map((g) => <span key={g.grid_id} className="path-chip hl">{g.grid_id}</span>)}</td>
                  <td className="small muted">同分母下由未覆盖变覆盖；可下钻查看恢复后的见证路径</td>
                </tr>
                <tr>
                  <td><span className="pill uncovered">覆盖丢失</span></td>
                  <td>{comp.attribution.lost_coverage_grids.map((g) => <span key={g.grid_id} className="path-chip">{g.grid_id}</span>) || "—"}</td>
                  <td className="small muted">新封路或开放窗口收紧导致</td>
                </tr>
                <tr>
                  <td><span className="pill unknown">人口资料修订</span></td>
                  <td>{comp.attribution.population_revision_effect.map((g) => <span key={g.grid_id} className="path-chip">{g.grid_id}（Δ{g.population_delta}）</span>) || "—"}</td>
                  <td className="small muted">分母变化，不计入道路效应</td>
                </tr>
                <tr>
                  <td>分析边界变化</td>
                  <td>新增 {comp.attribution.boundary_added_grids.join(", ") || "—"}；移除 {comp.attribution.boundary_removed_grids.join(", ") || "—"}</td>
                  <td className="small muted">仅单侧存在的网格不进入共同分母</td>
                </tr>
                <tr>
                  <td>未知份额变化</td>
                  <td>{comp.attribution.unknown_changed_grids.map((g) => <span key={g.grid_id} className="path-chip">{g.grid_id}</span>) || "—"}</td>
                  <td className="small muted">连接/分配依据变化，保持单列</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div className="card">
            <h3>逐网格明细（可核对排除原因与安置点变化）</h3>
            <div className="scroll-x">
              <table>
                <thead>
                  <tr>
                    <th>网格</th><th>分类</th><th>人口 A→B</th><th>覆盖 A→B</th><th>未知 A→B</th>
                    <th>安置点 A→B</th><th>B 侧原因</th>
                  </tr>
                </thead>
                <tbody>
                  {comp.per_grid.map((g) => (
                    <tr key={g.grid_id}>
                      <td className="mono">{g.grid_id}</td>
                      <td><span className={`pill ${g.classification === "newly_covered" ? "covered" : g.classification === "lost_coverage" ? "uncovered" : g.classification === "population_revised" ? "unknown" : "done"}`}>{g.classification}</span></td>
                      <td>{g.population_a} → {g.population_b}</td>
                      <td>{g.covered_pop_a} → {g.covered_pop_b}</td>
                      <td>{g.unknown_pop_a} → {g.unknown_pop_b}</td>
                      <td className="small">{g.shelters_a.join(",") || "—"} → {g.shelters_b.join(",") || "—"}</td>
                      <td className="small warn">{g.reasons_b.join("；") || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
