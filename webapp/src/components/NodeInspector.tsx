import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type BufferParams, type DateRange, type GraphNode, type CommunityName } from "../api";
import { BucketBadge, SourceBadge } from "./BucketBadge";
import { formatFeature } from "./NetworkGraph";

type Props = {
  term: string;
  node: GraphNode | undefined;
  community: CommunityName | null;
  mode: string;
  buffer?: BufferParams;   // set when the graph uses the buffer-overlap method
  range?: DateRange;       // the graph's period; tabs show evidence from it only
  onClose?: () => void;
};

type Tab = "buffer" | "signals" | "verbs" | "community" | "note";

export default function NodeInspector({ term, node, community, mode, buffer, range, onClose }: Props) {
  const [tab, setTab] = useState<Tab>(buffer ? "buffer" : "signals");
  const activeTab = tab === "buffer" && !buffer ? "signals" : tab;

  return (
    <div className="md:border-l border-slate-200 bg-white h-full overflow-hidden flex flex-col">
      <div className="p-4 border-b border-slate-200 sticky top-0 bg-white flex items-start gap-2">
        <div className="flex-1 min-w-0">
          <h2 className="text-lg font-bold text-slate-900 break-all">{term}</h2>
          {node && (
            <div className="mt-2 flex flex-wrap gap-2 text-xs text-slate-600">
              <span>出現 <b>{node.occurrence}</b></span>
              <span>·</span>
              <span>engagement <b>{Math.round(node.engagement_sum).toLocaleString()}</b></span>
              <span>·</span>
              <span>class: <b>{node.classification?.label ?? "-"}</b></span>
              {node.community != null && (
                <>
                  <span>·</span>
                  <span>community: <b>#{node.community}</b></span>
                </>
              )}
            </div>
          )}
          {node?.aliases && node.aliases.length > 0 && (
            <div className="mt-1 text-xs text-slate-500">表記ゆれ: {node.aliases.join(", ")}</div>
          )}
          {node && node.buckets.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {node.buckets.map((b) => <BucketBadge key={b} bucket={b} />)}
            </div>
          )}
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 text-slate-500 hover:text-slate-800 px-2 py-1 -mt-1 -mr-1"
            aria-label="close"
          >✕</button>
        )}
      </div>

      <nav className="flex border-b border-slate-200 shrink-0">
        {buffer && (
          <TabButton current={activeTab} value="buffer" onClick={setTab}>🧩 バッファ</TabButton>
        )}
        <TabButton current={activeTab} value="signals" onClick={setTab}>📰 原典</TabButton>
        <TabButton current={activeTab} value="verbs" onClick={setTab}>🔤 共起動詞</TabButton>
        <TabButton current={activeTab} value="community" onClick={setTab}>🎨 コミュニティ</TabButton>
        <TabButton current={activeTab} value="note" onClick={setTab}>💴 note.com</TabButton>
      </nav>

      <div className="flex-1 overflow-y-auto">
        {activeTab === "buffer" && buffer && <BufferTab term={term} params={buffer} range={range} />}
        {activeTab === "signals" && <SignalsTab term={term} mode={mode} range={range} />}
        {activeTab === "verbs" && <VerbsTab term={term} mode={mode} range={range} />}
        {activeTab === "community" && <CommunityTab community={community} />}
        {activeTab === "note" && <NoteCoverageTab term={term} />}
      </div>
    </div>
  );
}

function TabButton({
  current, value, onClick, children,
}: {
  current: Tab; value: Tab; onClick: (v: Tab) => void; children: React.ReactNode;
}) {
  const active = current === value;
  return (
    <button
      type="button"
      onClick={() => onClick(value)}
      className={`flex-1 py-2 text-sm font-medium transition ${
        active
          ? "border-b-2 border-blue-600 text-blue-700"
          : "text-slate-600 hover:text-slate-900"
      }`}
    >
      {children}
    </button>
  );
}

function BufferTab({ term, params, range }: { term: string; params: BufferParams; range?: DateRange }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["buffer-profile", term, params, range],
    queryFn: () => api.bufferProfile(term, params, range),
    retry: false,
  });
  if (isLoading) return <div className="p-4 text-sm text-slate-500">読込中...</div>;
  if (isError || !data) return <div className="p-4 text-sm text-slate-500">バッファプロファイル無し</div>;
  return (
    <div className="p-4 space-y-5">
      <div>
        <h4 className="text-xs uppercase tracking-wide text-slate-500 font-semibold">
          特徴的なバッファ
        </h4>
        <p className="text-xs text-slate-500 mt-1">
          前後{params.window}個以内に出た名詞{params.include_verbs ? "・動詞" : ""}。
          ソース別の出現率で正規化した PPMI 順 (×は出現回数)。
        </p>
        <div className="flex flex-wrap gap-1 mt-2">
          {data.profile.map((p) => (
            <span
              key={p.feature}
              className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${
                p.feature.startsWith("v:") ? "bg-amber-50 text-amber-900" : "bg-slate-100"
              }`}
            >
              {formatFeature(p.feature)}
              <span className="text-slate-500">×{p.count}</span>
            </span>
          ))}
        </div>
      </div>
      <div>
        <h4 className="text-xs uppercase tracking-wide text-slate-500 font-semibold">
          バッファが近い固有名詞
        </h4>
        {data.neighbors.length === 0 && (
          <p className="text-xs text-slate-500 mt-2">共有バッファを2つ以上持つ固有名詞は無し</p>
        )}
        <ul className="mt-2 space-y-2">
          {data.neighbors.map((n) => (
            <li key={n.term} className="rounded border border-slate-200 p-2">
              <div className="flex items-baseline gap-2">
                <span className="font-medium text-slate-900">{n.term}</span>
                <span className="text-xs font-mono text-slate-500">{n.weight.toFixed(2)}</span>
                {n.co_mentions > 0 && (
                  <span className="text-xs text-slate-400">同時出現 {n.co_mentions}</span>
                )}
              </div>
              <div className="mt-1 text-xs text-slate-600">
                {n.shared.map(formatFeature).join(" · ")}
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function SignalsTab({ term, mode, range }: { term: string; mode: string; range?: DateRange }) {
  const [sort, setSort] = useState<"engagement" | "date">("engagement");
  const { data: signals, isLoading } = useQuery({
    queryKey: ["signals", term, mode, sort, range],
    queryFn: () => api.signals({ term, mode, limit: 30, sort, range }),
  });

  if (isLoading) return <div className="p-4 text-sm text-slate-500">読込中...</div>;
  if (!signals?.length) return <div className="p-4 text-sm text-slate-500">シグナル無し</div>;

  return (
    <div className="p-4 space-y-3">
      <div className="flex items-center gap-2 text-xs">
        <span className="text-slate-500">並び順:</span>
        <button
          onClick={() => setSort("engagement")}
          className={`px-2 py-0.5 rounded ${sort === "engagement" ? "bg-blue-100 text-blue-800" : "text-slate-600"}`}
        >engagement</button>
        <button
          onClick={() => setSort("date")}
          className={`px-2 py-0.5 rounded ${sort === "date" ? "bg-blue-100 text-blue-800" : "text-slate-600"}`}
        >date</button>
        <span className="text-slate-400 ml-auto">{signals.length} 件</span>
      </div>
      {signals.map((s) => (
        <details key={s.signal_id} className="rounded border border-slate-200 p-3 bg-slate-50">
          <summary className="cursor-pointer text-sm">
            <span className="mr-2"><SourceBadge source={s.source} /></span>
            <span className="text-slate-500 text-xs mr-2">{s.date}</span>
            <span className="text-slate-500 text-xs">eng={Math.round(s.engagement)}</span>
            <div className="mt-1 text-slate-800 font-medium">{s.title || "(no title)"}</div>
          </summary>
          <div className="mt-3 space-y-2">
            {s.url && (
              <a
                href={s.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-600 text-xs hover:underline break-all block"
              >
                {s.url}
              </a>
            )}
            {s.body && (
              <div className="text-xs text-slate-700 whitespace-pre-wrap max-h-64 overflow-y-auto p-2 bg-white rounded border border-slate-100">
                {s.body.slice(0, 1500)}
                {s.body.length > 1500 && "\n\n…"}
              </div>
            )}
          </div>
        </details>
      ))}
    </div>
  );
}

function VerbsTab({ term, mode, range }: { term: string; mode: string; range?: DateRange }) {
  const { data: verbs, isLoading } = useQuery({
    queryKey: ["verbs", term, mode, range],
    queryFn: () => api.verbsForTerm({ term, mode, limit: 25, range }),
  });
  if (isLoading) return <div className="p-4 text-sm text-slate-500">読込中...</div>;
  if (!verbs?.length) return <div className="p-4 text-sm text-slate-500">共起動詞無し</div>;
  return (
    <div className="p-4">
      <p className="text-xs text-slate-500 mb-3">
        このタームを含むシグナル内に出現した動詞 (共起頻度順)。
        「そのタームで何が行われている / 語られているか」を示す。
      </p>
      <div className="flex flex-wrap gap-2">
        {verbs.map((v) => (
          <span
            key={v.verb}
            className="inline-flex items-center gap-1 bg-slate-100 rounded-full px-3 py-1 text-sm"
          >
            {v.verb}
            <span className="text-xs text-slate-500">{v.count}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function CommunityTab({ community }: { community: CommunityName | null }) {
  if (!community) {
    return (
      <div className="p-4 text-sm text-slate-500">
        このコミュニティは未命名。バッファ重なり方式のコミュニティ (3メンバー以上) は
        パイプラインが自動で命名します (手動なら
        <code className="mx-1 bg-slate-100 px-1 rounded">uv run scripts/name_communities.py run</code>)。
      </div>
    );
  }
  return (
    <div className="p-4 space-y-3">
      <div>
        <h3 className="font-semibold text-slate-900">🎨 {community.name}</h3>
        {community.description && (
          <p className="text-sm text-slate-700 mt-1 leading-6">{community.description}</p>
        )}
      </div>
      {community.buffers && community.buffers.length > 0 && (
        <div>
          <h4 className="text-xs uppercase tracking-wide text-slate-500 font-semibold">共有バッファ</h4>
          <div className="flex flex-wrap gap-1 mt-2">
            {community.buffers.slice(0, 15).map((b) => (
              <span key={b} className="inline-flex items-center bg-slate-100 rounded-full px-2 py-0.5 text-xs">
                {b}
              </span>
            ))}
          </div>
        </div>
      )}
      {community.top_verbs.length > 0 && (
        <div>
          <h4 className="text-xs uppercase tracking-wide text-slate-500 font-semibold">頻出動詞</h4>
          <div className="flex flex-wrap gap-1 mt-2">
            {community.top_verbs.slice(0, 12).map(([v, c]) => (
              <span key={v} className="inline-flex items-center gap-1 bg-slate-100 rounded-full px-2 py-0.5 text-xs">
                {v} <span className="text-slate-500">{c}</span>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function NoteCoverageTab({ term }: { term: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["note-coverage", term],
    queryFn: () => api.noteCoverageOne(term),
    retry: false,
  });
  if (isLoading) return <div className="p-4 text-sm text-slate-500">読込中...</div>;
  if (isError || !data) {
    return (
      <div className="p-4 text-sm text-slate-500">
        このタームの note.com飽和度キャッシュはまだありません。
        次回の pipeline 実行時 (top-N terms) に取得されます。
      </div>
    );
  }
  const total = data.total ?? 0;
  const cls =
    total === 0 ? "bg-green-100 text-green-800" :
    total < 10 ? "bg-green-100 text-green-800" :
    total < 100 ? "bg-yellow-100 text-yellow-800" :
    total < 1000 ? "bg-orange-100 text-orange-800" :
    "bg-red-100 text-red-800";
  const label =
    total === 0 ? "🟢 未開拓" :
    total < 10 ? "🟢 希薄" :
    total < 100 ? "🟡 中" :
    total < 1000 ? "🟠 やや飽和" :
    "🔴 飽和";
  return (
    <div className="p-4 space-y-3">
      <div className="flex items-center gap-3">
        <span className={`px-3 py-1 rounded-md font-medium text-sm ${cls}`}>{label}</span>
        <span className="text-2xl font-bold text-slate-900">{total.toLocaleString()}</span>
        <span className="text-xs text-slate-500">note.com 有料記事数</span>
      </div>
      {data.top_titles.length > 0 && (
        <div>
          <h4 className="text-xs uppercase tracking-wide text-slate-500 font-semibold mb-2">既存タイトル例</h4>
          <ul className="text-sm space-y-1 list-disc pl-5 text-slate-700">
            {data.top_titles.map((t, i) => <li key={i}>{t}</li>)}
          </ul>
        </div>
      )}
      <p className="text-xs text-slate-400 mt-2">最終取得: {data.fetched_at}</p>
    </div>
  );
}
