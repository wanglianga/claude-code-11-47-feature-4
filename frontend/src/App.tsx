import { useCallback, useState } from "react";
import ScenarioView from "./ScenarioView";
import ComparisonView from "./ComparisonView";
import SourcesView from "./SourcesView";
import CacheView from "./CacheView";
import PublicationsView from "./PublicationsView";
import ExposureView from "./ExposureView";

export type Toast = { kind: "info" | "error"; text: string; key: number };

let toastSeq = 0;

export default function App() {
  const [tab, setTab] = useState("scenario");
  const [toasts, setToasts] = useState<Toast[]>([]);

  const pushToast = useCallback((text: string, kind: "info" | "error" = "info") => {
    const key = ++toastSeq;
    setToasts((t) => [...t, { text, kind, key }]);
    window.setTimeout(() => {
      setToasts((t) => t.filter((x) => x.key !== key));
    }, 6000);
  }, []);

  const tabs = [
    ["scenario", "情景分析"],
    ["exposure", "临时暴露人口"],
    ["comparison", "情景对比"],
    ["sources", "来源目录"],
    ["cache", "缓存与任务"],
    ["publications", "发布记录"],
  ] as const;

  return (
    <div className="app">
      <header className="topbar">
        <h1>防汛需求与道路情景覆盖数据分析平台</h1>
        <span className="scope">边界：仅计算可达人口与证据限制，不做选址 / 分配 / 调运</span>
      </header>
      <nav className="tabs">
        {tabs.map(([key, label]) => (
          <button
            key={key}
            className={tab === key ? "active" : ""}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </nav>
      <main>
        {tab === "scenario" && <ScenarioView pushToast={pushToast} />}
        {tab === "exposure" && <ExposureView pushToast={pushToast} />}
        {tab === "comparison" && <ComparisonView pushToast={pushToast} />}
        {tab === "sources" && <SourcesView />}
        {tab === "cache" && <CacheView pushToast={pushToast} />}
        {tab === "publications" && <PublicationsView pushToast={pushToast} />}
      </main>
      {toasts.map((t) => (
        <div key={t.key} className={`toast ${t.kind === "error" ? "error" : ""}`}>
          {t.text}
        </div>
      ))}
    </div>
  );
}
