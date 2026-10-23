/** App-owned playback arbitration. Capture hooks and every player use this same owner. */
export class DictationPlayback {
  private current: HTMLMediaElement | null = null;
  private stopped: (() => void) | null = null;
  private canPlay: () => boolean;
  constructor(canPlay: () => boolean) { this.canPlay = canPlay; }
  stop() {
    const current = this.current, callback = this.stopped;
    this.current = null; this.stopped = null;
    current?.pause(); callback?.();
  }
  claim(player: HTMLMediaElement, onStop?: () => void) {
    if (!this.canPlay()) {
      player.pause();
      throw new Error("请先暂停采音再回听，避免把播放声录入");
    }
    if (this.current !== player) this.stop();
    this.current = player; this.stopped = onStop ?? null;
  }
  async play(player: HTMLMediaElement, onStop?: () => void) {
    this.claim(player, onStop);
    await player.play();
  }
  release(player: HTMLMediaElement | null) {
    if (player && this.current === player) this.stop();
  }
}
