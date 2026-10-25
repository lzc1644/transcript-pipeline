/** Data-only bridge. Target pages never expose a window-to-extension bridge. */
export const PROTOCOL_VERSION = 1;
export const CHANNEL = "transcript-dictation";
export const RECORDER_PATH = "/dictation/extension";
export const SERVICE_PATHS = ["/dictation", RECORDER_PATH];
// No target capture or text-delivery messages until S2.
export const TYPES = ["HELLO", "STATUS", "CONNECT", "DISCONNECT", "OPEN_RECORDER", "SHORTCUTS", "UI_STATE",
  "TOGGLE", "CANCEL", "DISCARD", "START", "STOP", "GET_OPERATION", "STATE", "ERROR", "RECORDER_HELLO",
  "SIDEBAR_HELLO", "SIDEBAR_STATE", "SIDEBAR_CLOSED", "CAPTURE_REQUEST", "AUDIO_FRAME", "CAPTURE_ERROR"] as const;
export type MessageType = typeof TYPES[number];
export interface Envelope {
  channel: typeof CHANNEL; protocolVersion: typeof PROTOCOL_VERSION; type: MessageType; requestId: string;
  operationId?: string; documentInstanceId?: string; sessionId?: string; data?: unknown;
}
export function id() { return crypto.randomUUID(); }
export function message(type: MessageType, fields: Omit<Partial<Envelope>, "type" | "channel" | "protocolVersion" | "requestId"> = {}): Envelope {
  return { channel: CHANNEL, protocolVersion: PROTOCOL_VERSION, requestId: id(), type, ...fields };
}
export function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
export function identifier(value: unknown): value is string { return typeof value === "string" && /^[\w-]{1,100}$/.test(value); }
export function envelope(value: unknown): value is Envelope {
  return record(value) && value.channel === CHANNEL && value.protocolVersion === PROTOCOL_VERSION &&
    TYPES.includes(value.type as MessageType) && identifier(value.requestId) &&
    [value.operationId, value.documentInstanceId, value.sessionId].every(v => v === undefined || identifier(v));
}
export function serviceOrigin(value: string) {
  const url = new URL(value.trim());
  if (url.username || url.password || url.hash || url.search || !["", "/", ...SERVICE_PATHS].includes(url.pathname))
    throw new Error("请填写服务根地址，不含账号、查询参数或片段。");
  if (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)))
    throw new Error("服务必须使用可信 HTTPS；HTTP 仅限开发用 loopback 地址。");
  return url.origin;
}
export function hostPermission(origin: string) {
  const url = new URL(serviceOrigin(origin));
  return `${url.protocol}//${url.hostname}/*`;
}
export function servicePage(value: string | undefined, origin: string, recorderOnly = false) {
  try { const u = new URL(value ?? ""); return u.origin === origin && (recorderOnly ? u.pathname === RECORDER_PATH : SERVICE_PATHS.includes(u.pathname)); }
  catch { return false; }
}

export type CaptureAction = "PREPARE" | "START" | "STOP" | "INSPECT";
export interface CaptureRequest { action: CaptureAction; leaseId: string; flush?: boolean; disable?: boolean }
export function captureRequest(value: unknown): value is CaptureRequest {
  return record(value) && ["PREPARE", "START", "STOP", "INSPECT"].includes(String(value.action)) &&
    identifier(value.leaseId) && (value.flush === undefined || typeof value.flush === "boolean") &&
    (value.disable === undefined || typeof value.disable === "boolean");
}
/** JSON-safe PCM16, one existing 80 ms worklet packet (or its partial tail). */
export interface AudioFrame { leaseId: string; sequence: number; pcm: number[]; level: number }
export function audioFrame(value: unknown): value is AudioFrame {
  return record(value) && identifier(value.leaseId) && Number.isSafeInteger(value.sequence) && Number(value.sequence) >= 0 &&
    Array.isArray(value.pcm) && value.pcm.length > 0 && value.pcm.length <= 1280 &&
    value.pcm.every(n => Number.isInteger(n) && n >= -32768 && n <= 32767) &&
    typeof value.level === "number" && Number.isFinite(value.level) && value.level >= 0 && value.level <= 1;
}
export interface CaptureBinding { windowId: number; documentInstanceId: string; enabled: boolean }
export function captureBinding(value: unknown): value is CaptureBinding {
  return record(value) && Number.isInteger(value.windowId) && identifier(value.documentInstanceId) && typeof value.enabled === "boolean";
}
