import { useEffect, useMemo, useRef } from "react";
import L from "leaflet";
import type {
  ArrivalRow,
  EdgeRevision,
  NodeRow,
  ShareRow,
  ShelterRow,
} from "./types";

interface Props {
  nodes: NodeRow[];
  edges: EdgeRevision[];
  shelters: ShelterRow[];
  shares: ShareRow[];
  arrivals: ArrivalRow[];
  witnessEdgeIds: string[];
  witnessNodeIds: string[];
  selectedGridId: string | null;
  onSelectGrid: (gridId: string) => void;
}

/**
 * 离线平面地图（L.CRS.Simple，单位米）。
 * 颜色纪律：底色元素一律中性灰；只有“计算结果”着色——网格按 covered/
 * uncovered/unknown 三态，边只在成为某网格见证路径时高亮，绝不以预设颜色
 * 代替路径分析结论。
 */
export default function CoverageMap({
  nodes,
  edges,
  shelters,
  shares,
  arrivals,
  witnessEdgeIds,
  witnessNodeIds,
  selectedGridId,
  onSelectGrid,
}: Props) {
  const elRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layerRef = useRef<L.LayerGroup | null>(null);

  const nodeById = useMemo(
    () => new Map(nodes.map((n) => [n.node_id, n])),
    [nodes]
  );
  const arrivalByNode = useMemo(
    () => new Map(arrivals.map((a) => [a.node_id, a])),
    [arrivals]
  );
  const gridStatus = useMemo(() => {
    const m = new Map<string, ShareRow>();
    for (const s of shares) {
      const prev = m.get(s.grid_id);
      const rank = { covered: 3, uncovered: 2, unknown: 1 };
      if (!prev || rank[s.status] > rank[prev.status]) m.set(s.grid_id, s);
    }
    return m;
  }, [shares]);

  useEffect(() => {
    if (!elRef.current || mapRef.current) return;
    const map = L.map(elRef.current, {
      crs: L.CRS.Simple,
      zoomControl: true,
      attributionControl: false,
      minZoom: -3,
      maxZoom: 4,
    });
    mapRef.current = map;
    layerRef.current = L.layerGroup().addTo(map);
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    const layer = layerRef.current;
    if (!map || !layer || nodes.length === 0) return;
    layer.clearLayers();

    const latlng = (x: number, y: number) => L.latLng(y, x);
    const xs = nodes.map((n) => n.geom_x);
    const ys = nodes.map((n) => n.geom_y);
    const bounds = L.latLngBounds(
      [Math.min(...ys) - 600, Math.min(...xs) - 600],
      [Math.max(...ys) + 600, Math.max(...xs) + 600]
    );

    // 有向边：中性灰底 + 方向箭头；见证路径边高亮
    const witnessSet = new Set(witnessEdgeIds);
    for (const e of edges) {
      const a = nodeById.get(e.from_node);
      const b = nodeById.get(e.to_node);
      if (!a || !b) continue;
      const isWitness = witnessSet.has(e.edge_id);
      const line = L.polyline([latlng(a.geom_x, a.geom_y), latlng(b.geom_x, b.geom_y)], {
        color: isWitness ? "#e8c33c" : "#3a4a5e",
        weight: isWitness ? 4 : 2,
        opacity: isWitness ? 1 : 0.8,
      });
      line.bindPopup(
        `<b>${e.edge_id}</b>（有向边）<br/>${e.from_node} → ${e.to_node}<br/>` +
          `长度 ${e.length_m}m / 常规限速 ${e.base_speed_kmh}km/h<br/>` +
          `修订 r${e.revision_no}${isWitness ? "<br/><b style='color:#e8c33c'>当前见证路径经过此边</b>" : ""}`
      );
      line.addTo(layer);
      // 方向小箭头（中点三角形，用 circleMarker 替代外部图标依赖）
      const mx = (a.geom_x + b.geom_x) / 2;
      const my = (a.geom_y + b.geom_y) / 2;
      L.circleMarker(latlng(mx, my), {
        radius: 3,
        color: isWitness ? "#e8c33c" : "#5a6d84",
        fillColor: isWitness ? "#e8c33c" : "#5a6d84",
        fillOpacity: 1,
      }).addTo(layer);
    }

    // 节点：允许等待者加环；见证节点高亮
    const witnessNodeSet = new Set(witnessNodeIds);
    for (const n of nodes) {
      const arrival = arrivalByNode.get(n.node_id);
      const hl = witnessNodeSet.has(n.node_id);
      const marker = L.circleMarker(latlng(n.geom_x, n.geom_y), {
        radius: hl ? 7 : 5,
        color: n.allow_wait ? "#4da3ff" : "#7d8ea3",
        weight: n.allow_wait ? 2 : 1,
        fillColor: hl ? "#e8c33c" : "#1e2937",
        fillOpacity: 1,
      });
      marker.bindPopup(
        `<b>${n.node_id}</b> ${n.name ?? ""}<br/>允许等待：${n.allow_wait ? "是" : "否"}` +
          (arrival
            ? `<br/>最早到达：${arrival.arrival_time.replace("T", " ").slice(0, 19)}` +
              `<br/>累计等待：${Math.round(arrival.wait_cost_sec)}s` +
              `<br/>见证路径：${arrival.witness_edge_path.join(" → ") || "（起点）"}`
            : "<br/>时限内不可达")
      );
      marker.addTo(layer);
    }

    // 安置点：青色菱形（用 marker + divIcon，无外部图片依赖）
    for (const s of shelters) {
      const icon = L.divIcon({
        className: "",
        html: `<div style="width:14px;height:14px;background:#39d0c8;transform:rotate(45deg);border:1px solid #0c1117;"></div>`,
        iconSize: [14, 14],
        iconAnchor: [7, 7],
      });
      L.marker(latlng(s.geom_x, s.geom_y), { icon })
        .bindPopup(`<b style="color:#39d0c8">安置点 ${s.shelter_id}</b><br/>${s.name}<br/>节点 ${s.node_id}<br/><span class="muted">开放情况见来源目录，到达时刻必须在开放区间内</span>`)
        .addTo(layer);
    }

    // 网格：颜色只来自计算状态
    for (const [gridId, share] of gridStatus) {
      const anyShare = shares.find((s) => s.grid_id === gridId)!;
      const x = anyShare.geom_x;
      const y = anyShare.geom_y;
      if (x == null || y == null) continue;
      const colorMap = { covered: "#2fbf71", uncovered: "#f08c2e", unknown: "#9a7bd4" } as const;
      const selected = selectedGridId === gridId;
      const marker = L.circleMarker(latlng(x, y), {
        radius: selected ? 11 : 8,
        color: selected ? "#ffffff" : colorMap[share.status],
        weight: selected ? 2 : 1,
        fillColor: colorMap[share.status],
        fillOpacity: 0.85,
      });
      marker.bindPopup(
        `<b>${gridId}</b> ${anyShare.grid_name ?? ""}<br/>计算状态：${share.status}` +
          (share.reason ? `<br/>原因：${share.reason}` : "")
      );
      marker.on("click", () => onSelectGrid(gridId));
      marker.addTo(layer);
    }

    map.fitBounds(bounds);
  }, [
    nodes,
    edges,
    shelters,
    shares,
    arrivals,
    witnessEdgeIds,
    witnessNodeIds,
    selectedGridId,
    nodeById,
    arrivalByNode,
    gridStatus,
    onSelectGrid,
  ]);

  return <div ref={elRef} style={{ height: "100%", width: "100%" }} />;
}
