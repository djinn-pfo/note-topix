import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type BacklogEntry, type ChallengeData } from "../api";

const KIND_LABEL: Record<string, string> = {
  why: "なぜ系",
  news: "速報コメント",
  retro: "振り返り",
  glossary: "用語解説",
  build: "開発記録",
  other: "未分類",
};
const STATE_LABEL: Record<string, string> = {
  candidate: "候補",
  researched: "調査済み",
  writing: "執筆中",
  published: "公開済み",
  dropped: "見送り",
};
const STATE_STYLE: Record<string, string> = {
  candidate: "bg-slate-100 text-slate-700",
  researched: "bg-amber-100 text-amber-800",
  writing: "bg-blue-100 text-blue-800",
  published: "bg-green-100 text-green-800",
  dropped: "bg-slate-50 text-slate-400",
};

export default function ChallengePage() {
  const [tab, setTab] = useState<"progress" | "backlog">("progress");
  return (
    <div className="h-full overflow-y-auto">
      <div className="p-4 max-w-5xl mx-auto">
        <nav className="flex gap-1 mb-4">
          {(["progress", "backlog"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-3 py-1.5 rounded text-sm font-medium ${
                tab === t ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-700 hover:bg-slate-200"
              }`}
            >
              {t === "progress" ? "🏃 100日チャレンジ" : "📥 ネタ在庫"}
            </button>
          ))}
        </nav>
        {tab === "progress" ? <Progress /> : <Backlog />}
      </div>
    </div>
  );
}

function Progress() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery<ChallengeData>({ queryKey: ["challenge"], queryFn: api.challenge });
  const refresh = useMutation({
    mutationFn: () => api.refreshArticles(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["challenge"] }),
  });
  const setKind = useMutation({
    mutationFn: ({ id, kind }: { id: number; kind: string }) => api.updateArticle(id, { kind }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["challenge"] }),
  });

  if (isLoading || !data) return <div className="text-slate-500 text-sm">読込中...</div>;
  const p = data.progress;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="連続日数" value={`${p.streak}日`} accent />
        <Stat label="達成日数" value={`${p.posted_days} / 100`} />
        <Stat label="残り" value={`${p.remaining}日`} />
        <Stat
          label="今日"
          value={p.today_done ? "✅ 投稿済み" : "⬜ まだ"}
          accent={!p.today_done}
        />
      </div>

      <div className="flex items-center gap-2 text-xs text-slate-500">
        <span>開始 {p.start} · {p.day_number}日目 · 登録 {p.published}本</span>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          className="ml-auto bg-slate-100 hover:bg-slate-200 px-2 py-1 rounded disabled:opacity-50"
        >
          {refresh.isPending ? "取得中..." : "🔄 noteから取り込み"}
        </button>
      </div>

      {p.missing_days.length > 0 && (
        <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
          投稿のない日: {p.missing_days.join(", ")}
        </div>
      )}

      <div className="bg-white border border-slate-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 text-xs">
            <tr>
              <th className="text-left px-3 py-2">公開日</th>
              <th className="text-left px-3 py-2">タイトル</th>
              <th className="text-right px-3 py-2">スキ</th>
              <th className="text-left px-3 py-2">型</th>
            </tr>
          </thead>
          <tbody>
            {data.articles.map((a) => {
              const delta =
                a.likes != null && a.likes_24h_ago != null ? a.likes - a.likes_24h_ago : 0;
              return (
                <tr key={a.article_id} className="border-t border-slate-100">
                  <td className="px-3 py-2 text-xs text-slate-500 whitespace-nowrap">{a.published_on}</td>
                  <td className="px-3 py-2">
                    <a href={a.url ?? undefined} target="_blank" rel="noopener noreferrer" className="hover:underline">
                      {a.title}
                    </a>
                    {a.price > 0 && <span className="ml-1 text-xs text-amber-700">有料</span>}
                  </td>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    <b>{a.likes ?? 0}</b>
                    {delta > 0 && <span className="text-green-600 text-xs ml-1">+{delta}</span>}
                  </td>
                  <td className="px-3 py-2">
                    <select
                      value={a.kind ?? "other"}
                      onChange={(e) => setKind.mutate({ id: a.article_id, kind: e.target.value })}
                      className="border border-slate-200 rounded text-xs px-1 py-0.5 bg-white"
                    >
                      {data.kinds.map((k) => (
                        <option key={k} value={k}>{KIND_LABEL[k] ?? k}</option>
                      ))}
                    </select>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {Object.keys(p.by_kind).length > 0 && (
        <div className="text-xs text-slate-500">
          型の内訳:{" "}
          {Object.entries(p.by_kind)
            .map(([k, n]) => `${KIND_LABEL[k] ?? k} ${n}`)
            .join(" · ")}
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className={`rounded-lg border p-3 ${accent ? "border-blue-200 bg-blue-50" : "border-slate-200 bg-white"}`}>
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-xl font-bold text-slate-900">{value}</div>
    </div>
  );
}

function Backlog() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery<BacklogEntry[]>({ queryKey: ["backlog"], queryFn: api.backlog });
  const [state, setState] = useState<string>("candidate");
  const [freshOnly, setFreshOnly] = useState(true);
  const [whyOnly, setWhyOnly] = useState(true);
  const update = useMutation({
    mutationFn: (payload: { briefing: string; state: string }) => api.updateBacklog(payload),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backlog"] }),
  });

  const rows = useMemo(() => {
    let out = data ?? [];
    if (state !== "all") out = out.filter((r) => r.state === state);
    if (freshOnly) out = out.filter((r) => r.fresh);
    if (whyOnly) out = out.filter((r) => r.why_questions.length > 0);
    return out;
  }, [data, state, freshOnly, whyOnly]);

  if (isLoading || !data) return <div className="text-slate-500 text-sm">読込中...</div>;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <select
          value={state}
          onChange={(e) => setState(e.target.value)}
          className="border border-slate-300 rounded px-2 py-1 bg-white"
        >
          <option value="all">すべての状態</option>
          {Object.entries(STATE_LABEL).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={freshOnly} onChange={(e) => setFreshOnly(e.target.checked)} />
          まだ動いている話題だけ
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={whyOnly} onChange={(e) => setWhyOnly(e.target.checked)} />
          「なぜ」の問いがあるものだけ
        </label>
        <span className="text-slate-400 ml-auto">{rows.length} / {data.length} 件</span>
      </div>

      {rows.map((r) => (
        <div key={r.briefing} className="bg-white border border-slate-200 rounded-lg p-3">
          <div className="flex items-start gap-2 flex-wrap">
            <span className={`text-xs px-2 py-0.5 rounded ${STATE_STYLE[r.state] ?? ""}`}>
              {STATE_LABEL[r.state] ?? r.state}
            </span>
            <span className="font-medium text-slate-900">{r.title}</span>
            <span className="text-xs text-slate-400">
              {r.date}
              {r.days_since_seen != null && ` · 最終観測 ${r.days_since_seen}日前`}
              {r.lineage_runs != null && ` · ${r.lineage_runs}回観測`}
              {r.signal_growth != null && r.signal_growth > 0 && ` · 記事+${r.signal_growth}`}
            </span>
            <div className="ml-auto flex gap-1">
              {["researched", "writing", "published", "dropped"].map((s) => (
                <button
                  key={s}
                  onClick={() => update.mutate({ briefing: r.briefing, state: s })}
                  className="text-xs bg-slate-100 hover:bg-slate-200 px-2 py-0.5 rounded"
                >
                  {STATE_LABEL[s]}へ
                </button>
              ))}
            </div>
          </div>
          {r.why_questions.length > 0 && (
            <ul className="mt-2 space-y-1">
              {r.why_questions.map((q) => (
                <li key={q} className="text-sm text-slate-700 flex items-start gap-2">
                  <span className="text-slate-300">❓</span>
                  <span>{q}</span>
                  <button
                    onClick={() => navigator.clipboard.writeText(q)}
                    className="text-[11px] text-slate-400 hover:text-slate-700 shrink-0"
                  >コピー</button>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-2 text-[11px] text-slate-400 font-mono break-all">{r.briefing}</div>
        </div>
      ))}
      {rows.length === 0 && (
        <div className="text-sm text-slate-500">
          条件に合うネタがありません。絞り込みを外してみてください。
        </div>
      )}
    </div>
  );
}
