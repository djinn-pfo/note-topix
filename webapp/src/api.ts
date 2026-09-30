// API client — Vite dev server proxies /api to FastAPI.
// In production this same-origin assumption still holds if you serve the
// built webapp from FastAPI.

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init);
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`${resp.status} ${resp.statusText}: ${text.slice(0, 200)}`);
  }
  return resp.json();
}

export type Meta = {
  last_run_at: string | null;
  date_min: string | null;   // earliest signal publication date (YYYY-MM-DD)
  date_max: string | null;
  signal_count: number;
  bucket_counts: Record<string, number>;
  source_counts: Record<string, number>;
  classifications: { total: number; by_label: Record<string, number> };
};

export type Classification = {
  label: string;
  confidence: number | null;
  rationale: string | null;
};

export type TermStats = {
  term: string;
  occurrence: number;
  engagement_sum: number;
  buckets: string[];
  classification: Classification | null;
  note_coverage_total: number | null;
};

export type GraphNode = {
  id: string;
  occurrence: number;
  engagement_sum: number;
  buckets: string[];
  dominant_bucket: string;
  classification: Classification | null;
  community: number | null;
  aliases?: string[];      // buffer method: variants merged into this core
};

export type GraphEdge = {
  source: string;
  target: string;
  weight: number;
  shared?: string[];       // buffer method: top shared buffer features ("v:" = verb)
  co_mentions?: number;    // buffer method: signals mentioning both cores
};

export type BufferProfile = {
  term: string;
  stats: {
    occurrence: number;
    mentions: number;
    buckets: string[];
    aliases: string[];
    label: string;
  };
  profile: { feature: string; ppmi: number; count: number }[];
  neighbors: { term: string; weight: number; shared: string[]; co_mentions: number }[];
};

// Inclusive publication-date range; omitted bounds are open.
export type DateRange = {
  date_from?: string;
  date_to?: string;
};

function appendRange(q: URLSearchParams, range?: DateRange) {
  if (range?.date_from) q.set("date_from", range.date_from);
  if (range?.date_to) q.set("date_to", range.date_to);
}

export type BufferParams = {
  window: number;
  include_verbs: boolean;
  label_filter?: string[];
};

export type CommunityName = {
  name: string;
  description: string | null;
  top_verbs: [string, number][];
  buffers?: string[];      // buffer method: shared nouns that tie the members together
};

export type GraphPayload = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  communities: Record<string, CommunityName>;
  config: Record<string, unknown>;
};

export type Signal = {
  signal_id: number;
  path: string;
  source: string;
  bucket: string;
  title: string;
  url: string;
  body: string;
  date: string;
  engagement: number;
};

export type Community = {
  name: string;
  description: string | null;
  members: string[];
  top_verbs: [string, number][];
  config: Record<string, unknown>;
  created_at: string;
};

export type BriefingListItem = {
  name: string;
  size_bytes: number;
  modified_at: number;
};

export type BriefingContent = {
  name: string;
  content: string;
  body: string;
  frontmatter: Record<string, unknown>;
};

export type NoteCoverage = {
  total: number | null;
  rounded: number | null;
  top_titles: string[];
  fetched_at: string;
};

// The editor writes either note drafts or annex sources (published to GitHub Pages).
export type EditDir = "manuscripts" | "annex";

export type ImageAsset = {
  name: string;
  url: string;
  size_bytes: number;
  modified_at: number;
  kind: string;          // illustration | diagram
  prompt: string;
  source: string;        // gemini / claude-svg / hand ...
  article: string;
  created_at: string;
};

export type Article = {
  article_id: number;
  note_key: string | null;
  url: string | null;
  title: string;
  published_on: string | null;
  kind: string | null;
  topic: string | null;
  briefing: string | null;
  annex_slug: string | null;
  price: number;
  memo: string | null;
  likes: number | null;
  comments: number | null;
  stats_fetched_at: string | null;
  likes_24h_ago: number | null;
};

export type ChallengeProgress = {
  published: number;
  posted_days: number;
  streak: number;
  start: string | null;
  day_number: number;
  remaining: number;
  today_done: boolean;
  missing_days: string[];
  by_kind: Record<string, number>;
  total_articles?: number;
};

export type ChallengeData = {
  progress: ChallengeProgress;
  articles: Article[];
  kinds: string[];
  states: string[];
};

export type PublishKit = {
  x_post?: string;
  tags?: string[];
  title_candidates?: string[];
  diagrams?: { title: string; why: string; svg: string }[];
};

export type BacklogEntry = {
  briefing: string;
  title: string;
  date: string;
  signal_count: number | null;
  why_questions: string[];
  state: string;
  note_kind: string | null;
  memo: string | null;
  article_id: number | null;
  last_seen: string | null;
  days_since_seen: number | null;
  fresh: boolean;
  lineage_runs: number | null;
  signal_growth: number | null;
};

export const api = {
  challenge: () => json<ChallengeData>("/api/challenge"),
  refreshArticles: () =>
    json<{ progress: ChallengeProgress; articles: Article[] }>("/api/articles/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }),
  updateArticle: (id: number, fields: Partial<Pick<Article, "kind" | "topic" | "briefing" | "memo">>) =>
    json<Article>(`/api/articles/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    }),
  articleStats: (id: number) =>
    json<{ fetched_at: string; likes: number; comments: number }[]>(`/api/articles/${id}/stats`),
  publishKit: (content: string) =>
    json<PublishKit>("/api/publish-kit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    }),
  backlog: () => json<BacklogEntry[]>("/api/backlog"),
  updateBacklog: (payload: { briefing: string; state: string; note_kind?: string; memo?: string }) =>
    json<BacklogEntry>("/api/backlog", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  meta: () => json<Meta>("/api/meta"),
  terms: (params: { mode: string; min_occurrence?: number; limit?: number }) => {
    const q = new URLSearchParams({
      mode: params.mode,
      min_occurrence: String(params.min_occurrence ?? 1),
      limit: String(params.limit ?? 500),
    });
    return json<TermStats[]>(`/api/terms?${q}`);
  },
  graph: (params: {
    mode: string;
    method: string;
    min_occurrence: number;
    min_weight: number;
    max_nodes: number;
    community: boolean;
    label_filter?: string[];
    buffer?: BufferParams;
    range?: DateRange;
  }) => {
    const q = new URLSearchParams({
      mode: params.mode,
      method: params.method,
      min_occurrence: String(params.min_occurrence),
      min_weight: String(params.min_weight),
      max_nodes: String(params.max_nodes),
      community: String(params.community),
    });
    for (const label of params.label_filter ?? []) q.append("label_filter", label);
    if (params.buffer) {
      q.set("window", String(params.buffer.window));
      q.set("include_verbs", String(params.buffer.include_verbs));
    }
    appendRange(q, params.range);
    return json<GraphPayload>(`/api/graph?${q}`);
  },
  bufferProfile: (term: string, params: BufferParams, range?: DateRange) => {
    const q = new URLSearchParams({
      term,
      window: String(params.window),
      include_verbs: String(params.include_verbs),
    });
    for (const label of params.label_filter ?? []) q.append("label_filter", label);
    appendRange(q, range);
    return json<BufferProfile>(`/api/buffer-profile?${q}`);
  },
  signals: (params: {
    term: string; mode: string; limit?: number; sort?: "engagement" | "date"; range?: DateRange;
  }) => {
    const q = new URLSearchParams({
      term: params.term,
      mode: params.mode,
      limit: String(params.limit ?? 30),
      sort: params.sort ?? "engagement",
    });
    appendRange(q, params.range);
    return json<Signal[]>(`/api/signals?${q}`);
  },
  verbsForTerm: (params: { term: string; mode: string; limit?: number; range?: DateRange }) => {
    const q = new URLSearchParams({
      term: params.term,
      mode: params.mode,
      limit: String(params.limit ?? 20),
    });
    appendRange(q, params.range);
    return json<{ verb: string; count: number }[]>(`/api/verbs-for-term?${q}`);
  },
  communities: () => json<Community[]>("/api/communities"),
  noteCoverage: () => json<Record<string, NoteCoverage>>("/api/note-coverage"),
  noteCoverageOne: (term: string) =>
    json<NoteCoverage>(`/api/note-coverage/${encodeURIComponent(term)}`),
  classifications: () => json<Record<string, Classification>>("/api/classifications"),
  briefings: () => json<BriefingListItem[]>("/api/briefings"),
  briefing: (name: string) =>
    json<BriefingContent>(`/api/briefings/${encodeURIComponent(name)}`),
  manuscripts: (dir: EditDir = "manuscripts") =>
    json<ManuscriptListItem[]>(`/api/manuscripts?dir=${dir}`),
  manuscript: (name: string, dir: EditDir = "manuscripts") =>
    json<ManuscriptContent>(`/api/manuscripts/${encodeURIComponent(name)}?dir=${dir}`),
  saveManuscript: (name: string, content: string, dir: EditDir = "manuscripts") =>
    json<{ name: string; size_bytes: number; saved_at: string }>(
      `/api/manuscripts/${encodeURIComponent(name)}?dir=${dir}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      },
    ),
  images: () => json<ImageAsset[]>("/api/images"),
  uploadImage: (file: File, meta: { prompt?: string; source?: string; article?: string; kind?: string }) => {
    const form = new FormData();
    form.append("file", file);
    form.append("prompt", meta.prompt ?? "");
    form.append("source", meta.source ?? "gemini");
    form.append("article", meta.article ?? "");
    form.append("kind", meta.kind ?? "");
    return json<ImageAsset>("/api/images", { method: "POST", body: form });
  },
  saveSvg: (payload: {
    name: string; svg: string; prompt?: string; article?: string; overwrite?: boolean;
  }) =>
    json<ImageAsset>("/api/images/svg", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  svgSource: (name: string) =>
    json<ImageAsset & { svg: string }>(`/api/images/svg/${encodeURIComponent(name)}`),
  updateImage: (name: string, meta: Partial<Pick<ImageAsset, "prompt" | "source" | "article" | "kind">>) =>
    json<ImageAsset>(`/api/images/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(meta),
    }),
  deleteImage: (name: string) =>
    json<{ name: string; deleted: boolean }>(`/api/images/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  deleteManuscript: (name: string, dir: EditDir = "manuscripts") =>
    json<{ name: string; deleted: boolean }>(
      `/api/manuscripts/${encodeURIComponent(name)}?dir=${dir}`,
      { method: "DELETE" },
    ),
};

export type ManuscriptListItem = {
  name: string;
  size_bytes: number;
  modified_at: number;
};

export type ManuscriptContent = {
  name: string;
  content: string;
  size_bytes: number;
  modified_at: number;
};
