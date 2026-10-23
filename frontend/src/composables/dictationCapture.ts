export type CaptureOwner = "live" | "local" | "probe";
/** One local PCM capture owner. Permission, worklet and flush callbacks belong to a lease. */
export class PcmCapture {
  deviceId = "";
  beforeCapture: () => void = () => {};
  onDevice: (id: string) => void = () => {};
  private inputStarted = 0;
  private lastSound = 0;
  get silentSeconds() { return this.lease?.node && !this.lease.stopping ? (Date.now() - Math.max(this.inputStarted, this.lastSound)) / 1000 : 0; }
  private lease: {
    owner: CaptureOwner; closed: boolean; stopping: boolean; stream?: MediaStream; context?: AudioContext;
    source?: MediaStreamAudioSourceNode; node?: AudioWorkletNode;
    flushed?: () => void; stop?: Promise<void>;
  } | null = null;

  get prepared() { return !!this.lease?.stream && !this.lease.closed && !this.lease.stopping; }
  isOwner(owner: CaptureOwner) { return !!this.lease && !this.lease.closed && this.lease.owner === owner; }
  get deviceInfo() {
    const track = this.lease?.stream?.getAudioTracks()[0];
    return { id: track?.getSettings().deviceId ?? "", label: track?.label ?? "" };
  }

  async prepare(owner: CaptureOwner, onDeviceError: (error: Error) => void, deviceOverride?: string) {
    if (this.lease && !this.lease.closed && this.lease.owner !== owner) throw new Error("麦克风正由另一项采音持有，请先结束它");
    if (this.lease) await this.stop(owner, false);
    if (this.lease && !this.lease.closed && this.lease.owner !== owner) throw new Error("麦克风正由另一项采音持有，请先结束它");
    if (!navigator.mediaDevices?.getUserMedia || !window.isSecureContext) {
      throw new Error("麦克风需要 HTTPS 或本机 localhost；也可上传录音。");
    }
    const lease = { owner, closed: false, stopping: false } as NonNullable<PcmCapture["lease"]>;
    this.lease = lease;
    this.beforeCapture();
    const requested = deviceOverride ?? this.deviceId;
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: requested ? { deviceId: { exact: requested } } : true, video: false });
    } catch (cause) {
      if (requested && cause instanceof DOMException && ["NotFoundError", "OverconstrainedError"].includes(cause.name)) {
        throw new Error("已选择的麦克风不可用，请在设置重新选择；不会自动切换其他设备。");
      }
      throw cause;
    }
    if (lease.closed || this.lease !== lease) {
      stream.getTracks().forEach(track => track.stop());
      throw new DOMException("采音操作已结束", "AbortError");
    }
    lease.stream = stream;
    const actual = stream.getAudioTracks()[0]?.getSettings?.().deviceId;
    if (requested && actual && requested !== actual) {
      await this.stop(owner, false);
      throw new Error("浏览器未使用已选择的麦克风，已停止；请重新选择设备。");
    }
    // System-default aliases can change physical device without changing ID.
    if (owner !== "probe" && actual && !["default", "communications"].includes(actual)) {
      this.deviceId = actual; this.onDevice(actual);
    }
    for (const track of stream.getAudioTracks()) {
      track.onended = () => {
        if (!lease.closed && !lease.stopping && this.lease === lease) {
          onDeviceError(new Error("麦克风设备已断开，已停止新增采音。请重新连接设备后继续，或结束听写。"));
        }
      };
    }
  }

  async start(owner: CaptureOwner, onPCM: (pcm: ArrayBuffer, level: number) => void) {
    const lease = this.lease;
    if (!lease?.stream || lease.closed || lease.stopping || lease.owner !== owner) throw new DOMException("采音操作已结束", "AbortError");
    if (lease.context) return;
    this.beforeCapture();
    const context = lease.context = new AudioContext();
    await context.audioWorklet.addModule("/dictation-worklet.js");
    if (lease.closed || lease.stopping || this.lease !== lease) throw new DOMException("采音操作已结束", "AbortError");
    const node = lease.node = new AudioWorkletNode(context, "dictation-capture");
    this.inputStarted = this.lastSound = Date.now();
    node.port.onmessage = event => {
      if (lease.closed || this.lease !== lease) return;
      if (event.data.flushed) { lease.flushed?.(); return; }
      if (event.data.pcm) {
        if (event.data.level >= 0.002) this.lastSound = Date.now();
        onPCM(event.data.pcm, event.data.level);
      }
    };
    const source = lease.source = context.createMediaStreamSource(lease.stream);
    const mute = context.createGain(); mute.gain.value = 0;
    source.connect(node); node.connect(mute); mute.connect(context.destination);
    await context.resume();
    if (lease.closed || lease.stopping || this.lease !== lease) throw new DOMException("采音操作已结束", "AbortError");
  }

  stop(owner: CaptureOwner, flush = true): Promise<void> {
    const lease = this.lease;
    if (!lease || lease.closed || lease.owner !== owner) return Promise.resolve();
    if (lease.stop) return lease.stop;
    lease.stopping = true;
    // Hardware is released synchronously, including while worklet flushing waits.
    lease.stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
    lease.source?.disconnect();
    lease.stop = (async () => {
      try {
        if (flush && lease.node) {
          await new Promise<void>((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error("采音尾包未确认，不能宣称暂停成功；请结束并检查已保存音频。")), 1000);
            lease.flushed = () => { clearTimeout(timer); resolve(); };
            lease.node!.port.postMessage("stop");
          });
        }
      } finally {
        lease.closed = true;
        lease.node?.disconnect();
        if (lease.node) lease.node.port.onmessage = null;
        if (lease.context && lease.context.state !== "closed") await lease.context.close();
        if (this.lease === lease) this.lease = null;
      }
    })();
    return lease.stop;
  }
}
