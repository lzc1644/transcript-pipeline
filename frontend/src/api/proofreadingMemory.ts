import { requestJson } from "./client";

const base = "/api/proofreading-memory";
export type MemoryRole = "asr" | "system" | "human" | "reference";
export type MemoryStatus = "pending" | "approved" | "rejected" | "disabled";
export interface MemoryMetadata {
  dataset_kind: "real_human" | "synthetic";
  scope: { book_id: string; speaker_id: string; content_type: "reading" | "conversation" };
}
export interface MemorySource {
  text: string; path: string; sha256: string;
}
export interface MemoryFragment {
  fragment_id: string; kind: string; system: [number, number]; human: [number, number];
  asr_alignment: { method: string; span?: [number, number] };
}
export interface MemoryCoverage {
  total_changes: number; selected_ids: string[]; omitted_ids: string[]; partial: boolean;
  preservation_sampling: string;
}
export interface MemoryRequest {
  request_id: string; metadata: MemoryMetadata; coverage: MemoryCoverage; fragments: MemoryFragment[];
  sources: Record<"asr" | "system" | "human", MemorySource> & { reference?: MemorySource };
}
export interface MemoryExperiment {
  id: string; request: MemoryRequest; analyses?: MemoryAnalysis[];
}
export interface MemoryHistory {
  id: string; metadata: MemoryMetadata; coverage: MemoryCoverage;
}
export interface MemoryEvidence {
  source_id: string; start: number; end: number; excerpt: string; pdf_page: number[] | null;
  source_sha256: string; page_note: string;
}
export interface MemoryCandidate {
  kind: string; text: string; reason: string; retrieval_keys: string[]; request_id: string;
  dataset_kind: string; scope: MemoryMetadata["scope"]; evidence: MemoryEvidence[]; warnings: string[];
  observed_form?: string; preferred_form?: string;
}
export interface MemoryEntry {
  memory_id: string; version: number; status: MemoryStatus; candidate: MemoryCandidate;
  review: { reviewer?: string; reason?: string };
  occurrences: { occurrence_id: string; request_id: string; response_mode: string;
    review_status: "unreviewed"; proposal: MemoryCandidate }[];
}
export interface MemoryAnalysis {
  id: string; status: "pending" | "running" | "success" | "failed";
  model?: string; error_message?: string; candidate_count?: number; response?: unknown;
}
export interface MemoryContextResult {
  context_id: string;
  context: { fingerprint: string; selected: { memory_id: string; version: number }[];
    omitted: { reason: string }[]; eligible_count: number; block_chars: number; notes: string[] };
}
export interface MemoryImportResult {
  candidate_count: number; inserted: string[]; duplicates: string[];
  occurrence_inserted: string[]; occurrence_duplicates: string[];
}
function path(id: string) { return `${base}/experiments/${encodeURIComponent(id)}`; }
function post<T>(url: string, body?: unknown) {
  return requestJson<T>(url, { method: "POST", ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
}
export function uploadMemorySource(role: MemoryRole, file: File) {
  return requestJson<{ token: string; name: string; size: number }>(
    `${base}/uploads/${role}?filename=${encodeURIComponent(file.name)}`,
    { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" } },
  );
}
export function prepareMemoryExperiment(payload: {
  inputs: Partial<Record<MemoryRole, string>>; metadata: MemoryMetadata;
  reference_metadata?: { source_kind: string; version: string; book_id: string };
  fragment_ids?: string[];
}) { return post<MemoryExperiment>(`${base}/experiments`, payload); }
export function listMemoryExperiments() {
  return requestJson<{ items: MemoryHistory[] }>(`${base}/experiments`);
}
export function getMemoryExperiment(id: string) { return requestJson<MemoryExperiment>(path(id)); }
export function startMemoryAnalysis(id: string, model?: string) {
  return post<{ analysis_id: string }>(`${path(id)}/analyses`, { allow_network: true, ...(model ? { model } : {}) });
}
export function getMemoryAnalysis(id: string, analysisId: string) {
  return requestJson<MemoryAnalysis>(`${path(id)}/analyses/${encodeURIComponent(analysisId)}`);
}
export function importMemoryAnalysis(id: string, analysisId: string) {
  return post<MemoryImportResult>(`${path(id)}/analyses/${encodeURIComponent(analysisId)}/import`);
}
export function importMemoryResponse(id: string, responseJson: string, mode: "offline" | "replay") {
  return post<MemoryImportResult>(`${path(id)}/import-response`, { response_json: responseJson, response_mode: mode });
}
export function listExperimentMemories(id: string) {
  return requestJson<{ items: MemoryEntry[] }>(`${path(id)}/memories`);
}
export function reviewMemory(id: string, entry: MemoryEntry, action: "approve" | "reject" | "disable", reviewer: string, reason: string) {
  return post(`${path(id)}/memories/${encodeURIComponent(entry.memory_id)}/review`, {
    action, reviewer, reason, expected_version: entry.version,
  });
}
export function exportMemoryContext(id: string, query: string, maxItems: number, maxChars: number) {
  return post<MemoryContextResult>(`${path(id)}/contexts`, { query, max_items: maxItems, max_chars: maxChars });
}
export function memoryRequestUrl(id: string) { return `${path(id)}/request`; }
export function memoryContextUrl(id: string, contextId: string, format: "json" | "txt") {
  return `${path(id)}/contexts/${encodeURIComponent(contextId)}/${format}`;
}
