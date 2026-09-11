/**
 * Client HTTP typé.
 *
 * L'authentification passe par un cookie httpOnly posé par l'API : aucun jeton
 * n'est stocké côté JavaScript, donc une faille XSS ne permet pas de le voler.
 * `credentials: "include"` est donc obligatoire sur chaque appel.
 */

export type Role = "admin" | "reviewer";

export interface User {
  id: string;
  email: string;
  role: Role;
}

export type RunStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "QUARANTINE"
  | "FAILED";

export type StepStatus = "PENDING" | "RUNNING" | "DONE" | "SKIPPED" | "FAILED";

export interface Step {
  step: string;
  position: number;
  status: StepStatus;
  rows_in: number;
  rows_out: number;
  duration_ms: number;
  corrections: number;
  anomalies: number;
  counters: Record<string, number>;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/** 'live' : reponse immediate, plein tarif. 'batch' : asynchrone, moitie prix. */
export type LlmMode = "live" | "batch";

export interface Run {
  id: string;
  store_id: string;
  file_name: string;
  file_sha256: string;
  status: RunStatus;
  current_step: string | null;
  progress: number;
  rows_in: number;
  rows_out: number;
  publishable_rate: number;
  error: string | null;
  network_enabled?: boolean;
  llm_mode?: LlmMode;
  triggered_by: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface RunDetail extends Run {
  steps: Step[];
  report: {
    llm_usage?: import("../components/LlmUsage").LlmUsageData;
    llm_budget?: LlmBudget;
    // Les FICHES publiables, comptées après regroupement — à ne pas confondre
    // avec `publishable_rate`, qui porte sur les lignes reçues.
    records_total?: number;
    records_publishable?: number;
    anomalies_by_code?: Record<string, number>;
    corrections_by_author?: Record<string, number>;
    // Les décisions, distinctes des lignes corrigées ci-dessus : une réponse
    // du LLM sur un libellé distinct vaut une décision, quel que soit le
    // nombre de lignes qui portent ce libellé.
    decisions_by_author?: Record<string, number>;
    quarantined?: boolean;
    quarantine_reason?: string | null;
    extra?: Record<string, unknown>;
  };
}

export interface LlmBudget {
  limit_usd: number;
  spent_usd: number;
  remaining_usd: number;
  used_ratio: number;
  exhausted: boolean;
  runs_charged: number;
  currency: string;
  note?: string;
}

export interface PipelineStep {
  key: string;
  label: string;
  family: "rules" | "semantic" | "human";
  description: string;
}

export interface Anomaly {
  row_id: string;
  field_name: string;
  code: string;
  severity: "INFO" | "WARNING" | "ERROR";
  detail: string;
  /** La valeur fautive, et le libellé de la ligne : sans eux, cent anomalies
   *  d'un même code sont cent lignes identiques. */
  value?: string;
  label?: string;
}

export interface Correction {
  row_id: string;
  field_name: string;
  old_value: string | null;
  new_value: string | null;
  author: "RULE" | "LLM" | "HUMAN";
  rule: string;
  confidence: number;
  created_at: string;
}

export interface StoreCatalog {
  store_id: string;
  products: number;
  publishable: number;
  source_rows: number;
  last_run_at: string | null;
}

export interface RunSummary {
  corrections_by_author: Record<string, number>;
  decisions_by_author?: Record<string, number>;
  records_total: number;
  records_publishable: number;
}

export interface Merge {
  id: string;
  store_id: string;
  survivor_id: string;
  survivor_label: string;
  absorbed_label: string;
  created_at: string;
}

export interface SourceRow {
  row_id: string;
  source_label: string;
  run_id: string;
}

/** Un champ corrigé à la main, qui doit tenir au dépôt suivant. */
export interface Override {
  id: string;
  store_id: string;
  product_key: string;
  field_name: string;
  previous_value: string | null;
  value: string | null;
  created_at: string;
}

export interface Product {
  id: string;
  store_id: string;
  label: string;
  label_normalized: string;
  label_enriched: string | null;
  ean: string | null;
  internal_code: string | null;
  url_image: string | null;
  url_ok: boolean;
  url_checked?: boolean;
  taxonomy_1: string | null;
  taxonomy_2: string | null;
  taxonomy_3: string | null;
  taxonomy_4: string | null;
  vat_rate: number | null;
  brand: string | null;
  quantity_value: number | null;
  quantity_unit: string | null;
  status: string;
  publishable: boolean;
  source_rows: number;
}

export interface ReviewTask {
  id: string;
  run_id: string;
  store_id: string;
  kind: string;
  field_name: string;
  title: string;
  question: string;
  current_value: string | null;
  proposed_value: string | null;
  source: "RULE" | "LLM" | "HUMAN";
  confidence: number;
  affected_rows: number;
  context: {
    distribution?: Record<string, number>;
    total_rows?: number;
    taxonomy?: string[];
    ean?: string | null;
    sample_row_ids?: string[];
    // Renseignes uniquement pour un doublon possible.
    left_key?: string;
    right_key?: string;
    left_label?: string;
    right_label?: string;
    score?: number;
  };
  status: string;
  requires_admin: boolean;
  created_at: string;
}

/** Une ligne du journal d'audit, telle qu'elle s'affiche. */
export interface AuditEntry {
  id: string;
  run_id: string;
  store_id: string | null;
  product_label: string | null;
  row_id: string;
  field_name: string;
  old_value: string | null;
  new_value: string | null;
  author: "RULE" | "LLM" | "HUMAN";
  rule: string;
  confidence: number;
  user_email: string | null;
  created_at: string;
}

export interface Metrics {
  runs_total: number;
  runs_last_7d: number;
  products_total: number;
  publishable_rate: number;
  pending_reviews: number;
  learned_rules: number;
  trend: Array<{
    run_id: string;
    store_id: string;
    started_at: string;
    publishable_rate: number;
    rows_in: number;
  }>;
  anomalies_by_code: Record<string, number>;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { credentials: "include", ...init });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* corps non JSON : on garde le statusText */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  login: (email: string, password: string) =>
    request<User>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  me: () => request<User>("/api/auth/me"),

  pipeline: () => request<PipelineStep[]>("/api/pipeline"),
  llmBudget: () => request<LlmBudget>("/api/llm/budget"),

  runs: () => request<Run[]>("/api/runs"),
  run: (id: string) => request<RunDetail>(`/api/runs/${id}`),
  anomalies: (id: string, code?: string) =>
    request<Anomaly[]>(
      `/api/runs/${id}/anomalies${code ? `?code=${encodeURIComponent(code)}` : ""}`,
    ),
  /**
   * L'état COURANT du run : fiches publiables et corrections par auteur.
   * Le rapport, lui, est figé à la fin du traitement — l'étage humain y vaut
   * toujours zéro, et les fiches qu'une validation vient de débloquer y
   * restent rejetées. Ce comptage lit la même table que le CSV exporté.
   */
  runSummary: (id: string) => request<RunSummary>(`/api/runs/${id}/summary`),
  corrections: (id: string, field?: string) =>
    request<Correction[]>(
      `/api/runs/${id}/corrections${field ? `?field=${encodeURIComponent(field)}` : ""}`,
    ),
  exportUrl: (id: string) => `/api/runs/${id}/export.csv`,
  sourceUrl: (id: string) => `/api/runs/${id}/source.csv`,
  deleteRun: (id: string) => request<void>(`/api/runs/${id}`, { method: "DELETE" }),

  upload: (file: File, storeId: string, checkUrls: boolean, llmMode: LlmMode) => {
    const form = new FormData();
    form.append("file", file);
    form.append("store_id", storeId);
    form.append("check_urls", String(checkUrls));
    form.append("llm_mode", llmMode);
    return request<Run>("/api/runs", { method: "POST", body: form });
  },

  reviewTasks: (statusFilter = "pending") =>
    request<ReviewTask[]>(`/api/review/tasks?status_filter=${statusFilter}`),
  decide: (taskId: string, action: "approve" | "reject" | "edit", value?: string) =>
    request<ReviewTask>(`/api/review/tasks/${taskId}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, value }),
    }),

  stores: () => request<StoreCatalog[]>("/api/stores"),

  /**
   * « Ces fiches sont le même produit. » C'est une décision, pas une
   * correction : elle est tracée avec son auteur et rejouée au dépôt suivant.
   */
  mergeProducts: (productIds: string[]) =>
    request<Product>("/api/products/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product_ids: productIds }),
    }),
  merges: (storeId?: string) =>
    request<Merge[]>(
      `/api/merges${storeId ? `?store_id=${encodeURIComponent(storeId)}` : ""}`,
    ),
  undoMerge: (mergeId: string) =>
    request<Product>(`/api/merges/${mergeId}/undo`, { method: "POST" }),

  updateProduct: (productId: string, fieldName: string, value: string | null) =>
    request<Product>(`/api/products/${productId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ field_name: fieldName, value }),
    }),

  overrides: (productId: string) =>
    request<Override[]>(`/api/products/${productId}/overrides`),

  undoOverride: (productId: string, fieldName: string) =>
    request<Product>(`/api/products/${productId}/overrides/${fieldName}`, {
      method: "DELETE",
    }),

  sourceRows: (productId: string) =>
    request<SourceRow[]>(`/api/products/${productId}/source-rows`),

  products: (
    params: { q?: string; only_publishable?: boolean; store_id?: string } = {},
  ) => {
    const query = new URLSearchParams();
    if (params.q) query.set("q", params.q);
    if (params.store_id) query.set("store_id", params.store_id);
    if (params.only_publishable) query.set("only_publishable", "true");
    return request<Product[]>(`/api/products?${query}`);
  },

  audit: (params: { author?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.author) query.set("author", params.author);
    query.set("limit", String(params.limit ?? 40));
    return request<AuditEntry[]>(`/api/audit?${query}`);
  },

  metrics: () => request<Metrics>("/api/metrics"),
};

/** Flux de progression d'un run (Server-Sent Events). */
export function subscribeToRun(
  runId: string,
  onEvent: (event: Record<string, unknown>) => void,
): () => void {
  const source = new EventSource(`/api/events/runs/${runId}`, {
    withCredentials: true,
  });
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data) as Record<string, unknown>);
    } catch {
      /* commentaire de maintien de connexion */
    }
  };
  // Le navigateur reconnecte tout seul ; on ne ferme que sur demande explicite.
  return () => source.close();
}
