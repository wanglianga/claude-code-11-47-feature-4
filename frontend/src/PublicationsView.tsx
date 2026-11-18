import { useEffect, useState } from "react";
import { api } from "./api";

interface Publication {
  publication_id: string;
  title: string;
  scenario_a_id: string;
  scenario_b_id: string;
  evidence_cutoff_at: string;
  created_by: string;
  created_at: string;
  input_manifest: Record<string, unknown>;
  comparison_json?: Record<string, unknown>;
}

export default function PublicationsView({
  pushToast,
}: {
  pushToast: (t: string, k?: "info" | "error") => void;
}) {
  const [rows, setRows] = useState<Publication[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Publication | null>(null);

  const refresh = () =>
    api
      .publications()
      .then((r) => setRows(r.publications as unknown as Publication[]));

  useEffect(() => {
    refresh().catch((e) => pushToast(String(e), "error"));
  }, []);

  const open = async (id: string) => {
    setOpenId(id);
    try {
      const d = await fetch(`/api/publications/${id}`).then((r) => r.json());
      setDetail(d as Publication);
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  return (
    <div className="full-page">
      <div className="card">
        <h3>结果发布：每次比较采用的完整输入均被固化</h3>
        <div className="small muted">
          发布清单包含两侧情景的人口基准日、出发时刻、时限、道路截止点、路网版本指纹、各网格人口与边界归属，
          确保事后可以区分“道路恢复 / 人口资料变化 / 分析边界改变”三种来源。
        </div>
      </div>
      <div className="card scroll-x">
        <table>
          <thead><tr><th>发布</th><th>标题</th><th>证据截止</th><th>发布人</th><th>时间</th><th></th></tr></thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.publication_id}>
                <td className="mono small">{p.publication_id.slice(0, 8)}</td>
                <td>{p.title}</td>
                <td className="small">{p.evidence_cutoff_at.replace("T", " ").slice(0, 16)}</td>
                <td>{p.created_by}</td>
                <td className="small">{p.created_at.replace("T", " ").slice(0, 16)}</td>
                <td><button className="btn secondary" onClick={() => open(p.publication_id)}>查看完整清单</button></td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan={6} className="muted">暂无发布记录，可在“情景对比”页生成。</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {openId && detail && (
        <div className="card">
          <h3>发布 {openId.slice(0, 8)} 的输入清单与比较结果</h3>
          <pre className="small" style={{ whiteSpace: "pre-wrap", maxHeight: 480, overflow: "auto" }}>
            {JSON.stringify(
              { input_manifest: detail.input_manifest, comparison: detail.comparison_json },
              null,
              2
            )}
          </pre>
        </div>
      )}
    </div>
  );
}
