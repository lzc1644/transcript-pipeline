/** Library API. Clients submit object IDs, never server-side paths. */
export interface LibraryBook {
  id: string; title: string; author: string; edition: string; tags: string[];
  page_count: number; ready_pages: number; recognized_pages: number; edited_pages: number; failed_pages: number;
  version: number; archived: boolean; updated_at: string; duplicate?: boolean;
  state?: "ready" | "deleting"; cleanup_error?: string;
}
export interface LibraryPage {
  page_number: number; text: string | null; current_revision_id: string | null;
  source_kind: string | null; ocr_status: string; error: string | null;
}
export interface PageRevision {
  id: string; text: string; source_kind: string; created_at: string; note?: string;
}
export interface LibraryRange { book_id: string; start_page: number; end_page: number }
export interface LibraryMaterial {
  id: string; name: string; version: number; ranges: LibraryRange[]; page_count: number;
  ready_pages: number; missing_pages: number; archived: boolean;
  state?: "ready" | "deleting"; cleanup_error?: string;
  missing_page_items: { book_id: string; page_number: number }[];
  duplicate_pages: { book_id: string; page_number: number; count: number }[];
}
export interface LibraryRevision {
  id: string; material_id: string; revision: number; name: string; page_count: number;
  book_count: number; status: string; created_at: string; has_updates: boolean; error?: string;
}
export interface LibraryJob {
  id: string; kind: "ocr" | "import" | "material"; book_id?: string; name?: string;
  status: string; total: number; completed: number; total_pages?: number; completed_pages?: number;
  failed_pages?: number; errors?: Record<string, string>; error: string; created_at: string;
}
export interface HistoryCandidate {
  id: string; task_id: string; source_file: string; title: string; page_count: number;
  reusable_pages: number; warnings: string[]; conflicting_pages: number[];
}
export interface ImportResult {
  status: string; book_id: string; imported_pages: number; warnings: string[]; error: string;
}
export interface Items<T> { items: T[]; total: number }
export interface OCRSettings {
  model?: string; reasoning_effort?: string; fast_mode?: boolean; max_concurrency?: number;
  submit_interval_seconds?: number;
}

export class LibraryApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/library${path}`, init);
  if (!response.ok) {
    const body = await response.text();
    let message = body;
    try {
      const value = JSON.parse(body) as { detail?: unknown };
      message = typeof value.detail === "string" ? value.detail : JSON.stringify(value.detail ?? value);
    } catch { /* Keep the response body if it is not JSON. */ }
    throw new LibraryApiError(message || `请求失败：HTTP ${response.status}`, response.status);
  }
  return response.json() as Promise<T>;
}
function json(method: string, payload: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) };
}
const enc = encodeURIComponent;
export const library = {
  listBooks: (q = "", page = 1, archived = false) => request<Items<LibraryBook>>(`/books?${new URLSearchParams({ q, page: String(page), page_size: "20", archived: String(archived) })}`),
  getBook: (id: string) => request<LibraryBook>(`/books/${enc(id)}`),
  uploadBook: (file: File) => request<LibraryBook>(`/books?filename=${enc(file.name)}`, { method: "POST", headers: { "Content-Type": "application/pdf" }, body: file }),
  updateBook: (book: LibraryBook) => request<LibraryBook>(`/books/${enc(book.id)}`, json("PATCH", { base_version: book.version, title: book.title, author: book.author, edition: book.edition, tags: book.tags })),
  archiveBook: (book: LibraryBook, archived: boolean) => request<LibraryBook>(`/books/${enc(book.id)}`, json("PATCH", { base_version: book.version, archived })),
  purgeBook: (id: string) => request<unknown>(`/books/${enc(id)}?purge=true`, { method: "DELETE" }),
  getPage: (id: string, page: number) => request<LibraryPage>(`/books/${enc(id)}/pages/${page}`),
  previewUrl: (id: string, page: number) => `/api/library/books/${enc(id)}/pages/${page}/preview`,
  pageHistory: (id: string, page: number) => request<Items<PageRevision>>(`/books/${enc(id)}/pages/${page}/history`),
  savePage: (id: string, page: number, text: string, baseRevision: string | null, note: string) => request<LibraryPage>(`/books/${enc(id)}/pages/${page}/text`, json("PUT", { text, base_revision_id: baseRevision, note })),
  startOCR: (id: string, pages: number[] | null, settings: OCRSettings) => request<LibraryJob>(`/books/${enc(id)}/ocr-jobs`, json("POST", { pages, ...settings })),
  jobs: () => request<Items<LibraryJob>>("/jobs"),
  clearPreviews: () => request<{ removed_files: number; removed_bytes: number }>("/cache/previews", { method: "DELETE" }),
  resume: (id: string) => request<LibraryJob>(`/jobs/${enc(id)}/resume`, json("POST", {})),
  materials: (archived = false) => request<Items<LibraryMaterial>>(`/materials?archived=${archived}`),
  getMaterial: (id: string) => request<LibraryMaterial>(`/materials/${enc(id)}`),
  createMaterial: (name: string, ranges: LibraryRange[]) => request<LibraryMaterial>("/materials", json("POST", { name, ranges })),
  updateMaterial: (material: LibraryMaterial, name: string, ranges: LibraryRange[]) => request<LibraryMaterial>(`/materials/${enc(material.id)}`, json("PATCH", { base_version: material.version, name, ranges })),
  archiveMaterial: (material: LibraryMaterial, archived: boolean) => request<LibraryMaterial>(`/materials/${enc(material.id)}`, json("PATCH", { base_version: material.version, archived })),
  purgeMaterial: (id: string) => request<unknown>(`/materials/${enc(id)}?purge=true`, { method: "DELETE" }),
  revisions: (id: string) => request<Items<LibraryRevision>>(`/materials/${enc(id)}/revisions`),
  publish: (material: LibraryMaterial) => request<LibraryRevision>(`/materials/${enc(material.id)}/revisions`, json("POST", { base_version: material.version })),
  revisionUrl: (id: string, kind: "pdf" | "txt" | "sources") => `/api/library/revisions/${enc(id)}/${kind}`,
  candidates: () => request<Items<HistoryCandidate>>("/imports"),
  importHistory: (candidateIds: string[]) => request<Items<ImportResult>>("/imports", json("POST", { candidate_ids: candidateIds })),
};
