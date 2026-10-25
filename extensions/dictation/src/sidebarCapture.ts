import { PcmCapture } from '../../../frontend/src/composables/dictationCapture';
import { record, type AudioFrame, type CaptureRequest } from '../../../shared/dictationExtensionProtocol';

type Lease = {
  id: string; operationId: string; authorization: number; sequence: number; closed: boolean; pending: number;
  chain: Promise<void>; abort: Promise<never>; cancel: () => void; stop?: Promise<void>;
};
/** Hardware-only carrier. HTTP/WS, backend positions and recognition stay in the service page. */
export class SidebarHost {
  readonly capture = new PcmCapture();
  enabled = false;
  phase = 'unarmed';
  error = '';
  private lease: Lease | null = null;
  private closed = new Set<string>();
  private probeGeneration = 0;
  private probing = false;
  private relay: (operationId: string, frame: AudioFrame) => Promise<unknown>;
  private changed: () => void;
  private fault: (operationId: string, leaseId: string, error: string) => void;
  constructor(relay: SidebarHost['relay'], changed: () => void, fault: SidebarHost['fault']) {
    this.relay = relay; this.changed = changed; this.fault = fault;
  }
  snapshot() { return { enabled: this.enabled, phase: this.phase, error: this.error,
    operationId: this.lease?.operationId, leaseId: this.lease?.id, sequence: this.lease?.sequence ?? 0 }; }
  async probe() {
    if (this.probing || (this.lease && !this.lease.closed)) throw new Error('请先结束当前采音。');
    const own = ++this.probeGeneration;
    this.probing = true; this.enabled = false; this.phase = 'probing'; this.error = ''; this.changed();
    let samples = 0, deviceError: Error | undefined, timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await this.capture.prepare('probe', error => { deviceError = error; void this.capture.stop('probe', false); });
      if (own !== this.probeGeneration) return;
      await Promise.race([
        this.capture.start('probe', pcm => { samples += pcm.byteLength / 2; }),
        new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('音频上下文未启动，请保持侧栏可见后重试。')), 5000); }),
      ]);
      await new Promise(resolve => setTimeout(resolve, 400));
      if (own !== this.probeGeneration) return;
      if (deviceError) throw deviceError;
      if (!samples) throw new Error('未收到麦克风采样，请检查设备后重试。');
      await this.capture.stop('probe');
      this.enabled = true; this.phase = 'ready';
    } catch (cause) {
      if (own === this.probeGeneration) { this.phase = 'unarmed'; this.error = String(cause); }
    } finally {
      if (timer) clearTimeout(timer);
      if (own === this.probeGeneration) { await this.capture.stop('probe', false); this.probing = false; }
      this.changed();
    }
  }
  async disable() {
    const own = ++this.probeGeneration; this.probing = false; this.enabled = false;
    const live = this.stop(false), probe = this.capture.stop('probe', false);
    await Promise.all([live, probe]);
    if (own === this.probeGeneration) { this.phase = 'unarmed'; this.changed(); }
  }
  private fail(lease: Lease, cause: unknown) {
    if (lease.closed || lease.stop) return;
    this.error = cause instanceof Error ? cause.message : String(cause);
    this.enabled = false;
    // stop() releases hardware before its first await. No dependency on the service page.
    void this.stop(false).catch(() => {});
    this.phase = 'failed'; this.changed(); this.fault(lease.operationId, lease.id, this.error);
  }
  private enqueue(lease: Lease, pcm: ArrayBuffer, level: number) {
    if (lease.closed) return;
    if (lease.pending >= 62) { this.fail(lease, new Error('侧栏音频积压接近5秒，已关闭麦克风。')); return; }
    const frame: AudioFrame = { leaseId: lease.id, sequence: lease.sequence++, pcm: Array.from(new Int16Array(pcm)), level };
    lease.pending++;
    lease.chain = lease.chain.then(async () => {
      if (lease.closed) return;
      // Retry only the same sequence. The page deduplicates lost receipts before assigning backend positions.
      for (let attempt = 0; ; attempt++) {
        try {
          const reply = await this.relay(lease.operationId, frame);
          if (!record(reply) || reply.ok !== true || reply.nextSequence !== frame.sequence + 1) throw new Error('网页未确认音频包。');
          return;
        } catch (cause) { if (lease.closed || attempt === 1) throw cause; }
      }
    }).finally(() => { lease.pending--; });
    void lease.chain.catch(cause => this.fail(lease, cause));
  }
  async request(operationId: string, data: CaptureRequest) {
    if (data.action === 'STOP') {
      const lease = this.lease;
      if (lease?.id === data.leaseId && lease.operationId !== operationId) throw new Error('采音操作不匹配。');
      this.closed.add(data.leaseId);
      if (lease?.id === data.leaseId && lease.operationId === operationId) {
        if (data.disable && lease.authorization === this.probeGeneration) this.enabled = false;
        await this.stop(data.flush !== false);
        if (data.disable && this.lease === lease && lease.authorization === this.probeGeneration) { this.phase = 'unarmed'; this.changed(); }
      }
      return { ok: true };
    }
    if (data.action === 'INSPECT') return { ok: true, ...this.snapshot() };
    if (this.closed.has(data.leaseId)) throw new Error('采音 lease 已结束，不能重新开启。');
    if (data.action === 'PREPARE') {
      if (!this.enabled || document.hidden) throw new Error('请保持本窗口侧栏可见，并先授权试音。');
      if (this.lease?.id === data.leaseId && this.lease.operationId === operationId) return { ok: true };
      if (this.probing || (this.lease && !this.lease.closed)) throw new Error('侧栏已有采音操作。');
      let cancel!: () => void;
      const abort = new Promise<never>((_, reject) => { cancel = () => reject(new DOMException('采音已取消', 'AbortError')); });
      void abort.catch(() => {});
      const lease: Lease = this.lease = { id: data.leaseId, operationId, authorization: this.probeGeneration, sequence: 0, pending: 0,
        chain: Promise.resolve(), closed: false, abort, cancel };
      this.phase = 'preparing'; this.error = ''; this.changed();
      try { await Promise.race([this.capture.prepare('live', error => this.fail(lease, error)), abort]); }
      catch (cause) { this.fail(lease, cause); throw cause; }
    } else {
      const lease = this.lease;
      if (!lease || lease.id !== data.leaseId || lease.operationId !== operationId || lease.closed) throw new Error('采音绑定已失效。');
      try { await Promise.race([this.capture.start('live', (pcm, level) => this.enqueue(lease, pcm, level)), lease.abort]); }
      catch (cause) { this.fail(lease, cause); throw cause; }
      if (!lease.closed && !lease.stop) { this.phase = 'recording'; this.changed(); }
    }
    return { ok: true };
  }
  stop(flush = true): Promise<void> {
    const lease = this.lease;
    if (!lease || lease.closed) return Promise.resolve();
    if (lease.stop) return lease.stop;
    this.closed.add(lease.id); lease.cancel();
    if (!flush) lease.closed = true;
    const hardware = this.capture.stop('live', flush);
    lease.stop = (async () => {
      try { await hardware; if (flush) await lease.chain; }
      catch (cause) {
        if (this.lease === lease && lease.authorization === this.probeGeneration) {
          this.error = String(cause); this.enabled = false; this.fault(lease.operationId, lease.id, this.error);
        }
        throw cause;
      }
      finally {
        lease.closed = true;
        if (this.lease === lease && lease.authorization === this.probeGeneration) {
          this.phase = this.error ? 'failed' : this.enabled ? 'ready' : 'unarmed'; this.changed();
        }
      }
    })();
    return lease.stop;
  }
}
