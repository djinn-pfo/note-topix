import { useEffect, useMemo, useRef } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import type { GraphEdge, GraphNode } from "../api";

const COMMUNITY_COLORS = [
  "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
  "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
  "#dcbeff", "#9a6324", "#800000", "#aaffc3", "#808000",
  "#ffd8b1", "#000075", "#a9a9a9",
];
const BUCKET_COLORS: Record<string, string> = {
  academic: "#7986cb",
  hn: "#ff8a65",
  "jp-tech": "#4db6ac",
  "jp-social": "#f06292",
  "general-jp": "#fdd835",
  curated: "#9575cd",
};

// Buffer features prefixed "v:" are verbs.
export function formatFeature(f: string): string {
  return f.startsWith("v:") ? `${f.slice(2)}(動)` : f;
}

type Props = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  colorBy: "community" | "bucket";
  selected: string | null;
  onSelect: (term: string | null) => void;
  height: number;
};

export default function NetworkGraph({
  nodes,
  edges,
  colorBy,
  selected,
  onSelect,
  height,
}: Props) {
  const fgRef = useRef<ForceGraphMethods | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement>(null);

  // react-force-graph 2d mutates the nodes array; keep a fresh copy per render.
  const data = useMemo(
    () => ({
      nodes: nodes.map((n) => ({ ...n })),
      links: edges.map((e) => ({ ...e })),
    }),
    [nodes, edges],
  );

  useEffect(() => {
    // Modest zoom-to-fit whenever the data changes.
    const t = setTimeout(() => fgRef.current?.zoomToFit(400, 40), 400);
    return () => clearTimeout(t);
  }, [nodes, edges]);

  return (
    <div ref={containerRef} className="w-full bg-white border border-slate-200 rounded-md overflow-hidden">
      <ForceGraph2D
        ref={fgRef}
        graphData={data}
        height={height}
        nodeRelSize={4}
        cooldownTicks={120}
        linkColor={() => "rgba(120,130,140,0.35)"}
        linkWidth={(l: any) => 0.4 + 3 * Math.max(0, l.weight)}
        linkLabel={(l: any) =>
          l.shared?.length
            ? `${l.weight.toFixed(2)} · ${l.shared.map(formatFeature).join(", ")}` +
              (l.co_mentions ? ` · 同時出現 ${l.co_mentions}` : "")
            : ""
        }
        nodeLabel={(n: any) => {
          const cls = n.classification?.label ?? "(unlabeled)";
          return `${n.id}\noccurrence: ${n.occurrence}\nengagement: ${Math.round(n.engagement_sum)}\nbuckets: ${n.buckets.join(", ")}\nclass: ${cls}`;
        }}
        nodeVal={(n: any) => 2 + Math.log1p(n.engagement_sum) * 0.5}
        nodeCanvasObjectMode={() => "after"}
        nodeCanvasObject={(node: any, ctx, globalScale) => {
          const isSel = selected === node.id;
          const fill =
            colorBy === "community" && node.community != null
              ? COMMUNITY_COLORS[node.community % COMMUNITY_COLORS.length]
              : BUCKET_COLORS[node.dominant_bucket] ?? "#90a4ae";
          const r = 3 + Math.log1p(node.engagement_sum) * 0.7;
          ctx.beginPath();
          ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
          ctx.fillStyle = fill;
          ctx.fill();
          if (isSel) {
            ctx.lineWidth = 2 / globalScale;
            ctx.strokeStyle = "#111";
            ctx.stroke();
          }
          // Draw label at higher zooms
          if (globalScale > 1.2 || isSel) {
            const fontSize = Math.max(9, 12 / globalScale);
            ctx.font = `${fontSize}px sans-serif`;
            ctx.fillStyle = "#222";
            ctx.textAlign = "center";
            ctx.textBaseline = "top";
            ctx.fillText(node.id, node.x, node.y + r + 2);
          }
        }}
        onNodeClick={(node: any) => onSelect(node.id)}
        onBackgroundClick={() => onSelect(null)}
      />
    </div>
  );
}
