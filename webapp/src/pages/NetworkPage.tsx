import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type BufferParams, type DateRange } from "../api";
import NetworkGraph from "../components/NetworkGraph";
import NodeInspector from "../components/NodeInspector";

const EDGE_METHODS = [
  { value: "npmi", label: "NPMI (推奨)" },
  { value: "pmi", label: "PMI" },
  { value: "jaccard", label: "Jaccard" },
  { value: "count", label: "生共起カウント" },
  // AI mode only: proper nouns linked by overlap of their nearby common nouns / verbs.
  { value: "buffer", label: "バッファ重なり (AI固有名詞)" },
];
const WEIGHT_RANGE: Record<string, { min: number; max: number; step: number; default: number }> = {
  npmi: { min: -0.2, max: 1.0, step: 0.05, default: 0.25 },
  pmi: { min: 0, max: 8, step: 0.5, default: 3.0 },
  jaccard: { min: 0, max: 1, step: 0.02, default: 0.1 },
  count: { min: 2, max: 50, step: 1, default: 4 },
  buffer: { min: 0, max: 1, step: 0.01, default: 0.15 },
};

const LABELS = [
  "PROPER_PRODUCT", "PROPER_ORG", "PROPER_PERSON", "PROPER_PLACE",
  "PROPER_WORK", "CONCEPT", "GENERIC", "JUNK",
];
const PROPER_LABELS = LABELS.filter((l) => l.startsWith("PROPER_"));

// "Last N days" presets count back from the newest signal, not from today,
// so they stay meaningful when fetches lag.
const RANGE_PRESETS = [
  { label: "全期間", days: null },
  { label: "1日", days: 1 },
  { label: "3日", days: 3 },
  { label: "7日", days: 7 },
  { label: "14日", days: 14 },
] as const;

function shiftDate(ymd: string, days: number): string {
  const d = new Date(`${ymd}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export default function NetworkPage() {
  const [mode, setMode] = useState<"all" | "propn" | "ai">("all");
  const [method, setMethod] = useState<string>("npmi");
  const [minOcc, setMinOcc] = useState(8);
  const [minWeight, setMinWeight] = useState(0.25);
  const [maxNodes, setMaxNodes] = useState(150);
  const [community, setCommunity] = useState(true);
  const [colorBy, setColorBy] = useState<"community" | "bucket">("community");
  const [aiLabels, setAiLabels] = useState<string[]>(PROPER_LABELS);
  const [dateFrom, setDateFrom] = useState("");   // "" = open bound
  const [dateTo, setDateTo] = useState("");
  const [bufferWindow, setBufferWindow] = useState(5);
  const [includeVerbs, setIncludeVerbs] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [controlsOpen, setControlsOpen] = useState<boolean>(false);

  const effectiveMode = mode === "ai" ? "all" : mode;
  const labelFilter = mode === "ai" ? aiLabels : undefined;
  const { data: meta } = useQuery({ queryKey: ["meta"], queryFn: api.meta });
  const range: DateRange | undefined = useMemo(
    () => (dateFrom || dateTo ? { date_from: dateFrom || undefined, date_to: dateTo || undefined } : undefined),
    [dateFrom, dateTo],
  );
  const applyPreset = (days: number | null) => {
    if (days === null || !meta?.date_max) {
      setDateFrom("");
      setDateTo("");
      return;
    }
    setDateFrom(shiftDate(meta.date_max, -(days - 1)));
    setDateTo(meta.date_max);
  };
  const bufferParams: BufferParams | undefined =
    method === "buffer"
      ? { window: bufferWindow, include_verbs: includeVerbs, label_filter: labelFilter }
      : undefined;
  const methods = EDGE_METHODS.filter((m) => m.value !== "buffer" || mode === "ai");

  const selectMethod = (m: string) => {
    setMethod(m);
    setMinWeight(WEIGHT_RANGE[m].default);
    // Proper nouns are sparse; the default node threshold would hide most cores.
    if (m === "buffer") setMinOcc(2);
  };
  const selectMode = (m: "all" | "propn" | "ai") => {
    setMode(m);
    if (m === "ai" && method !== "buffer") selectMethod("buffer");
    if (m !== "ai" && method === "buffer") {
      selectMethod("npmi");
      setMinOcc(8);
    }
  };

  const { data: graph, isLoading, error } = useQuery({
    queryKey: ["graph", effectiveMode, method, minOcc, minWeight, maxNodes, community, labelFilter, bufferParams, range],
    queryFn: () =>
      api.graph({
        mode: effectiveMode,
        method,
        min_occurrence: minOcc,
        min_weight: minWeight,
        max_nodes: maxNodes,
        community,
        label_filter: labelFilter,
        buffer: bufferParams,
        range,
      }),
  });

  type Node = NonNullable<typeof graph>["nodes"][number];
  const nodeById = useMemo(() => {
    const map = new Map<string, Node>();
    graph?.nodes.forEach((n) => map.set(n.id, n));
    return map;
  }, [graph]);

  const selectedNode = selected ? nodeById.get(selected) : undefined;
  const selectedCommunity =
    selectedNode?.community != null && graph?.communities[String(selectedNode.community)]
      ? graph.communities[String(selectedNode.community)]
      : null;

  const wr = WEIGHT_RANGE[method];

  return (
    <div className="flex h-full overflow-hidden relative">
      {/* Mobile toggle bar */}
      <div className="md:hidden fixed top-12 left-0 right-0 z-30 bg-white border-b border-slate-200 px-3 py-2 flex items-center gap-2 shadow-sm">
        <button
          type="button"
          onClick={() => setControlsOpen(true)}
          className="text-sm bg-slate-100 hover:bg-slate-200 px-2 py-1 rounded"
        >☰ 設定</button>
        <span className="text-xs text-slate-500 truncate">
          {graph ? `${graph.nodes.length}n / ${graph.edges.length}e` : "..."}
        </span>
      </div>
      {controlsOpen && (
        <div
          className="md:hidden fixed inset-0 bg-black/40 z-40"
          onClick={() => setControlsOpen(false)}
        />
      )}
      {/* Controls sidebar */}
      <aside
        className={`fixed md:static top-0 bottom-0 left-0 z-50 w-64 shrink-0 bg-white border-r border-slate-200 overflow-y-auto p-4 space-y-4 text-sm transform transition-transform md:transform-none ${
          controlsOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0"
        }`}
      >
        <div className="flex items-center gap-2 md:hidden">
          <h2 className="font-semibold text-slate-800 flex-1">設定</h2>
          <button
            type="button"
            onClick={() => setControlsOpen(false)}
            className="text-slate-500 hover:text-slate-700 px-1"
            aria-label="close"
          >✕</button>
        </div>
        <h2 className="font-semibold text-slate-800 hidden md:block">共起ネットワーク</h2>

        <div>
          <label className="text-xs font-medium text-slate-600 block mb-1">
            期間 (記事の公開日)
          </label>
          <div className="flex flex-wrap gap-1 mb-2">
            {RANGE_PRESETS.map((p) => {
              const active =
                p.days === null
                  ? !dateFrom && !dateTo
                  : !!meta?.date_max && dateTo === meta.date_max &&
                    dateFrom === shiftDate(meta.date_max, -(p.days - 1));
              return (
                <button
                  key={p.label}
                  type="button"
                  onClick={() => applyPreset(p.days)}
                  className={`px-2 py-0.5 rounded text-xs ${
                    active ? "bg-blue-100 text-blue-800" : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                  }`}
                >
                  {p.days === null ? p.label : `直近${p.label}`}
                </button>
              );
            })}
          </div>
          <div className="flex items-center gap-1">
            <input
              type="date"
              value={dateFrom}
              min={meta?.date_min ?? undefined}
              max={dateTo || meta?.date_max || undefined}
              onChange={(e) => setDateFrom(e.target.value)}
              className="flex-1 min-w-0 border border-slate-300 rounded px-1 py-0.5 text-xs bg-white"
            />
            <span className="text-slate-400 text-xs">〜</span>
            <input
              type="date"
              value={dateTo}
              min={dateFrom || meta?.date_min || undefined}
              max={meta?.date_max ?? undefined}
              onChange={(e) => setDateTo(e.target.value)}
              className="flex-1 min-w-0 border border-slate-300 rounded px-1 py-0.5 text-xs bg-white"
            />
          </div>
          {meta?.date_min && (
            <p className="text-[11px] text-slate-400 mt-1">
              データ: {meta.date_min} 〜 {meta.date_max}
              {range && " · 期間指定時はその期間の記事だけで再計算 (コミュニティ名は全期間の分割と一致した時のみ表示)"}
            </p>
          )}
        </div>

        <div>
          <label className="text-xs font-medium text-slate-600 block mb-1">
            ターム抽出モード
          </label>
          <select
            value={mode}
            onChange={(e) => selectMode(e.target.value as any)}
            className="w-full border border-slate-300 rounded px-2 py-1 bg-white"
          >
            <option value="all">全名詞 (POS)</option>
            <option value="propn">固有名詞のみ (POS)</option>
            <option value="ai">AI分類 (DBラベル)</option>
          </select>
        </div>

        {mode === "ai" && (
          <div>
            <label className="text-xs font-medium text-slate-600 block mb-1">
              採用ラベル
            </label>
            <div className="space-y-1">
              {LABELS.map((label) => (
                <label key={label} className="flex items-center gap-2 text-xs cursor-pointer">
                  <input
                    type="checkbox"
                    checked={aiLabels.includes(label)}
                    onChange={(e) => {
                      setAiLabels((prev) =>
                        e.target.checked
                          ? [...prev, label]
                          : prev.filter((l) => l !== label),
                      );
                    }}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </div>
          </div>
        )}

        <div>
          <label className="text-xs font-medium text-slate-600 block mb-1">
            エッジ重み方式
          </label>
          <select
            value={method}
            onChange={(e) => selectMethod(e.target.value)}
            className="w-full border border-slate-300 rounded px-2 py-1 bg-white"
          >
            {methods.map((m) => (
              <option key={m.value} value={m.value}>{m.label}</option>
            ))}
          </select>
        </div>

        {method === "buffer" && (
          <div className="space-y-2 rounded border border-slate-200 bg-slate-50 p-2">
            <Slider
              label="バッファ窓幅 (前後N個)"
              value={bufferWindow}
              min={1} max={15} step={1}
              onChange={setBufferWindow}
            />
            <label className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={includeVerbs}
                onChange={(e) => setIncludeVerbs(e.target.checked)}
              />
              近接動詞もバッファに含める
            </label>
            <p className="text-[11px] leading-4 text-slate-500">
              固有名詞 (core) の前後N個の名詞・動詞 (buffer) をソース別に正規化し、
              その重なり (cosine) で辺を張る。辺にホバーで共有バッファを表示。
            </p>
          </div>
        )}

        <Slider
          label={`最小エッジ重み (${method})`}
          value={minWeight}
          min={wr.min} max={wr.max} step={wr.step}
          onChange={setMinWeight}
        />

        <Slider
          label="ノード最小出現数"
          value={minOcc}
          min={1} max={50} step={1}
          onChange={setMinOcc}
        />

        <Slider
          label="ノード数上限"
          value={maxNodes}
          min={30} max={500} step={10}
          onChange={setMaxNodes}
        />

        <div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={community}
              onChange={(e) => setCommunity(e.target.checked)}
            />
            Louvain クラスタリング
          </label>
          {community && (
            <div className="mt-2 ml-6 text-xs">
              <label className="block text-slate-600">色分け</label>
              <div className="flex gap-2 mt-1">
                <label className="flex items-center gap-1">
                  <input
                    type="radio"
                    value="community"
                    checked={colorBy === "community"}
                    onChange={() => setColorBy("community")}
                  />
                  community
                </label>
                <label className="flex items-center gap-1">
                  <input
                    type="radio"
                    value="bucket"
                    checked={colorBy === "bucket"}
                    onChange={() => setColorBy("bucket")}
                  />
                  bucket
                </label>
              </div>
            </div>
          )}
        </div>

        {graph && (
          <div className="mt-6 pt-4 border-t border-slate-200 text-xs text-slate-500 space-y-1">
            <div>Nodes: <b>{graph.nodes.length}</b></div>
            <div>Edges: <b>{graph.edges.length}</b></div>
            <div>Communities: <b>{Object.keys(graph.communities).length}</b> 命名済</div>
          </div>
        )}
      </aside>

      {/* Graph area — leave room for mobile toggle bar at top */}
      <section className="flex-1 flex flex-col overflow-hidden bg-slate-50 pt-11 md:pt-0">
        <div className="flex-1 p-2 md:p-4 overflow-hidden">
          {isLoading && <div className="text-slate-500">グラフ計算中...</div>}
          {error && <div className="text-red-600">エラー: {String(error)}</div>}
          {graph && (
            <NetworkGraph
              nodes={graph.nodes}
              edges={graph.edges}
              colorBy={colorBy}
              selected={selected}
              onSelect={setSelected}
              height={window.innerHeight - 140}
            />
          )}
        </div>
      </section>

      {/* Node inspector — bottom sheet on mobile, side panel on desktop */}
      {selected && (
        <>
          <div
            className="md:hidden fixed inset-0 bg-black/30 z-40"
            onClick={() => setSelected(null)}
          />
          <div className="fixed md:static bottom-0 left-0 right-0 md:right-auto md:top-0 z-50 h-[70vh] md:h-full md:w-96 shrink-0 rounded-t-lg md:rounded-none shadow-2xl md:shadow-none">
            <NodeInspector
              term={selected}
              node={selectedNode}
              community={selectedCommunity}
              mode={effectiveMode}
              buffer={bufferParams}
              range={range}
              onClose={() => setSelected(null)}
            />
          </div>
        </>
      )}
    </div>
  );
}

function Slider({
  label, value, min, max, step, onChange,
}: {
  label: string; value: number; min: number; max: number; step: number;
  onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="flex justify-between items-center">
        <label className="text-xs font-medium text-slate-600">{label}</label>
        <span className="text-xs text-slate-500 font-mono">{value}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="w-full mt-1"
      />
    </div>
  );
}
