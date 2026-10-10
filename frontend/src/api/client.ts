export interface AsrCandidate {
  id: string;
  label: string;
  engine: string;
  model: string;
  supported_devices: string[];
  dependency_status: "installed" | "missing" | "unknown";
  unavailable_reason: string;
  cache_status: string;
  runtime_validation: string;
}

export interface AsrSelectionPayload {
  asr_candidate?: string | null;
  secondary_asr_candidate?: string | null;
}

export interface ConfigResponse {
  asr_candidates: AsrCandidate[];
  profiles: string[];
  backends: string[];
  configured_backends: string[];
  default_backend: string;
  default_ocr_backend: string;
  active_profile: string;
  video_extensions: string[];
  reference_extensions: string[];
  default_output_dir: string;
  upload_dir: string;
  content_types: string[];
}

export interface FrontendSettings {
  codex_lb_base_url: string;
  codex_lb_api_key: string;
  has_codex_lb_api_key: boolean;
  codex_lb_bypass_proxy: boolean;
  profile: string;
  asr_candidate: string;
  secondary_asr_candidate: string;
  backend: string;
  remote_concurrency: number;
  book_name: string;
  chapter: string;
  glossary_file: string;
  model: string;
  reasoning_effort: string;
  ocr_backend: string;
  ocr_model: string;
  ocr_reasoning_effort: string;
  ocr_max_concurrency: number;
  ocr_submit_interval_seconds: number;
  api_key_env: string;
  settings_path: string;
}

export interface FrontendSettingsPayload extends AsrSelectionPayload {
  codex_lb_base_url?: string | null;
  codex_lb_api_key?: string | null;
  clear_codex_lb_api_key?: boolean;
  codex_lb_bypass_proxy?: boolean | null;
  profile?: string | null;
  backend?: string | null;
  remote_concurrency?: number | null;
  book_name?: string | null;
  chapter?: string | null;
  glossary_file?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  ocr_backend?: string | null;
  ocr_model?: string | null;
  ocr_reasoning_effort?: string | null;
}

export interface FileItem {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
}

export interface FileListResponse {
  current_path: string;
  parent_path: string | null;
  items: FileItem[];
}

export type UploadKind = "video" | "reference" | "manifest" | "glossary" | "pdf_ocr";

export interface UploadResponse {
  kind: UploadKind;
  name: string;
  path: string;
  directory: string;
  size: number;
}

export interface BatchItemState {
  job_id: string;
  mode?: string;
  video_source?: string;
  reference_source?: string;
  content_type?: string;
  output_dir?: string;
  book_name?: string;
  chapter?: string;
  glossary_file?: string;
  status: string;
  current_stage?: string;
  completed_stages?: string[];
  failed_stage?: string;
  error_message?: string;
  copied_output_path?: string;
  ocr_items?: OCRProgressItem[];
  pages_total?: number;
  pages_completed?: number;
  pages_failed?: number;
  resumable?: boolean;
}

export interface OCRProgressItem {
  source_file: string;
  output_file?: string;
  success?: boolean;
  page_count: number;
  completed_pages: number;
  failed_pages: number;
  failed_page_numbers: number[];
  page_errors: Record<string, string>;
  resumable: boolean;
}

export interface JobInputSummary {
  [key: string]: string | undefined;
  video_source?: string;
  reference_source?: string;
  output_dir?: string;
  manifest?: string;
  videos_dir?: string;
  reference_dir?: string;
  shared_reference?: string;
  content_type?: string;
  book_name?: string;
  chapter?: string;
  glossary_file?: string;
}

export interface JobState {
  asr_candidate?: string;
  secondary_asr_candidate?: string;
  asr_engine?: string;
  asr_model?: string;
  id: string;
  kind: string;
  status: string;
  created_at: string;
  updated_at: string;
  current_stage: string;
  error_message: string;
  output_path: string;
  total?: number;
  success?: number;
  failed?: number;
  partial?: number;
  items?: BatchItemState[];
  input_summary?: JobInputSummary;
  run_mode?: string;
  download_name?: string;
  ocr_items?: OCRProgressItem[];
  pages_total?: number;
  pages_completed?: number;
  pages_failed?: number;
  resumable?: boolean;
  resume_count?: number;
}

export interface JobListResponse {
  items: JobState[];
}

export interface JobArtifact {
  id: string;
  stage: string;
  label: string;
  path: string;
  exists: boolean;
  size: number;
  content_type: string;
}

export interface JobArtifactListResponse {
  items: JobArtifact[];
}

export interface JobArtifactContent extends JobArtifact {
  content: string;
}

export type ResultDownloadFormat = "markdown" | "txt";

export interface SingleJobPayload extends AsrSelectionPayload {
  video: string;
  reference?: string | null;
  output_dir: string;
  content_type?: string | null;
  profile?: string | null;
  backend?: string | null;
  config?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  ocr_backend?: string | null;
  ocr_model?: string | null;
  ocr_reasoning_effort?: string | null;
  ocr_max_concurrency?: number | null;
  ocr_submit_interval_seconds?: number | null;
  book_name?: string | null;
  chapter?: string | null;
  glossary_file?: string | null;
  refine_prompt?: string | null;
}

export interface BatchJobPayload extends AsrSelectionPayload {
  manifest?: string | null;
  videos_dir?: string | null;
  reference_dir?: string | null;
  shared_reference?: string | null;
  output_dir?: string | null;
  content_type?: string | null;
  profile?: string | null;
  backend?: string | null;
  config?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  ocr_backend?: string | null;
  ocr_model?: string | null;
  ocr_reasoning_effort?: string | null;
  ocr_max_concurrency?: number | null;
  ocr_submit_interval_seconds?: number | null;
  remote_concurrency?: number | null;
  book_name?: string | null;
  chapter?: string | null;
  glossary_file?: string | null;
  refine_prompt?: string | null;
}

export interface RefinePromptResponse {
  prompt: string;
}

export interface StageRunPayload extends AsrSelectionPayload {
  profile?: string | null;
  backend?: string | null;
  config?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  ocr_backend?: string | null;
  ocr_model?: string | null;
  ocr_reasoning_effort?: string | null;
  ocr_max_concurrency?: number | null;
  ocr_submit_interval_seconds?: number | null;
}

export interface StageFileRunPayload extends StageRunPayload {
  input_files: Record<string, string>;
  result_name: string;
}

export interface StageFileInputSlot {
  key: string;
  label: string;
  extensions: string[];
}

export interface StageFileContract {
  stage_name: string;
  input_slots: StageFileInputSlot[];
  default_result_name: string;
}

export interface StageInputUploadResponse {
  stage_name: string;
  slot: string;
  name: string;
  path: string;
  size: number;
}

export interface JobRerunPayload extends AsrSelectionPayload {
  start_stage: string;
  profile?: string | null;
  backend?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  ocr_backend?: string | null;
  ocr_model?: string | null;
  ocr_reasoning_effort?: string | null;
  ocr_max_concurrency?: number | null;
  ocr_submit_interval_seconds?: number | null;
}

export interface PDFBookOCRPayload {
  input_path: string;
  config?: string | null;
  ocr_model?: string | null;
  ocr_reasoning_effort?: string | null;
  ocr_max_concurrency?: number | null;
  ocr_submit_interval_seconds?: number | null;
}

export interface PDFBookOCRItem {
  source_file: string;
  output_file: string;
  success: boolean;
  text_length: number;
  warnings: string[];
  error: string;
  page_count: number;
  completed_pages: number;
  failed_pages: number;
  failed_page_numbers: number[];
  page_errors: Record<string, string>;
  resumable: boolean;
}

export interface PDFBookOCRTask {
  id: string;
  kind: "pdf-ocr";
  status: "pending" | "running" | "success" | "partial" | "failed";
  created_at: string;
  updated_at: string;
  current_stage: string;
  error_message: string;
  output_path: string;
  total?: number;
  success?: number;
  failed?: number;
  pages_total?: number;
  pages_completed?: number;
  pages_failed?: number;
  resumable?: boolean;
  resume_count?: number;
  items?: PDFBookOCRItem[];
  input_summary?: {
    input_path?: string;
  };
}

export interface PDFBookOCRTaskListResponse {
  items: PDFBookOCRTask[];
}

function formatApiError(status: number, rawBody: string): string {
  if (!rawBody) {
    return `请求失败：HTTP ${status}`;
  }

  try {
    const parsed = JSON.parse(rawBody) as { detail?: unknown };
    if (typeof parsed.detail === "string") {
      return parsed.detail;
    }
    if (Array.isArray(parsed.detail)) {
      return parsed.detail
        .map((item) => {
          if (!item || typeof item !== "object") {
            return String(item);
          }
          const record = item as Record<string, unknown>;
          const loc = Array.isArray(record.loc) ? record.loc.join(".") : "";
          const msg = typeof record.msg === "string" ? record.msg : JSON.stringify(record);
          return loc ? `${loc}: ${msg}` : msg;
        })
        .join("；");
    }
    return JSON.stringify(parsed);
  } catch {
    return rawBody;
  }
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(formatApiError(response.status, detail));
  }

  return (await response.json()) as T;
}

function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") {
      return;
    }
    search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `?${query}` : "";
}

export function getConfig(): Promise<ConfigResponse> {
  return requestJson<ConfigResponse>("/api/config");
}

export function getFrontendSettings(): Promise<FrontendSettings> {
  return requestJson<FrontendSettings>("/api/frontend-settings");
}

export function getRefineDefaultInstruction(contentType = "book_club"): Promise<RefinePromptResponse> {
  const searchParams = new URLSearchParams({ content_type: contentType });
  return requestJson<RefinePromptResponse>(`/api/refine-default-instruction?${searchParams.toString()}`);
}

export function saveFrontendSettings(payload: FrontendSettingsPayload): Promise<FrontendSettings> {
  return requestJson<FrontendSettings>("/api/frontend-settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function listFs(path: string | null, type: "file" | "dir" | "all", showHidden: boolean): Promise<FileListResponse> {
  return requestJson<FileListResponse>(`/api/fs/list${buildQuery({ path, type, show_hidden: showHidden })}`);
}

export async function uploadFile(
  file: File,
  kind: UploadKind,
  options?: { groupId?: string; relativePath?: string },
): Promise<UploadResponse> {
  const response = await fetch(
    `/api/uploads${buildQuery({
      kind,
      filename: file.name,
      group_id: options?.groupId,
      relative_path: options?.relativePath,
    })}`,
    {
      method: "POST",
      body: file,
    },
  );

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(formatApiError(response.status, detail));
  }

  return (await response.json()) as UploadResponse;
}

export function submitPDFBookOCR(payload: PDFBookOCRPayload): Promise<{ task_id: string }> {
  return requestJson<{ task_id: string }>("/api/pdf-book-ocr", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getPDFBookOCRTask(taskId: string): Promise<PDFBookOCRTask> {
  return requestJson<PDFBookOCRTask>(`/api/pdf-book-ocr/${encodeURIComponent(taskId)}`);
}

export function retryPDFBookOCR(taskId: string): Promise<{ task_id: string }> {
  return requestJson<{ task_id: string }>(`/api/pdf-book-ocr/${encodeURIComponent(taskId)}/retry`, {
    method: "POST",
  });
}

export function listPDFBookOCRTasks(): Promise<PDFBookOCRTaskListResponse> {
  return requestJson<PDFBookOCRTaskListResponse>("/api/pdf-book-ocr");
}

export function pdfBookOCRResultUrl(taskId: string, outputFile: string): string {
  const encodedPath = outputFile
    .split("/")
    .filter(Boolean)
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `/api/pdf-book-ocr/${encodeURIComponent(taskId)}/results/${encodedPath}`;
}

export function pdfBookOCRArchiveUrl(taskId: string): string {
  return `/api/pdf-book-ocr/${encodeURIComponent(taskId)}/download`;
}

export function deletePDFBookOCRTask(taskId: string): Promise<{ success: boolean }> {
  return requestJson<{ success: boolean }>(`/api/pdf-book-ocr/${encodeURIComponent(taskId)}`, {
    method: "DELETE",
  });
}

export async function uploadStageInput(
  stageName: string,
  slotKey: string,
  file: File,
): Promise<StageInputUploadResponse> {
  const response = await fetch(
    `/api/stage-inputs/${encodeURIComponent(stageName)}/${encodeURIComponent(slotKey)}${buildQuery({ filename: file.name })}`,
    {
      method: "POST",
      body: file,
    },
  );

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(formatApiError(response.status, detail));
  }

  return (await response.json()) as StageInputUploadResponse;
}

export function submitJob(payload: SingleJobPayload): Promise<{ job_id: string }> {
  return requestJson<{ job_id: string }>("/api/jobs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getJob(jobId: string): Promise<JobState> {
  return requestJson<JobState>(`/api/jobs/${jobId}`);
}

export function listJobs(): Promise<JobListResponse> {
  return requestJson<JobListResponse>("/api/jobs");
}

export function listJobArtifacts(jobId: string): Promise<JobArtifactListResponse> {
  return requestJson<JobArtifactListResponse>(`/api/jobs/${jobId}/artifacts`);
}

export function getJobArtifact(jobId: string, artifactId: string): Promise<JobArtifactContent> {
  return requestJson<JobArtifactContent>(`/api/jobs/${jobId}/artifacts/${artifactId}`);
}

function appendResultFormat(url: string, format?: ResultDownloadFormat): string {
  if (!format || format === "markdown") {
    return url;
  }
  return `${url}?format=${encodeURIComponent(format)}`;
}

export function jobResultUrl(jobId: string, format?: ResultDownloadFormat): string {
  return appendResultFormat(`/api/jobs/${encodeURIComponent(jobId)}/result`, format);
}

export function listBatches(): Promise<JobListResponse> {
  return requestJson<JobListResponse>("/api/batches");
}

export function submitBatchJob(payload: BatchJobPayload): Promise<{ batch_id: string }> {
  return requestJson<{ batch_id: string }>("/api/batch-jobs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getBatch(batchId: string): Promise<JobState> {
  return requestJson<JobState>(`/api/batches/${batchId}`);
}

export function batchResultUrl(batchId: string, format?: ResultDownloadFormat): string {
  return appendResultFormat(`/api/batches/${encodeURIComponent(batchId)}/result`, format);
}

export function batchItemResultUrl(batchId: string, itemJobId: string, format?: ResultDownloadFormat): string {
  return appendResultFormat(
    `/api/batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemJobId)}/result`,
    format,
  );
}

export function listBatchItemArtifacts(batchId: string, itemJobId: string): Promise<JobArtifactListResponse> {
  return requestJson<JobArtifactListResponse>(
    `/api/batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemJobId)}/artifacts`,
  );
}

export function getBatchItemArtifact(
  batchId: string,
  itemJobId: string,
  artifactId: string,
): Promise<JobArtifactContent> {
  return requestJson<JobArtifactContent>(
    `/api/batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemJobId)}/artifacts/${encodeURIComponent(artifactId)}`,
  );
}

export function submitStageRun(stageName: string, payload: StageRunPayload): Promise<{ run_id: string }> {
  return requestJson<{ run_id: string }>(`/api/stages/${stageName}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getStageFileContract(stageName: string): Promise<StageFileContract> {
  return requestJson<StageFileContract>(`/api/stages/${encodeURIComponent(stageName)}/file-contract`);
}

export function submitStageFileRun(stageName: string, payload: StageFileRunPayload): Promise<{ run_id: string }> {
  return requestJson<{ run_id: string }>(`/api/stages/${encodeURIComponent(stageName)}/file-run`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function stageFileResultUrl(runId: string): string {
  return `/api/stage-runs/${encodeURIComponent(runId)}/result`;
}

export function getStageRun(runId: string): Promise<JobState> {
  return requestJson<JobState>(`/api/stage-runs/${runId}`);
}

export function retryStageRun(runId: string): Promise<{ run_id: string }> {
  return requestJson<{ run_id: string }>(`/api/stage-runs/${encodeURIComponent(runId)}/retry`, {
    method: "POST",
  });
}

export function listStageRuns(): Promise<JobListResponse> {
  return requestJson<JobListResponse>("/api/stage-runs");
}

export function deleteJob(jobId: string): Promise<{ success: boolean }> {
  return requestJson<{ success: boolean }>(`/api/jobs/${jobId}`, {
    method: "DELETE",
  });
}

export function rerunJob(jobId: string, payload: JobRerunPayload): Promise<{ job_id: string }> {
  return requestJson<{ job_id: string }>(`/api/jobs/${jobId}/rerun`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function rerunBatchItem(
  batchId: string,
  itemJobId: string,
  payload: JobRerunPayload,
): Promise<{ batch_id: string; job_id: string }> {
  return requestJson<{ batch_id: string; job_id: string }>(
    `/api/batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemJobId)}/rerun`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function deleteBatch(batchId: string): Promise<{ success: boolean }> {
  return requestJson<{ success: boolean }>(`/api/batches/${batchId}`, {
    method: "DELETE",
  });
}

export function deleteStageRun(runId: string): Promise<{ success: boolean }> {
  return requestJson<{ success: boolean }>(`/api/stage-runs/${runId}`, {
    method: "DELETE",
  });
}
