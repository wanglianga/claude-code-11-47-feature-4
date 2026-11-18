import { useEffect, useState } from "react";
import { api } from "./api";
import type { CacheRow, TaskRow } from "./types";

const C1 = "2026-07-15T07:30:00+00:00";
const C2 = "2026-07-15T08:45:00+00:00";
const T0 = "2026-07-15T08:00:00+00:00";

export default function CacheView({
  pushToast,
}: {
  pushToast: (t: string, k?: "info" | "error") => void;
}) {
  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [cache, setCache] = useState<CacheRow[]>([]);
  const [versions, setVersions] = useState<Array<{
    version_id: number; cutoff_at: string; edge_fingerprint: string; event_fingerprint: string; note: string | null;
  }>>([]);
  const [eventForm, setEventForm] = useState({
    event_id: `EVT-${Math.floor(100 + Math.random() * 900)}`,
    event_type: "closure",
    edge_id: "E6",
    effective_from: T0,
    effective_to: "",
    speed_limit_kmh: "",
  });

  const refresh = async () => {
    try {
      const [t, c, v] = await Promise.all([
        api.listTasks(),
        api.cacheIndex(),
        api.networkVersions(),
      ]);
      setTasks(t.tasks);
      setCache(c.cache_index);
      setVersions(v.versions);
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const applyC2 = async () => {
    try {
      await api.applyC2();
      pushToast("C2 修订已录入：E7 重开 08:40，E2 新封路 08:15-08:35");
      await refresh();
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  const createEvent = async () => {
    try {
      const payload: Record<string, unknown> = {
        ...eventForm,
        effective_to: eventForm.effective_to || null,
        speed_limit_kmh: eventForm.speed_limit_kmh ? Number(eventForm.speed_limit_kmh) : null,
      };
      const out = await api.createEvent(payload);
      pushToast(
        `临时事件已登记并即时参与计算；失效任务 ${out.invalidation.staled_total} 个（含恢复候选 ${out.invalidation.recovery_candidate_task_ids?.length ?? 0} 个）`
      );
      await refresh();
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  const precompute = async () => {
    try {
      const r = await api.precompute({
        population_as_of: "2026-06-01",
        base_day: T0,
        cutoff: C1,
        deadline_minutes: 30,
        offsets_minutes: [0, 30, 60, 90, 120],
      });
      pushToast(`预计算完成 ${r.precomputed.length} 个常规窗口`);
      await refresh();
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  const rebuild = async () => {
    try {
      const r = await api.rebuild(C2);
      pushToast(`重建完成：${r.rebuilt_count} 个失效窗口改用 C2 新版本`);
      await refresh();
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  return (
    <div className="full-page">
      <div className="card">
        <h3>混合计算控制：常规窗口预计算 + 临时事件即时计算</h3>
        <div className="row">
          <button className="btn secondary" onClick={precompute}>预计算 5 个常规窗口（C1）</button>
          <button className="btn secondary" onClick={applyC2}>应用 C2 道路修订（重开+新封路）</button>
          <button className="btn secondary" onClick={rebuild}>按 C2 重建失效预计算窗口</button>
          <button className="btn secondary" onClick={refresh}>刷新</button>
        </div>
        <div className="small muted" style={{ marginTop: 8 }}>
          缓存键是完整情景签名（人口基准日+出发时刻+时限+路网版本+分析边界）；不同出发时刻或开放条件即使命中缓存也不会互相复用覆盖面。
        </div>
      </div>

      <div className="card">
        <h3>临时封路 / 限速登记（received_at=now，立即参与下一次即时计算）</h3>
        <div className="row">
          <label className="field"><span>事件号</span><input value={eventForm.event_id} onChange={(e) => setEventForm({ ...eventForm, event_id: e.target.value })} /></label>
          <label className="field"><span>类型</span>
            <select value={eventForm.event_type} onChange={(e) => setEventForm({ ...eventForm, event_type: e.target.value })}>
              <option value="closure">closure 封路</option>
              <option value="speed_limit">speed_limit 限速</option>
            </select>
          </label>
          <label className="field"><span>影响边</span><input value={eventForm.edge_id} onChange={(e) => setEventForm({ ...eventForm, edge_id: e.target.value })} /></label>
        </div>
        <div className="row">
          <label className="field"><span>生效起</span><input value={eventForm.effective_from} onChange={(e) => setEventForm({ ...eventForm, effective_from: e.target.value })} /></label>
          <label className="field"><span>生效止（空=未恢复）</span><input value={eventForm.effective_to} onChange={(e) => setEventForm({ ...eventForm, effective_to: e.target.value })} /></label>
          <label className="field"><span>限速 km/h（仅限速）</span><input value={eventForm.speed_limit_kmh} onChange={(e) => setEventForm({ ...eventForm, speed_limit_kmh: e.target.value })} /></label>
        </div>
        <button className="btn danger" onClick={createEvent}>登记并沿依赖边失效</button>
      </div>

      <div className="card scroll-x">
        <h3>路网版本与指纹（旧任务不能覆盖新版本索引）</h3>
        <table>
          <thead><tr><th>版本</th><th>资料截止点</th><th>边指纹</th><th>事件指纹</th><th>说明</th></tr></thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.version_id}>
                <td>v{v.version_id}</td>
                <td className="small">{v.cutoff_at.replace("T", " ").slice(0, 19)}</td>
                <td className="mono small">{v.edge_fingerprint}</td>
                <td className="mono small">{v.event_fingerprint}</td>
                <td className="small muted">{v.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card scroll-x">
        <h3>计算任务（引用计数 / 失效标记 / 证据截止）</h3>
        <table>
          <thead><tr><th>任务</th><th>情景</th><th>类型</th><th>状态</th><th>查看者</th><th>失效</th><th>证据截止</th><th>签名</th></tr></thead>
          <tbody>
            {tasks.map((t) => (
              <tr key={t.task_id}>
                <td className="mono small">{t.task_id.slice(0, 8)}</td>
                <td>{t.scenario_name}</td>
                <td>{t.kind}</td>
                <td><span className={`pill ${t.status === "done" ? "done" : "uncovered"}`}>{t.status}</span></td>
                <td>{t.ref_count}</td>
                <td>{t.stale ? <span className="pill stale">stale</span> : "fresh"}</td>
                <td className="small">{t.evidence_cutoff_at.replace("T", " ").slice(0, 16)}</td>
                <td className="mono small">{t.signature.slice(0, 16)}…</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card scroll-x">
        <h3>缓存索引（每项标明证据截止时间；stale 行不再被命中）</h3>
        <table>
          <thead><tr><th>签名</th><th>任务</th><th>版本</th><th>出发时刻</th><th>时限</th><th>证据截止</th><th>状态</th><th>计算时间</th></tr></thead>
          <tbody>
            {cache.map((c) => (
              <tr key={c.signature}>
                <td className="mono small">{c.signature}</td>
                <td className="mono small">{c.task_id.slice(0, 8)}</td>
                <td>v{c.graph_version_id}</td>
                <td className="small">{c.departure_time.replace("T", " ").slice(11, 16)}</td>
                <td>{c.max_trip_minutes}min</td>
                <td className="small">{c.evidence_cutoff_at.replace("T", " ").slice(0, 16)}</td>
                <td>{c.stale ? <span className="pill stale">stale</span> : <span className="pill covered">usable</span>}</td>
                <td className="small muted">{c.computed_at.replace("T", " ").slice(0, 16)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
