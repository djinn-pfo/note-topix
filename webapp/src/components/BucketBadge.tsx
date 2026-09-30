const BUCKET_COLORS: Record<string, string> = {
  academic: "bg-indigo-100 text-indigo-800",
  hn: "bg-orange-100 text-orange-800",
  "jp-tech": "bg-teal-100 text-teal-800",
  "jp-social": "bg-pink-100 text-pink-800",
  "general-jp": "bg-yellow-100 text-yellow-800",
  curated: "bg-purple-100 text-purple-800",
};

const SOURCE_LABELS: Record<string, string> = {
  hackernews: "🟧 HN",
  arxiv: "📄 arXiv",
  zenn: "📗 Zenn",
  qiita: "📘 Qiita",
  bluesky: "🦋 Bluesky",
  google_trends: "🔎 GTrends",
  raindrop: "💧 X-share",
};

export function BucketBadge({ bucket }: { bucket: string }) {
  const cls = BUCKET_COLORS[bucket] ?? "bg-slate-100 text-slate-800";
  return (
    <span className={`inline-block text-xs font-medium px-2 py-0.5 rounded ${cls}`}>
      {bucket}
    </span>
  );
}

export function SourceBadge({ source }: { source: string }) {
  return (
    <span className="inline-block text-xs font-medium text-slate-700">
      {SOURCE_LABELS[source] ?? source}
    </span>
  );
}
