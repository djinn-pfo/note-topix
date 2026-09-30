import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ImageAsset } from "../api";

// note's editor accepts a pasted image, so "copy to clipboard" is the fastest
// path from here into a draft. Clipboard image writes are PNG-only, so
// anything else is converted through a canvas first.
async function blobToPng(blob: Blob, scale = 1): Promise<Blob> {
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    await new Promise((resolve, reject) => {
      img.onload = resolve;
      img.onerror = () => reject(new Error("画像を読み込めませんでした"));
      img.src = url;
    });
    const canvas = document.createElement("canvas");
    canvas.width = (img.naturalWidth || 800) * scale;
    canvas.height = (img.naturalHeight || 600) * scale;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("canvas を用意できませんでした");
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    return await new Promise((resolve, reject) =>
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("PNG に変換できませんでした"))), "image/png"),
    );
  } finally {
    URL.revokeObjectURL(url);
  }
}

function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export default function ImagesPage() {
  const qc = useQueryClient();
  const { data: images, isLoading } = useQuery({ queryKey: ["images"], queryFn: api.images });
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "illustration" | "diagram">("all");
  const [status, setStatus] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [svgOpen, setSvgOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = () => qc.invalidateQueries({ queryKey: ["images"] });
  const flash = (msg: string) => {
    setStatus(msg);
    setTimeout(() => setStatus(""), 2500);
  };

  const upload = useMutation({
    mutationFn: (files: File[]) =>
      Promise.all(files.map((f) => api.uploadImage(f, { source: "gemini" }))),
    onSuccess: (saved) => {
      refresh();
      setSelected(saved[saved.length - 1]?.name ?? null);
      flash(`${saved.length} 件を保存しました`);
    },
    onError: (e: Error) => flash(`保存に失敗: ${e.message}`),
  });

  // Paste straight from Gemini: Ctrl+V anywhere on the page.
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const items = Array.from(e.clipboardData?.items ?? []);
      const files = items
        .filter((i) => i.type.startsWith("image/"))
        .map((i) => i.getAsFile())
        .filter((f): f is File => !!f)
        .map((f) => new File([f], f.name || `pasted-${Date.now()}.png`, { type: f.type }));
      if (files.length) {
        e.preventDefault();
        upload.mutate(files);
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [upload]);

  const shown = useMemo(
    () => (images ?? []).filter((i) => filter === "all" || i.kind === filter),
    [images, filter],
  );
  const current = images?.find((i) => i.name === selected) ?? null;

  return (
    <div className="flex h-full overflow-hidden">
      <section className="flex-1 overflow-y-auto p-4">
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <h2 className="font-semibold text-slate-800">🖼️ 画像</h2>
          <div className="flex gap-1 text-xs">
            {(["all", "illustration", "diagram"] as const).map((k) => (
              <button
                key={k}
                onClick={() => setFilter(k)}
                className={`px-2 py-0.5 rounded ${
                  filter === k ? "bg-blue-100 text-blue-800" : "bg-slate-100 text-slate-600"
                }`}
              >
                {k === "all" ? "すべて" : k === "illustration" ? "イラスト" : "図解"}
              </button>
            ))}
          </div>
          <div className="ml-auto flex gap-2">
            <button
              onClick={() => setSvgOpen((v) => !v)}
              className="text-sm bg-slate-100 hover:bg-slate-200 px-3 py-1 rounded"
            >✏️ SVG図解</button>
            <button
              onClick={() => fileRef.current?.click()}
              className="text-sm bg-blue-600 text-white px-3 py-1 rounded hover:bg-blue-700"
            >＋ 追加</button>
          </div>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            multiple
            className="hidden"
            onChange={(e) => {
              const files = Array.from(e.target.files ?? []);
              if (files.length) upload.mutate(files);
              e.target.value = "";
            }}
          />
        </div>

        {status && <div className="mb-3 text-sm text-slate-600">{status}</div>}

        {svgOpen && <SvgEditor onSaved={(n) => { refresh(); setSelected(n); setSvgOpen(false); flash("SVG を保存しました"); }} />}

        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            const files = Array.from(e.dataTransfer.files).filter((f) => f.type.startsWith("image/"));
            if (files.length) upload.mutate(files);
          }}
          className={`border-2 border-dashed rounded-lg p-4 mb-4 text-center text-sm ${
            dragOver ? "border-blue-400 bg-blue-50 text-blue-700" : "border-slate-200 text-slate-400"
          }`}
        >
          {upload.isPending
            ? "アップロード中..."
            : "ここに画像をドラッグ&ドロップ、または Ctrl+V で貼り付け（Gemini からそのまま）"}
        </div>

        {isLoading && <div className="text-slate-500 text-sm">読込中...</div>}
        {!isLoading && shown.length === 0 && (
          <div className="text-slate-500 text-sm">画像がまだありません。</div>
        )}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {shown.map((img) => (
            <button
              key={img.name}
              onClick={() => setSelected(img.name)}
              className={`border rounded-lg overflow-hidden bg-white text-left hover:shadow ${
                selected === img.name ? "border-blue-500 ring-2 ring-blue-200" : "border-slate-200"
              }`}
            >
              <div className="h-28 bg-slate-50 flex items-center justify-center overflow-hidden">
                <img src={img.url} alt={img.name} className="max-h-28 max-w-full object-contain" />
              </div>
              <div className="p-2">
                <div className="text-xs text-slate-700 truncate">{img.name}</div>
                <div className="text-[11px] text-slate-400">
                  {img.kind === "diagram" ? "図解" : "イラスト"}
                  {img.article && ` · ${img.article}`}
                </div>
              </div>
            </button>
          ))}
        </div>
      </section>

      {current && (
        <aside className="w-96 shrink-0 border-l border-slate-200 bg-white overflow-y-auto">
          <ImageDetail
            image={current}
            onClose={() => setSelected(null)}
            onChanged={refresh}
            flash={flash}
          />
        </aside>
      )}
    </div>
  );
}

function ImageDetail({
  image, onClose, onChanged, flash,
}: {
  image: ImageAsset;
  onClose: () => void;
  onChanged: () => void;
  flash: (m: string) => void;
}) {
  const [prompt, setPrompt] = useState(image.prompt);
  const [article, setArticle] = useState(image.article);
  const [scale, setScale] = useState(2);

  useEffect(() => {
    setPrompt(image.prompt);
    setArticle(image.article);
  }, [image.name, image.prompt, image.article]);

  const isSvg = image.name.toLowerCase().endsWith(".svg");
  const markdownRef = `![${image.prompt ? image.prompt.slice(0, 30) : image.name}](${image.url})`;

  const save = useMutation({
    mutationFn: () => api.updateImage(image.name, { prompt, article }),
    onSuccess: () => { onChanged(); flash("保存しました"); },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteImage(image.name),
    onSuccess: () => { onChanged(); onClose(); flash("削除しました"); },
  });

  const copyImage = async () => {
    try {
      const blob = await (await fetch(image.url)).blob();
      const png = blob.type === "image/png" && !isSvg ? blob : await blobToPng(blob, isSvg ? scale : 1);
      await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
      flash("画像をコピーしました。note の編集画面に貼り付けられます");
    } catch (e) {
      flash(`コピーできませんでした（ダウンロードを使ってください）: ${(e as Error).message}`);
    }
  };

  const downloadPng = async () => {
    const blob = await (await fetch(image.url)).blob();
    const png = isSvg || blob.type !== "image/png" ? await blobToPng(blob, isSvg ? scale : 1) : blob;
    download(png, image.name.replace(/\.[^.]+$/, "") + ".png");
  };

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-start gap-2">
        <h3 className="font-semibold text-slate-900 break-all flex-1">{image.name}</h3>
        <button onClick={onClose} className="text-slate-500 hover:text-slate-800 px-1">✕</button>
      </div>

      <div className="bg-slate-50 rounded border border-slate-200 p-2 flex items-center justify-center">
        <img src={image.url} alt={image.name} className="max-h-64 max-w-full object-contain" />
      </div>

      <div className="flex flex-wrap gap-2">
        <button onClick={copyImage} className="text-sm bg-blue-600 text-white px-3 py-1.5 rounded hover:bg-blue-700">
          📋 画像をコピー
        </button>
        <button onClick={downloadPng} className="text-sm bg-slate-100 px-3 py-1.5 rounded hover:bg-slate-200">
          ⬇ PNG保存
        </button>
        <a
          href={image.url}
          download={image.name}
          className="text-sm bg-slate-100 px-3 py-1.5 rounded hover:bg-slate-200"
        >⬇ 原本</a>
      </div>

      {isSvg && (
        <label className="block text-xs text-slate-600">
          PNG 書き出し倍率: {scale}x
          <input
            type="range" min={1} max={4} step={1} value={scale}
            onChange={(e) => setScale(parseInt(e.target.value))}
            className="w-full"
          />
        </label>
      )}

      <div>
        <label className="text-xs font-medium text-slate-600 block mb-1">生成プロンプト</label>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={5}
          placeholder="Gemini に投げたプロンプトを貼っておくと、後で作り直せます"
          className="w-full border border-slate-300 rounded p-2 text-xs font-mono"
        />
        <button
          onClick={() => navigator.clipboard.writeText(prompt).then(() => flash("プロンプトをコピーしました"))}
          className="mt-1 text-xs bg-slate-100 px-2 py-0.5 rounded hover:bg-slate-200"
        >📋 プロンプトをコピー</button>
      </div>

      <div>
        <label className="text-xs font-medium text-slate-600 block mb-1">資料編に貼る Markdown</label>
        <code className="block bg-slate-50 border border-slate-200 rounded p-2 text-[11px] break-all">
          {markdownRef}
        </code>
        <button
          onClick={() => navigator.clipboard.writeText(markdownRef).then(() => flash("Markdown をコピーしました"))}
          className="mt-1 text-xs bg-slate-100 px-2 py-0.5 rounded hover:bg-slate-200"
        >📋 Markdown をコピー</button>
        <p className="text-[11px] text-slate-400 mt-1">
          資料編（documents/annex）の本文に貼ると、プレビューにも公開サイトにも表示されます。
          note 本文には「📋 画像をコピー」で直接貼り付けてください。
        </p>
      </div>

      <div>
        <label className="text-xs font-medium text-slate-600 block mb-1">関連する記事 / 資料編</label>
        <input
          value={article}
          onChange={(e) => setArticle(e.target.value)}
          className="w-full border border-slate-300 rounded px-2 py-1 text-sm"
        />
      </div>

      <div className="text-[11px] text-slate-400">
        {image.source && `source: ${image.source} · `}
        {Math.round(image.size_bytes / 1024)} KB
        {image.created_at && ` · ${image.created_at.slice(0, 10)}`}
      </div>

      <div className="flex gap-2 pt-2 border-t border-slate-200">
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending}
          className="text-sm bg-blue-600 text-white px-3 py-1.5 rounded hover:bg-blue-700 disabled:opacity-50"
        >保存</button>
        <button
          onClick={() => { if (confirm(`${image.name} を削除しますか？`)) remove.mutate(); }}
          className="text-sm text-red-700 bg-red-50 px-3 py-1.5 rounded hover:bg-red-100 ml-auto"
        >🗑 削除</button>
      </div>
    </div>
  );
}

// Structure diagrams (boxes, arrows, labels) come out cleaner as SVG than as a
// generated image: text stays sharp and it can be edited afterwards.
function SvgEditor({ onSaved }: { onSaved: (name: string) => void }) {
  const [name, setName] = useState("diagram.svg");
  const [svg, setSvg] = useState(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 200" width="640" height="200">\n' +
      '  <rect width="640" height="200" fill="#ffffff"/>\n' +
      '  <rect x="24" y="60" width="180" height="70" rx="10" fill="#e0e7ff" stroke="#4f46e5"/>\n' +
      '  <text x="114" y="100" text-anchor="middle" font-size="16" fill="#1e1b4b">入力</text>\n' +
      '</svg>',
  );
  const [note, setNote] = useState("");
  const save = useMutation({
    mutationFn: () => api.saveSvg({ name, svg, prompt: note }),
    onSuccess: (saved) => onSaved(saved.name),
  });

  return (
    <div className="border border-slate-200 rounded-lg p-3 mb-4 bg-slate-50">
      <div className="flex gap-2 items-center mb-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="border border-slate-300 rounded px-2 py-1 text-sm"
        />
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending}
          className="text-sm bg-blue-600 text-white px-3 py-1 rounded hover:bg-blue-700 disabled:opacity-50"
        >保存</button>
        <span className="text-xs text-slate-500">
          保存後、一覧から PNG 書き出し・コピーができます
        </span>
      </div>
      <div className="grid md:grid-cols-2 gap-2">
        <textarea
          value={svg}
          onChange={(e) => setSvg(e.target.value)}
          rows={12}
          className="w-full border border-slate-300 rounded p-2 text-xs font-mono"
        />
        <div
          className="bg-white border border-slate-200 rounded p-2 overflow-auto flex items-center justify-center"
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>
      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="この図の説明 / 作成メモ"
        className="mt-2 w-full border border-slate-300 rounded px-2 py-1 text-sm"
      />
    </div>
  );
}
