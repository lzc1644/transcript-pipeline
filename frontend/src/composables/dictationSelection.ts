export interface SelectionToken { id: string | null; generation: number }
export interface VersionedSnapshot { id: string; revision: number; event_sequence?: number }

/** The only admission gate for HTTP, polling and WebSocket snapshots. */
export class DictationSelection {
  private generation = 0;
  private id: string | null = null;
  private revision = -1;
  private eventSequence = -1;

  select(id: string | null): SelectionToken {
    this.id = id;
    this.generation += 1;
    this.revision = this.eventSequence = -1;
    return this.token();
  }
  token(): SelectionToken { return { id: this.id, generation: this.generation }; }
  current(token: SelectionToken): boolean {
    return this.id === token.id && this.generation === token.generation;
  }
  accept(value: VersionedSnapshot, token: SelectionToken): boolean {
    if (!this.current(token) || value.id !== token.id || value.revision < this.revision) return false;
    const sequence = value.event_sequence ?? 0;
    if (value.revision === this.revision && sequence < this.eventSequence) return false;
    this.revision = value.revision;
    this.eventSequence = sequence;
    return true;
  }
}
