import { audioFrame, id, record, type AudioFrame, type CaptureRequest } from "../../../shared/dictationExtensionProtocol";
import type { CaptureOwner, CaptureSource } from "./dictationCapture";

/** The sidebar owns hardware; the existing controller alone assigns backend PCM positions. */
export class SidebarCapture implements CaptureSource {
  private lease = "";
  private closed = true;
  private ready = false;
  private sequence = 0;
  private callback: ((pcm: ArrayBuffer, level: number) => void) | null = null;
  private onError: (error: Error) => void = () => {};
  private stopping: Promise<void> | null = null;
  private lastSound = 0;
  private faulted = false;
  private request: (data: CaptureRequest) => Promise<unknown>;
  constructor(request: (data: CaptureRequest) => Promise<unknown>) { this.request = request; }
  get prepared() { return this.ready && !this.closed && !this.stopping; }
  get silentSeconds() { return this.callback && !this.stopping ? (Date.now() - this.lastSound) / 1000 : 0; }
  isOwner(owner: CaptureOwner) { return owner === "live" && !this.closed; }
  private async call(data: CaptureRequest) {
    const reply = await this.request(data);
    if (!record(reply) || reply.ok !== true) throw new Error(record(reply) && typeof reply.error === "string" ? reply.error : "侧栏采音未确认，请检查侧栏与服务网页。");
    return reply;
  }
  async prepare(owner: CaptureOwner, onError: (error: Error) => void) {
    if (owner !== "live") throw new Error("侧栏采音只用于本次扩展听写。");
    if (!this.closed) throw new Error("侧栏仍有未结束的采音，请先结束。");
    const lease = this.lease = id();
    this.closed = false; this.ready = false; this.sequence = 0; this.stopping = null; this.faulted = false; this.onError = onError;
    await this.call({ action: "PREPARE", leaseId: lease });
    if (this.closed || this.lease !== lease || this.stopping) throw new DOMException("准备已取消", "AbortError");
    this.ready = true;
  }
  async start(owner: CaptureOwner, onPCM: (pcm: ArrayBuffer, level: number) => void) {
    if (owner !== "live" || !this.prepared) throw new DOMException("采音操作已结束", "AbortError");
    this.callback = onPCM; this.lastSound = Date.now();
    await this.call({ action: "START", leaseId: this.lease });
    if (this.closed || this.stopping) throw new DOMException("采音操作已结束", "AbortError");
  }
  feed(value: unknown) {
    if (!audioFrame(value) || value.leaseId !== this.lease || this.closed || this.faulted || !this.callback)
      throw new Error("采音包不属于当前音频 lease。");
    if (value.sequence < this.sequence) return { ok: true, nextSequence: this.sequence }; // Lost receipt: acknowledge, never resend PCM.
    if (value.sequence !== this.sequence) throw new Error("侧栏音频顺序出现缺口，不能带缺口续录。");
    const packet = value as AudioFrame;
    // Register before the synchronous consumer: an uncertain consumer failure must never replay audio.
    this.sequence++;
    try {
      if (packet.level >= 0.002) this.lastSound = Date.now();
      this.callback(new Int16Array(packet.pcm).buffer, packet.level);
    } catch (cause) { this.faulted = true; throw cause; }
    return { ok: true, nextSequence: this.sequence };
  }
  fail(leaseId: string, reason: string) { if (!this.closed && leaseId === this.lease) this.onError(new Error(reason)); }
  stop(owner: CaptureOwner, flush = true): Promise<void> {
    if (owner !== "live" || this.closed) return Promise.resolve();
    if (this.stopping) return this.stopping;
    if (!flush) this.callback = null;
    this.stopping = (async () => {
      try { await this.call({ action: "STOP", leaseId: this.lease, flush }); }
      finally { this.closed = true; this.ready = false; this.callback = null; }
    })();
    return this.stopping;
  }
}
