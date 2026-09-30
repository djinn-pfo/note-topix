import { useEffect, useState } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type EditDir, type ManuscriptContent, type PublishKit } from "../api";
import CopyButton from "../components/CopyButton";

type ViewMode = "split" | "edit" | "preview";

// Frontmatter the annex builder expects. No inline comments: the parser reads
// values verbatim.
const ANNEX_TEMPLATE = (filename: string) => `---
title: "${filename.replace(/^\d{4}-\d{2}-\d{2}-/, "").replace(/\.md$/, "")}"
date: ${new Date().toISOString().slice(0, 10)}
slug: ${filename.replace(/\.md$/, "")}
note_url: ""
summary: ""
draft: true
---

この記事の裏側の資料です。本文は note 側にあります。

## 自動生成されたトピック

## 調査ログ
`;

function PublishKitPanel({ kit, onClose }: { kit: PublishKit; onClose: () => void }) {
  const [saved, setSaved] = useState<string>("");
  const saveSvg = async (title: string, svg: string) => {
    try {
      const asset = await api.saveSvg({ name: `${title.slice(0, 24)}.svg`, svg, prompt: title });
      setSaved(`🖼️ ${asset.name} を画像に保存しました`);
    } catch (e) {
      setSaved(`保存失敗: ${(e as Error).message}`);
    }
  };
  return (
    <div className="border-b border-slate-200 bg-slate-50 p-3 space-y-3 max-h-[45vh] overflow-y-auto">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-semibold text-slate-800">📣 公開キット</h3>
        {saved && <span className="text-xs text-slate-600">{saved}</span>}
        <button onClick={onClose} className="ml-auto text-slate-500 hover:text-slate-800 px-1">✕</button>
      </div>

      {kit.title_candidates?.length ? (
        <div>
          <div className="text-xs font-medium text-slate-600 mb-1">タイトル案</div>
          <ul className="space-y-1">
            {kit.title_candidates.map((t) => (
              <li key={t} className="text-sm flex items-start gap-2">
                <span>{t}</span>
                <button
                  onClick={() => navigator.clipboard.writeText(t)}
                  className="text-[11px] text-slate-400 hover:text-slate-700 shrink-0"
                >コピー</button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {kit.x_post && (
        <div>
          <div className="text-xs font-medium text-slate-600 mb-1">X 投稿文（{"{URL}"} を記事リンクに置き換え）</div>
          <pre className="text-xs bg-white border border-slate-200 rounded p-2 whitespace-pre-wrap">{kit.x_post}</pre>
          <button
            onClick={() => navigator.clipboard.writeText(kit.x_post ?? "")}
            className="mt-1 text-xs bg-slate-100 px-2 py-0.5 rounded hover:bg-slate-200"
          >📋 コピー</button>
        </div>
      )}

      {kit.tags?.length ? (
        <div>
          <div className="text-xs font-medium text-slate-600 mb-1">ハッシュタグ</div>
          <div className="flex flex-wrap gap-1">
            {kit.tags.map((t) => (
              <span key={t} className="text-xs bg-white border border-slate-200 rounded-full px-2 py-0.5">#{t}</span>
            ))}
          </div>
          <button
            onClick={() => navigator.clipboard.writeText((kit.tags ?? []).map((t) => `#${t}`).join(" "))}
            className="mt-1 text-xs bg-slate-100 px-2 py-0.5 rounded hover:bg-slate-200"
          >📋 まとめてコピー</button>
        </div>
      ) : null}

      {kit.diagrams?.length ? (
        <div className="space-y-2">
          <div className="text-xs font-medium text-slate-600">図解の候補</div>
          {kit.diagrams.map((d) => (
            <div key={d.title} className="bg-white border border-slate-200 rounded p-2">
              <div className="text-sm font-medium text-slate-800">{d.title}</div>
              <div className="text-xs text-slate-500 mb-2">{d.why}</div>
              <div className="overflow-auto" dangerouslySetInnerHTML={{ __html: d.svg }} />
              <button
                onClick={() => saveSvg(d.title, d.svg)}
                className="mt-2 text-xs bg-blue-600 text-white px-2 py-1 rounded hover:bg-blue-700"
              >画像に保存（あとで編集・PNG書き出し）</button>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default function ManuscriptsPage() {
  const qc = useQueryClient();
  const [dir, setDir] = useState<EditDir>("manuscripts");
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [dirty, setDirty] = useState<boolean>(false);
  // Default to split on desktop; mobile defaults to edit-only (checked at render)
  const [view, setView] = useState<ViewMode>(() =>
    typeof window !== "undefined" && window.innerWidth < 768 ? "edit" : "split",
  );
  const [saveStatus, setSaveStatus] = useState<string>("");
  const [kit, setKit] = useState<PublishKit | null>(null);
  const [drawerOpen, setDrawerOpen] = useState<boolean>(false);

  const { data: manuscripts } = useQuery({
    queryKey: ["manuscripts", dir],
    queryFn: () => api.manuscripts(dir),
  });

  const { data: current } = useQuery<ManuscriptContent>({
    queryKey: ["manuscript", dir, selected],
    queryFn: () => api.manuscript(selected!, dir),
    enabled: !!selected,
  });

  useEffect(() => {
    if (current) {
      setDraft(current.content);
      setDirty(false);
    }
  }, [current]);

  useEffect(() => {
    if (manuscripts && manuscripts.length > 0 && selected === null) {
      setSelected(manuscripts[0].name);
    }
  }, [manuscripts, selected]);

  const saveMutation = useMutation({
    mutationFn: () => api.saveManuscript(selected!, draft, dir),
    onSuccess: (data) => {
      setDirty(false);
      setSaveStatus(`✅ 保存 ${new Date(data.saved_at).toLocaleTimeString()}`);
      setTimeout(() => setSaveStatus(""), 2500);
      qc.invalidateQueries({ queryKey: ["manuscripts", dir] });
      qc.invalidateQueries({ queryKey: ["manuscript", dir, selected] });
    },
    onError: (err) => {
      setSaveStatus(`❌ ${String(err).slice(0, 80)}`);
    },
  });

  // 📣 公開キット: X draft, tags, title options and diagram skeletons for this draft.
  const kitMutation = useMutation({
    mutationFn: () => api.publishKit(draft),
    onSuccess: (data) => setKit(data),
    onError: (err) => setSaveStatus(`❌ 公開キット失敗: ${String(err).slice(0, 120)}`),
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => api.deleteManuscript(name, dir),
    onSuccess: () => {
      setSelected(null);
      setDraft("");
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["manuscripts", dir] });
    },
  });

  // Ctrl+S / Cmd+S to save
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        if (dirty && selected) saveMutation.mutate();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [dirty, selected, saveMutation]);

  function handleNew() {
    const today = new Date().toISOString().slice(0, 10);
    const suggestion = `${today}.md`;
    const name = window.prompt(
      "新規原稿のファイル名 (拡張子.md 必須。例: 2026-09-19.md や 2026-09-19-topic.md)",
      suggestion,
    );
    if (!name) return;
    const trimmed = name.trim();
    if (!trimmed.endsWith(".md")) {
      alert(".md で終わるファイル名を指定してください");
      return;
    }
    const initial = dir === "annex" ? ANNEX_TEMPLATE(trimmed) : `# タイトル\n\n<!-- ${trimmed} -->\n\n`;
    api.saveManuscript(trimmed, initial, dir).then(() => {
      qc.invalidateQueries({ queryKey: ["manuscripts", dir] });
      setSelected(trimmed);
    }).catch((err) => alert(`作成失敗: ${err}`));
  }

  function handleDelete() {
    if (!selected) return;
    if (!window.confirm(`本当に ${selected} を削除しますか？`)) return;
    deleteMutation.mutate(selected);
  }

  const drawerContent = (
    <>
      <div className="p-3 border-b border-slate-200 sticky top-0 bg-white space-y-2">
       <div className="flex gap-1 text-xs">
        {(["manuscripts", "annex"] as const).map((d) => (
          <button
            key={d}
            type="button"
            onClick={() => {
              if (dirty && !window.confirm("未保存の変更があります。破棄しますか？")) return;
              setDir(d);
              setSelected(null);
              setDraft("");
              setDirty(false);
            }}
            className={`px-2 py-1 rounded ${
              dir === d ? "bg-blue-100 text-blue-800 font-medium" : "bg-slate-100 text-slate-600"
            }`}
          >
            {d === "manuscripts" ? "📝 note原稿" : "📚 資料編"}
          </button>
        ))}
       </div>
       <div className="flex items-center gap-2">
        <h2 className="font-semibold text-slate-800 flex-1 text-sm">
          {dir === "annex" ? "documents/annex" : "manuscripts"}
        </h2>
        <button
          type="button"
          onClick={handleNew}
          className="text-xs bg-blue-600 hover:bg-blue-700 text-white px-2 py-1 rounded"
        >
          ＋ 新規
        </button>
        <button
          type="button"
          onClick={() => setDrawerOpen(false)}
          className="md:hidden text-slate-500 hover:text-slate-700 px-1"
          aria-label="close"
        >✕</button>
       </div>
      </div>
      <ul>
        {manuscripts?.map((m) => {
          const label = m.name.replace(/\.md$/, "");
          const active = selected === m.name;
          return (
            <li key={m.name}>
              <button
                type="button"
                onClick={() => {
                  if (dirty && !window.confirm("未保存の変更があります。破棄しますか？")) return;
                  setSelected(m.name);
                  setDrawerOpen(false);
                }}
                className={`w-full text-left px-3 py-2 text-sm border-b border-slate-100 transition ${
                  active
                    ? "bg-blue-50 text-blue-900 font-medium"
                    : "text-slate-700 hover:bg-slate-50"
                }`}
              >
                <div className="truncate">{label}</div>
                <div className="text-xs text-slate-400 mt-0.5">
                  {(m.size_bytes / 1024).toFixed(1)} KB
                </div>
              </button>
            </li>
          );
        })}
        {manuscripts?.length === 0 && (
          <li className="p-3 text-sm text-slate-500">まだ原稿がありません。＋ 新規で作成。</li>
        )}
      </ul>
    </>
  );

  return (
    <div className="h-full flex overflow-hidden relative">
      {/* Mobile drawer overlay */}
      {drawerOpen && (
        <div
          className="md:hidden fixed inset-0 bg-black/40 z-40"
          onClick={() => setDrawerOpen(false)}
        />
      )}
      {/* Drawer / left rail */}
      <aside
        className={`fixed md:static top-0 bottom-0 left-0 z-50 w-64 shrink-0 border-r border-slate-200 bg-white overflow-y-auto transform transition-transform md:transform-none ${
          drawerOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0"
        }`}
      >
        {drawerContent}
      </aside>

      {/* Editor + preview */}
      <section className="flex-1 flex flex-col overflow-hidden bg-slate-50">
        {!selected ? (
          <div className="flex-1 flex flex-col items-center justify-center text-slate-500 gap-3 p-4 text-center">
            <div>原稿を選択するか、＋ 新規で作成してください。</div>
            <button
              type="button"
              onClick={() => setDrawerOpen(true)}
              className="md:hidden text-sm bg-blue-600 hover:bg-blue-700 text-white px-3 py-2 rounded"
            >
              ☰ ファイル一覧を開く
            </button>
          </div>
        ) : (
          <>
            {/* Toolbar */}
            <div className="border-b border-slate-200 bg-white px-2 md:px-4 py-2 flex items-center gap-1.5 md:gap-2 shrink-0 flex-wrap">
              <button
                type="button"
                onClick={() => setDrawerOpen(true)}
                className="md:hidden text-slate-700 hover:bg-slate-100 px-2 py-1 rounded"
                aria-label="ファイル一覧を開く"
              >☰</button>
              <div className="flex gap-0.5">
                <ToolbarButton active={view === "edit"} onClick={() => setView("edit")}>編集</ToolbarButton>
                <ToolbarButton active={view === "split"} onClick={() => setView("split")}>
                  <span className="hidden md:inline">分割</span>
                  <span className="md:hidden">両方</span>
                </ToolbarButton>
                <ToolbarButton active={view === "preview"} onClick={() => setView("preview")}>プレビュー</ToolbarButton>
              </div>
              <div className="flex-1" />
              {dirty && <span className="text-xs text-amber-700">●</span>}
              {saveStatus && <span className="hidden md:inline text-xs text-slate-600">{saveStatus}</span>}
              <button
                type="button"
                onClick={() => saveMutation.mutate()}
                disabled={!dirty || saveMutation.isPending}
                className={`text-sm font-medium px-2 md:px-3 py-1.5 rounded ${
                  dirty
                    ? "bg-blue-600 hover:bg-blue-700 text-white"
                    : "bg-slate-200 text-slate-500 cursor-not-allowed"
                }`}
              >
                💾<span className="hidden md:inline"> 保存 (Ctrl+S)</span>
              </button>
              <CopyButton text={draft} label="📋" />
              <button
                type="button"
                onClick={() => kitMutation.mutate()}
                disabled={kitMutation.isPending || draft.length < 200}
                title="X投稿文・タグ・タイトル案・図解の骨組みを作る"
                className="text-sm bg-slate-100 hover:bg-slate-200 px-2 py-1.5 rounded disabled:opacity-50"
              >
                {kitMutation.isPending ? "…" : "📣"}<span className="hidden md:inline"> 公開キット</span>
              </button>
              <button
                type="button"
                onClick={handleDelete}
                className="text-sm text-red-600 hover:text-red-800 px-2"
                title="削除"
              >
                🗑
              </button>
            </div>

            {kit && <PublishKitPanel kit={kit} onClose={() => setKit(null)} />}

            {/* Editor body */}
            <div className="flex-1 overflow-hidden flex flex-col md:flex-row">
              {(view === "edit" || view === "split") && (
                <textarea
                  value={draft}
                  onChange={(e) => {
                    setDraft(e.target.value);
                    setDirty(true);
                  }}
                  className="flex-1 min-h-0 w-full md:h-full p-3 md:p-6 font-mono text-sm bg-white md:border-r border-b md:border-b-0 border-slate-200 focus:outline-none resize-none leading-6"
                  spellCheck={false}
                  placeholder="ここに書く"
                />
              )}
              {(view === "preview" || view === "split") && (
                <div className="flex-1 min-h-0 w-full md:h-full overflow-y-auto p-3 md:p-6 bg-white">
                  <article className="briefing-prose max-w-none">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{draft}</ReactMarkdown>
                  </article>
                </div>
              )}
            </div>
            <div className="border-t border-slate-200 bg-white px-4 py-1.5 text-xs text-slate-500 flex gap-4 shrink-0">
              <span>{selected}</span>
              <span>{draft.length.toLocaleString()} 字</span>
              <span>{(new Blob([draft]).size / 1024).toFixed(1)} KB</span>
            </div>
          </>
        )}
      </section>
    </div>
  );
}

function ToolbarButton({
  active, onClick, children,
}: {
  active: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`text-xs px-2 py-1 rounded ${
        active ? "bg-slate-800 text-white" : "text-slate-700 hover:bg-slate-100"
      }`}
    >
      {children}
    </button>
  );
}
