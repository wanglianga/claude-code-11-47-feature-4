import { useEffect, useState } from "react";
import { api } from "./api";
import type {
  ConnectorRow,
  EdgeRevision,
  GridRow,
  NodeRow,
  OpenWindow,
  RoadEventRow,
  ShelterRow,
  SourceDoc,
} from "./types";

const TABS = ["documents", "events", "edges", "shelters", "population"] as const;

export default function SourcesView() {
  const [tab, setTab] = useState<(typeof TABS)[number]>("documents");
  const [docs, setDocs] = useState<SourceDoc[]>([]);
  const [events, setEvents] = useState<RoadEventRow[]>([]);
  const [audits, setAudits] = useState<Array<Record<string, unknown>>>([]);
  const [edges, setEdges] = useState<EdgeRevision[]>([]);
  const [nodes, setNodes] = useState<NodeRow[]>([]);
  const [shelters, setShelters] = useState<ShelterRow[]>([]);
  const [windows, setWindows] = useState<OpenWindow[]>([]);
  const [grids, setGrids] = useState<GridRow[]>([]);
  const [connectors, setConnectors] = useState<ConnectorRow[]>([]);
  const [asOf, setAsOf] = useState("2026-06-01");

  useEffect(() => {
    api.documents().then((r) => setDocs(r.documents)).catch(() => undefined);
    api.events(true).then((r) => {
      setEvents(r.events);
      setAudits(r.audits);
    }).catch(() => undefined);
    api.edges().then((r) => setEdges(r.edge_revisions)).catch(() => undefined);
    api.nodes().then((r) => setNodes(r.nodes)).catch(() => undefined);
    api.shelters().then((r) => {
      setShelters(r.shelters);
      setWindows(r.open_windows);
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    api.grids(asOf).then((r) => {
      setGrids(r.grids);
      setConnectors(r.connectors);
    }).catch(() => undefined);
  }, [asOf]);

  const nodeName = (id: string) => nodes.find((n) => n.node_id === id)?.name ?? id;

  return (
    <div className="full-page">
      <div className="card">
        <h3>来源目录：原文、单位、发生时间、登记时间、修订号与证据截止点</h3>
        <nav className="tabs" style={{ borderBottom: "none", padding: 0 }}>
          {TABS.map((t) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
              { { documents: "来源原文", events: "封路/限速与审计", edges: "有向边修订", shelters: "安置点与开放", population: "人口与连接" }[t] }
            </button>
          ))}
        </nav>
      </div>

      {tab === "documents" && (
        <div className="card scroll-x">
          <table>
            <thead>
              <tr><th>#</th><th>类型</th><th>标题</th><th>单位</th><th>发生时间</th><th>登记时间</th><th>证据截止</th><th>修订</th><th>原文摘录</th></tr>
            </thead>
            <tbody>
              {docs.map((d) => (
                <tr key={d.doc_id}>
                  <td>{d.doc_id}</td>
                  <td className="mono">{d.dataset_type}</td>
                  <td>{d.title}</td>
                  <td className="small muted">{d.unit_note ?? "—"}</td>
                  <td className="small">{d.source_occurred_at?.replace("T", " ").slice(0, 16) ?? "—"}</td>
                  <td className="small">{d.received_at.replace("T", " ").slice(0, 16)}</td>
                  <td className="small">{d.cutoff_at.replace("T", " ").slice(0, 16)}</td>
                  <td>r{d.revision_no}{d.supersedes_doc_id ? `（替代 #${d.supersedes_doc_id}）` : ""}</td>
                  <td className="small muted">{d.raw_text ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tab === "events" && (
        <>
          <div className="card scroll-x">
            <h3>事件（时变；遍历边时按整条通行时段判断）</h3>
            <table>
              <thead><tr><th>事件</th><th>类型</th><th>边</th><th>生效起</th><th>生效止</th><th>限速</th><th>修订</th><th>来源原文</th></tr></thead>
              <tbody>
                {events.map((e) => (
                  <tr key={`${e.event_id}-r${e.revision_no}`}>
                    <td className="mono">{e.event_id}</td>
                    <td><span className={`pill ${e.event_type === "closure" ? "uncovered" : "done"}`}>{e.event_type}</span></td>
                    <td className="mono">{e.edge_id}</td>
                    <td className="small">{e.effective_from.replace("T", " ").slice(11, 16)}</td>
                    <td className="small">{e.effective_to ? e.effective_to.replace("T", " ").slice(11, 16) : "尚未恢复"}</td>
                    <td>{e.speed_limit_kmh ? `${e.speed_limit_kmh}km/h` : "—"}</td>
                    <td>r{e.revision_no}</td>
                    <td className="small muted">{e.source_raw ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="card scroll-x">
            <h3>修订审计留痕</h3>
            <table>
              <thead><tr><th>事件</th><th>修订号</th><th>变更摘要</th><th>变更内容</th><th>时间</th></tr></thead>
              <tbody>
                {audits.map((a, i) => (
                  <tr key={i}>
                    <td className="mono">{String(a.event_id)}</td>
                    <td>r{String(a.revision_no)}</td>
                    <td>{String(a.change_summary)}</td>
                    <td className="small mono">{JSON.stringify(a.change_json)}</td>
                    <td className="small">{String(a.changed_at).replace("T", " ").slice(0, 16)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab === "edges" && (
        <div className="card scroll-x">
          <h3>有向边修订（含单向交通组织；历史修订不隐藏）</h3>
          <table>
            <thead><tr><th>边</th><th>修订</th><th>方向</th><th>长度(m)</th><th>常规限速</th><th>有效期起</th><th>有效期止</th></tr></thead>
            <tbody>
              {edges.map((e) => (
                <tr key={`${e.edge_id}-${e.revision_no}`}>
                  <td className="mono">{e.edge_id}</td>
                  <td>r{e.revision_no}</td>
                  <td className="mono">{e.from_node}（{nodeName(e.from_node)}）→ {e.to_node}（{nodeName(e.to_node)}）</td>
                  <td>{e.length_m}</td>
                  <td>{e.base_speed_kmh}km/h</td>
                  <td className="small">{e.valid_from.replace("T", " ").slice(0, 16)}</td>
                  <td className="small">{e.valid_to ? e.valid_to.replace("T", " ").slice(0, 16) : "至今"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="small muted" style={{ marginTop: 8 }}>
            节点等待属性：{nodes.filter((n) => n.allow_wait).map((n) => n.node_id).join("、")} 允许等待；其余节点到达后不得停留等待道路恢复。
          </div>
        </div>
      )}

      {tab === "shelters" && (
        <div className="card scroll-x">
          <h3>既有安置点与开放区间（仅分析，不做选址/分配）</h3>
          <table>
            <thead><tr><th>安置点</th><th>名称</th><th>路网节点</th><th>开放起</th><th>开放止</th></tr></thead>
            <tbody>
              {shelters.map((s) => {
                const ws = windows.filter((w) => w.shelter_id === s.shelter_id);
                return ws.map((w, i) => (
                  <tr key={`${s.shelter_id}-${i}`}>
                    <td className="mono">{i === 0 ? s.shelter_id : ""}</td>
                    <td>{i === 0 ? s.name : ""}</td>
                    <td className="mono">{i === 0 ? s.node_id : ""}</td>
                    <td className="small">{w.open_from.replace("T", " ").slice(0, 16)}</td>
                    <td className="small">{w.open_to ? w.open_to.replace("T", " ").slice(0, 16) : "长期开放"}</td>
                  </tr>
                ));
              })}
            </tbody>
          </table>
        </div>
      )}

      {tab === "population" && (
        <>
          <div className="card">
            <h3>人口基准日（决定可见的人口修订）</h3>
            <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} style={{ maxWidth: 220 }} />
            <div className="small muted" style={{ marginTop: 6 }}>
              切到 2026-07-01 可看到 G6 的 1000 → 1200 修订；对比报告会把该分母变化单列为人口资料效应。
            </div>
          </div>
          <div className="card scroll-x">
            <h3>网格人口与互斥连接份额</h3>
            <table>
              <thead><tr><th>网格</th><th>边界</th><th>当前人口(修订)</th><th>连接节点与互斥比例</th><th>比例合计</th></tr></thead>
              <tbody>
                {grids.map((g) => {
                  const cs = connectors.filter((c) => c.grid_id === g.grid_id);
                  const sum = cs.reduce((a, c) => a + c.allocation_ratio, 0);
                  return (
                    <tr key={g.grid_id}>
                      <td className="mono">{g.grid_id}<div className="small muted">{g.name}</div></td>
                      <td>{g.boundary_id ?? "—"}</td>
                      <td>{g.population} <span className="small muted">(r{g.pop_revision})</span></td>
                      <td>{cs.length === 0
                        ? <span className="warn small">无连接依据 → 未知</span>
                        : cs.map((c) => (
                          <span key={c.node_id} className="path-chip">{c.node_id}：{(c.allocation_ratio * 100).toFixed(0)}%</span>
                        ))}</td>
                      <td className={sum > 1 ? "warn" : sum < 1 ? "warn" : "good"}>
                        {sum.toFixed(2)}{sum > 1 ? "（矛盾→未知）" : sum < 1 ? "（缺口→未知）" : ""}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
