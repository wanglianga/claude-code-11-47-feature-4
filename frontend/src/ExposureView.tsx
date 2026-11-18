import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import type {
  ExposureEstimateDetail,
  ExposureEstimateListItem,
  ExposureEvidence,
  ExposurePeriod,
  ExposureSources,
} from "./types";

const W1_START = "2026-07-15T09:00:00+00:00";
const W1_END = "2026-07-15T10:00:00+00:00";
const EX_C1 = "2026-07-15T08:55:00+00:00";
const EX_C2 = "2026-07-15T09:55:00+00:00";

const fmt = (n: number | null | undefined, digits = 0) =>
  n === null || n === undefined ? "不可估" : Number(n).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });

const modeLabel: Record<string, string> = {
  calibrated: "校准",
  conservative: "保守",
  mixed: "混合",
};

export default function ExposureView({
  pushToast,
}: {
  pushToast: (t: string, k?: "info" | "error") => void;
}) {
  const [tab, setTab] = useState<"estimate" | "register" | "browse">("estimate");
  return (
    <div className="full-page">
      <div className="card">
        <h3>临时暴露人口区间（网格 × 时段）—— 叠加于冻结常住人口基线之上</h3>
        <div className="small muted">
          校准路径：覆盖率稳定且现场核验的移动信号按分层系数扩展并给抽样误差；
          保守路径：已核实人数作下界、容量/登记约束上界。跨来源重叠无法识别绝不相加；
          隐私抑制与覆盖下降只扩大未知范围，不记零、不做比例外推。
        </div>
        <nav className="tabs" style={{ borderBottom: "none", padding: 0, marginTop: 8 }}>
          {([
            ["estimate", "区间估计与基线叠加"],
            ["register", "临时来源登记"],
            ["browse", "来源覆盖浏览"],
          ] as const).map(([k, label]) => (
            <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>
              {label}
            </button>
          ))}
        </nav>
      </div>
      {tab === "estimate" && <EstimatePane pushToast={pushToast} />}
      {tab === "register" && <RegisterPane pushToast={pushToast} />}
      {tab === "browse" && <BrowsePane />}
    </div>
  );
}

// ============================================================ 区间估计主面板 --
function EstimatePane({
  pushToast,
}: {
  pushToast: (t: string, k?: "info" | "error") => void;
}) {
  const [form, setForm] = useState({
    window_start: W1_START,
    window_end: W1_END,
    evidence_cutoff: EX_C1,
    privacy_threshold: 10,
    baseline_pop_as_of: "2026-06-01",
    boundary_id: "",
    reachable_task_id: "",
  });
  const [versions, setVersions] = useState<ExposureEstimateListItem[]>([]);
  const [detail, setDetail] = useState<ExposureEstimateDetail | null>(null);
  const [selectedGrid, setSelectedGrid] = useState<string | null>(null);
  const [drillEvidence, setDrillEvidence] = useState<ExposureEvidence[]>([]);
  const [loading, setLoading] = useState(false);

  const refreshList = () =>
    api.listExposureEstimates().then((r) => setVersions(r.estimates)).catch(() => undefined);

  useEffect(() => {
    refreshList();
  }, []);

  const run = async () => {
    setLoading(true);
    try {
      const d = await api.createExposureEstimate({
        ...form,
        boundary_id: form.boundary_id || null,
        reachable_task_id: form.reachable_task_id || null,
        created_by: "analyst-ui",
      });
      setDetail(d);
      setSelectedGrid(null);
      pushToast(`已生成估计版本 v${d.estimate.estimate_version}（旧版本保留）`);
      await refreshList();
    } catch (e) {
      pushToast(String(e), "error");
    } finally {
      setLoading(false);
    }
  };

  const openVersion = async (id: string) => {
    try {
      const d = await api.exposureEstimate(id);
      setDetail(d);
      setSelectedGrid(null);
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  const drill = async (gridId: string) => {
    if (!detail) return;
    setSelectedGrid(gridId);
    try {
      const r = await api.exposureGridDrilldown(detail.estimate.estimate_id, gridId);
      setDrillEvidence(r.evidence);
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  const totals = detail?.totals;
  const periods = useMemo(
    () =>
      (detail?.periods ?? []).filter(
        (p) =>
          p.increase_lower > 0 ||
          p.increase_upper !== null ||
          p.decrease_lower > 0 ||
          p.point_unknown > 0
      ),
    [detail]
  );

  return (
    <>
      <div className="card">
        <h3>估计输入（时间窗对齐 + 证据截止 + 隐私阈值）</h3>
        <div className="row">
          <label className="field">
            <span>时段开始（UTC）</span>
            <input
              type="datetime-local"
              value={form.window_start.slice(0, 16)}
              onChange={(e) => setForm({ ...form, window_start: e.target.value + ":00+00:00" })}
            />
          </label>
          <label className="field">
            <span>时段结束（UTC）</span>
            <input
              type="datetime-local"
              value={form.window_end.slice(0, 16)}
              onChange={(e) => setForm({ ...form, window_end: e.target.value + ":00+00:00" })}
            />
          </label>
        </div>
        <div className="row">
          <label className="field">
            <span>证据截止点</span>
            <select
              value={form.evidence_cutoff}
              onChange={(e) => setForm({ ...form, evidence_cutoff: e.target.value })}
            >
              <option value={EX_C1}>E1 08:55（首轮，晚到转移登记未到达）</option>
              <option value={EX_C2}>E2 09:55（晚到登记到达后，产生新版本）</option>
            </select>
          </label>
          <label className="field">
            <span>隐私抑制阈值（人）</span>
            <input
              type="number"
              value={form.privacy_threshold}
              onChange={(e) => setForm({ ...form, privacy_threshold: Number(e.target.value) })}
            />
          </label>
        </div>
        <div className="row">
          <label className="field">
            <span>常住人口基准日</span>
            <input
              type="date"
              value={form.baseline_pop_as_of}
              onChange={(e) => setForm({ ...form, baseline_pop_as_of: e.target.value })}
            />
          </label>
          <label className="field">
            <span>分析边界</span>
            <select
              value={form.boundary_id}
              onChange={(e) => setForm({ ...form, boundary_id: e.target.value })}
            >
              <option value="">全部边界</option>
              <option value="B_MAIN">B_MAIN 主城区</option>
              <option value="B_EXT">B_EXT 扩展区</option>
            </select>
          </label>
        </div>
        <label className="field">
          <span>叠加可达基线任务 task_id（可选，用于可达需求区间）</span>
          <input
            placeholder="留空则只输出基线人口 + 净变化区间"
            value={form.reachable_task_id}
            onChange={(e) => setForm({ ...form, reachable_task_id: e.target.value })}
          />
        </label>
        <button className="btn" onClick={run} disabled={loading}>
          {loading ? "计算中…" : "生成临时暴露人口估计版本"}
        </button>
        {versions.length > 0 && (
          <div className="small muted" style={{ marginTop: 8 }}>
            历史版本（点击回看，旧预案保留当时分母）：
            {versions.map((v) => (
              <button
                key={v.estimate_id}
                className="path-chip"
                style={{ marginLeft: 6, cursor: "pointer" }}
                onClick={() => openVersion(v.estimate_id)}
              >
                v{v.estimate_version} · {v.evidence_cutoff_at.slice(11, 16)} · {v.grid_count}格
              </button>
            ))}
          </div>
        )}
      </div>

      {detail && totals && (
        <div className="card">
          <h3>
            估计版本 v{detail.estimate.estimate_version} ｜ 窗口{" "}
            {detail.estimate.window_start.slice(11, 16)}–
            {detail.estimate.window_end.slice(11, 16)} ｜ 截止{" "}
            {detail.estimate.evidence_cutoff_at.slice(11, 16)}
          </h3>
          <div className="stat-grid">
            <div className="stat">
              <div className="v">{fmt(totals.baseline_total)}</div>
              <div className="k">常住人口基线（固定分母）</div>
            </div>
            <div className="stat good">
              <div className="v">
                {fmt(totals.increase_lower_total)} ~ {fmt(totals.increase_upper_total)}
              </div>
              <div className="k">临时增加区间（人）</div>
            </div>
            <div className="stat warn">
              <div className="v">{fmt(totals.decrease_lower_total)}</div>
              <div className="k">临时减少（转移登记，确定值）</div>
            </div>
            <div className="stat">
              <div className="v">
                {fmt(totals.delta_lower_total)} ~ {fmt(totals.delta_upper_total)}
              </div>
              <div className="k">净变化区间（人）</div>
            </div>
            <div className="stat">
              <div className="v" style={{ color: "var(--unknown)" }}>
                {totals.point_unknown_sources}
              </div>
              <div className="k">点状未知来源（隐私抑制条数）</div>
            </div>
            <div className="stat">
              <div className="v" style={{ color: "var(--stale)" }}>
                {totals.open_upper_grid_count}
              </div>
              <div className="k">上界不可估计网格（覆盖缺口）</div>
            </div>
          </div>
          <div className="evidence" style={{ marginTop: 8 }}>{detail.estimate.method_note}</div>
        </div>
      )}

      {detail && (
        <div className="card scroll-x">
          <h3>网格 × 时段区间（点击行查看逐来源采用证据）</h3>
          <table>
            <thead>
              <tr>
                <th>网格</th>
                <th>常住基线</th>
                <th>临时增加 [下界, 上界]</th>
                <th>临时减少</th>
                <th>净变化 [下界, 上界]</th>
                <th>未知范围</th>
                <th>方法</th>
                <th>叠加后可达需求</th>
              </tr>
            </thead>
            <tbody>
              {periods.map((p) => (
                <PeriodRow
                  key={p.period_id}
                  p={p}
                  selected={selectedGrid === p.grid_id}
                  onSelect={() => drill(p.grid_id)}
                />
              ))}
            </tbody>
          </table>
          <div className="small muted" style={{ marginTop: 6 }}>
            仅展示存在临时来源或未知项的网格；无任何信号的网格保持确定区间 [0,0]，不虚构未知人数。
          </div>
        </div>
      )}

      {selectedGrid && (
        <div className="card scroll-x">
          <h3>下钻：{selectedGrid} 的逐来源采用证据</h3>
          <table>
            <thead>
              <tr>
                <th>来源</th>
                <th>来源行</th>
                <th>原始观测</th>
                <th>采用 [下界, 上界]</th>
                <th>覆盖率</th>
                <th>校准</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {drillEvidence.map((e) => (
                <tr key={e.evidence_id}>
                  <td className="mono">{e.source_kind}</td>
                  <td className="small mono">{e.source_ref}</td>
                  <td>
                    {e.suppressed ? (
                      <span className="pill unknown">已抑制</span>
                    ) : (
                      fmt(e.observed_count)
                    )}
                  </td>
                  <td>
                    {fmt(e.adopted_lower)} ~ {fmt(e.adopted_upper)}
                  </td>
                  <td>{e.coverage_value ? `${(e.coverage_value * 100).toFixed(0)}%` : "—"}</td>
                  <td>{e.calibrated ? <span className="pill covered">是</span> : "—"}</td>
                  <td className="small muted">{e.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function PeriodRow({
  p,
  selected,
  onSelect,
}: {
  p: ExposurePeriod;
  selected: boolean;
  onSelect: () => void;
}) {
  const unknownText =
    p.increase_upper === null
      ? "上界不可估"
      : `结构未知 ${fmt(p.interval_unknown)}${p.point_unknown ? `｜抑制 ${p.point_unknown} 条` : ""}`;
  return (
    <tr
      onClick={onSelect}
      style={{ cursor: "pointer", background: selected ? "rgba(77,163,255,.12)" : undefined }}
    >
      <td className="mono">{p.grid_id}</td>
      <td>{fmt(p.baseline_population)}</td>
      <td className={p.increase_upper === null ? "warn" : "good"}>
        {fmt(p.increase_lower)} ~ {fmt(p.increase_upper)}
      </td>
      <td className="warn">{fmt(p.decrease_lower)}</td>
      <td className={p.delta_upper === null ? "warn" : ""}>
        {fmt(p.delta_lower)} ~ {fmt(p.delta_upper)}
      </td>
      <td className="small">
        {unknownText}
        {p.sampling_margin > 0 && (
          <div className="small muted">抽样误差半宽 ±{fmt(p.sampling_margin)}</div>
        )}
      </td>
      <td>
        <span className={`pill ${p.method_mode === "calibrated" ? "covered" : "done"}`}>
          {modeLabel[p.method_mode]}
        </span>
      </td>
      <td>
        {p.demand_lower === null ? (
          <span className="small muted">未关联覆盖任务</span>
        ) : (
          <>
            {fmt(p.demand_lower)} ~ {fmt(p.demand_upper)}
            <div className="small muted">基线可达 {fmt(p.reachable_baseline)}</div>
          </>
        )}
      </td>
    </tr>
  );
}

// ============================================================ 临时来源登记 ----
function RegisterPane({
  pushToast,
}: {
  pushToast: (t: string, k?: "info" | "error") => void;
}) {
  const [kind, setKind] = useState<"mobile" | "venue" | "transfer" | "overlap">("mobile");
  const [grid, setGrid] = useState("G8");
  const [count, setCount] = useState(50);
  const [suppressed, setSuppressed] = useState(false);
  const [coverage, setCoverage] = useState(0.3);
  const [stable, setStable] = useState(false);
  const [verified, setVerified] = useState(false);
  const [late, setLate] = useState(false);

  const commonWindow = { window_start: W1_START, window_end: W1_END };

  const submit = async () => {
    try {
      if (kind === "mobile") {
        await api.registerMobile({
          operator_id: "OP-UI",
          grid_id: grid,
          stratum: "visitor",
          ...commonWindow,
          sample_count: suppressed ? null : count,
          suppressed,
          operator_coverage: coverage,
          coverage_stable: stable,
          field_verified: verified,
          source_title: "界面登记移动信号",
        });
      } else if (kind === "venue") {
        await api.registerVenue({
          venue_id: `V-UI-${grid}`,
          venue_name: "界面登记场所",
          presence_kind: "scenic",
          grid_id: grid,
          ...commonWindow,
          verified_count: suppressed ? null : count,
          privacy_suppressed: suppressed,
        });
      } else if (kind === "transfer") {
        await api.registerTransfer({
          grid_id: grid,
          ...commonWindow,
          registered_persons: count,
          late_arrival: late,
        });
      } else {
        await api.registerOverlapRule({
          grid_id: grid,
          source_a: "mobile",
          source_b: "scenic",
          relation: "disjoint",
          evidence_note: "界面登记的可证明互斥关系",
        });
      }
      pushToast("来源已登记（received_at=now，需用不早于当前的截止点重新估计）");
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  return (
    <div className="card">
      <h3>临时来源登记（登记即落来源目录，可追溯原文与登记时间）</h3>
      <nav className="tabs" style={{ borderBottom: "none", padding: 0 }}>
        {([
          ["mobile", "移动信号"],
          ["venue", "场所/学校"],
          ["transfer", "转移登记"],
          ["overlap", "重叠规则"],
        ] as const).map(([k, label]) => (
          <button key={k} className={kind === k ? "active" : ""} onClick={() => setKind(k)}>
            {label}
          </button>
        ))}
      </nav>
      <div className="row">
        <label className="field">
          <span>网格</span>
          <input value={grid} onChange={(e) => setGrid(e.target.value)} />
        </label>
        {kind !== "overlap" && (
          <label className="field">
            <span>人数（抑制时忽略）</span>
            <input
              type="number"
              value={count}
              onChange={(e) => setCount(Number(e.target.value))}
            />
          </label>
        )}
      </div>
      {kind === "mobile" && (
        <div className="row">
          <label className="field">
            <span>运营商覆盖率</span>
            <input
              type="number"
              step="0.05"
              value={coverage}
              onChange={(e) => setCoverage(Number(e.target.value))}
            />
          </label>
          <label className="field">
            <span>开关</span>
            <div className="small">
              <label>
                <input type="checkbox" checked={suppressed} onChange={(e) => setSuppressed(e.target.checked)} /> 隐私抑制
              </label>
              <label style={{ marginLeft: 8 }}>
                <input type="checkbox" checked={stable} onChange={(e) => setStable(e.target.checked)} /> 覆盖稳定
              </label>
              <label style={{ marginLeft: 8 }}>
                <input type="checkbox" checked={verified} onChange={(e) => setVerified(e.target.checked)} /> 现场核验
              </label>
            </div>
          </label>
        </div>
      )}
      {kind === "venue" && (
        <label className="field">
          <span>隐私抑制（小样本签到不可见）</span>
          <input type="checkbox" checked={suppressed} onChange={(e) => setSuppressed(e.target.checked)} />
        </label>
      )}
      {kind === "transfer" && (
        <label className="field">
          <span>晚到登记（只进入新估计版本）</span>
          <input type="checkbox" checked={late} onChange={(e) => setLate(e.target.checked)} />
        </label>
      )}
      <button className="btn" onClick={submit}>登记来源</button>
      <div className="small muted" style={{ marginTop: 6 }}>
        演示要点：取消“覆盖稳定/现场核验”后，移动信号只能作保守下界且上界不可估；
        勾选“隐私抑制”时不得填写人数，接口会拒绝。
      </div>
    </div>
  );
}

// ============================================================ 来源覆盖浏览 ----
function BrowsePane() {
  const [sources, setSources] = useState<ExposureSources | null>(null);
  useEffect(() => {
    api.exposureSources().then(setSources).catch(() => undefined);
  }, []);
  if (!sources) return <div className="card">加载中…</div>;
  const boolPill = (v: unknown) =>
    v ? <span className="pill covered">是</span> : <span className="pill stale">否</span>;
  return (
    <>
      <div className="card scroll-x">
        <h3>移动信号采样与覆盖率（含覆盖下降、抑制标记）</h3>
        <table>
          <thead>
            <tr>
              <th>#</th><th>运营商</th><th>网格</th><th>分层</th><th>采样人数</th>
              <th>覆盖率</th><th>稳定</th><th>核验</th><th>登记时间</th>
            </tr>
          </thead>
          <tbody>
            {sources.mobile_signals.map((s) => (
              <tr key={String(s.sample_id)}>
                <td>{String(s.sample_id)}</td>
                <td className="mono">{String(s.operator_id)}</td>
                <td className="mono">{String(s.grid_id)}</td>
                <td>{String(s.stratum)}</td>
                <td>{s.suppressed ? <span className="pill unknown">抑制</span> : fmt(Number(s.sample_count))}</td>
                <td className={s.coverage_stable ? "" : "warn"}>
                  {(Number(s.operator_coverage) * 100).toFixed(0)}%
                </td>
                <td>{boolPill(s.coverage_stable)}</td>
                <td>{boolPill(s.field_verified)}</td>
                <td className="small">{String(s.received_at).slice(0, 16)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="card scroll-x">
        <h3>场所/学校在场、容量与转移登记</h3>
        <table>
          <thead>
            <tr><th>场所</th><th>类型</th><th>网格</th><th>已核实</th><th>容量</th><th>抑制</th></tr>
          </thead>
          <tbody>
            {sources.venue_presence.map((v) => {
              const cap = sources.venue_capacity.find((c) => c.venue_id === v.venue_id);
              return (
                <tr key={String(v.presence_id)}>
                  <td>{String(v.venue_name)}</td>
                  <td>{v.presence_kind === "scenic" ? "景区" : "寄宿学校"}</td>
                  <td className="mono">{String(v.grid_id)}</td>
                  <td>{v.privacy_suppressed ? <span className="pill unknown">抑制</span> : fmt(Number(v.verified_count))}</td>
                  <td>{cap ? fmt(Number(cap.capacity_persons)) : "—"}</td>
                  <td>{boolPill(v.privacy_suppressed)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <table style={{ marginTop: 10 }}>
          <thead>
            <tr><th>#</th><th>网格</th><th>登记人数</th><th>去向</th><th>晚到</th><th>登记时间</th></tr>
          </thead>
          <tbody>
            {sources.transfers.map((t) => (
              <tr key={String(t.transfer_id)}>
                <td>{String(t.transfer_id)}</td>
                <td className="mono">{String(t.grid_id)}</td>
                <td>{fmt(Number(t.registered_persons))}</td>
                <td>{t.destination_grid_id ? String(t.destination_grid_id) : "区外/未知"}</td>
                <td>{boolPill(t.late_arrival)}</td>
                <td className="small">{String(t.received_at).slice(0, 16)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="card scroll-x">
        <h3>分层校准系数与可证明重叠关系</h3>
        <table>
          <thead>
            <tr><th>运营商</th><th>网格</th><th>系数</th><th>相对抽样误差</th><th>覆盖率基准</th><th>核验</th></tr>
          </thead>
          <tbody>
            {sources.calibrations.map((c) => (
              <tr key={String(c.calibration_id)}>
                <td className="mono">{String(c.operator_id)}</td>
                <td className="mono">{String(c.grid_id)}</td>
                <td>{String(c.calibration_factor)}</td>
                <td>±{(Number(c.sampling_rel_error) * 100).toFixed(0)}%</td>
                <td>{(Number(c.coverage_basis) * 100).toFixed(0)}%</td>
                <td>{boolPill(c.field_verified)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <table style={{ marginTop: 10 }}>
          <thead>
            <tr><th>网格</th><th>来源 A</th><th>来源 B</th><th>关系</th><th>重叠 [min, max]</th><th>核验依据</th></tr>
          </thead>
          <tbody>
            {sources.overlap_rules.map((r) => (
              <tr key={String(r.rule_id)}>
                <td className="mono">{String(r.grid_id)}</td>
                <td>{String(r.source_a)}</td>
                <td>{String(r.source_b)}</td>
                <td><span className="pill done">{String(r.relation)}</span></td>
                <td>
                  {fmt(Number(r.min_overlap_persons))} ~ {r.max_overlap_persons === null ? "∞" : fmt(Number(r.max_overlap_persons))}
                </td>
                <td className="small muted">{String(r.evidence_note)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
