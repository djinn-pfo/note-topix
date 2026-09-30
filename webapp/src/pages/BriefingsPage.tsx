import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import CopyButton from "../components/CopyButton";

export default function BriefingsPage() {
  const [selected, setSelected] = useState<string | null>(null);

  const { data: briefings } = useQuery({
    queryKey: ["briefings"],
    queryFn: api.briefings,
  });

  const { data: briefing } = useQuery({
    queryKey: ["briefing", selected],
    queryFn: () => api.briefing(selected!),
    enabled: !!selected,
  });

  // Auto-select the newest briefing on first load.
  if (briefings && briefings.length > 0 && selected === null) {
    setSelected(briefings[0].name);
  }

  return (
    <div className="h-full flex flex-col md:flex-row overflow-hidden">
      {/* Mobile: dropdown selector at top */}
      <div className="md:hidden p-3 border-b border-slate-200 bg-white shrink-0">
        <label className="text-xs font-semibold text-slate-700 block mb-1">
          📝 ブリーフィング ({briefings?.length ?? 0}件)
        </label>
        <select
          value={selected ?? ""}
          onChange={(e) => setSelected(e.target.value || null)}
          className="w-full border border-slate-300 rounded px-2 py-2 bg-white text-sm"
        >
          {briefings?.map((b) => (
            <option key={b.name} value={b.name}>
              {b.name.replace(/\.md$/, "")}
            </option>
          ))}
        </select>
      </div>

      {/* Desktop: left rail file list */}
      <aside className="hidden md:block w-72 shrink-0 border-r border-slate-200 bg-white overflow-y-auto">
        <div className="p-3 border-b border-slate-200 sticky top-0 bg-white">
          <h2 className="font-semibold text-slate-800">📝 ブリーフィング</h2>
          <p className="text-xs text-slate-500 mt-1">
            {briefings?.length ?? 0} 件 · note下書きの叩き台
          </p>
        </div>
        <ul>
          {briefings?.map((b) => {
            const label = b.name.replace(/\.md$/, "");
            const active = selected === b.name;
            return (
              <li key={b.name}>
                <button
                  type="button"
                  onClick={() => setSelected(b.name)}
                  className={`w-full text-left px-3 py-2 text-sm border-b border-slate-100 transition ${
                    active
                      ? "bg-blue-50 text-blue-900 font-medium"
                      : "text-slate-700 hover:bg-slate-50"
                  }`}
                >
                  <div className="truncate">{label}</div>
                  <div className="text-xs text-slate-400 mt-0.5">
                    {(b.size_bytes / 1024).toFixed(1)} KB
                  </div>
                </button>
              </li>
            );
          })}
        </ul>
      </aside>

      {/* Content pane */}
      <section className="flex-1 overflow-y-auto">
        {!briefing ? (
          <div className="p-8 text-slate-500">
            ブリーフィングを選択してください。
            {briefings?.length === 0 && (
              <p className="mt-3 text-sm">
                まだブリーフィングがありません。以下で生成できます:
                <pre className="mt-2 bg-slate-100 p-3 rounded text-xs">
                  uv run scripts/generate_briefing.py suggest --top 10{"\n"}
                  uv run scripts/generate_briefing.py prepare --community "&lt;name&gt;"
                </pre>
              </p>
            )}
          </div>
        ) : (
          <div className="max-w-4xl mx-auto p-3 md:p-8">
            <div className="mb-3 md:mb-4 flex items-center gap-2 flex-wrap sticky top-0 bg-slate-50 py-2 z-10">
              <CopyButton text={briefing.content} label="📋 全文コピー" />
              <CopyButton text={briefing.body} label="📋 本文のみ" />
              <span className="hidden md:inline text-xs text-slate-400 ml-auto font-mono truncate">
                documents/briefings/{briefing.name}
              </span>
            </div>
            <article className="briefing-prose bg-white rounded-lg border border-slate-200 p-4 md:p-8">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{briefing.body}</ReactMarkdown>
            </article>
          </div>
        )}
      </section>
    </div>
  );
}
