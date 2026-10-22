import assert from "node:assert/strict";
import { DictationSelection } from "../src/composables/dictationSelection.ts";

// A delayed poll cannot reselect A after the user explicitly chooses B.
{
  const selection = new DictationSelection();
  let displayed = null;
  const commit = (value, token) => { if (selection.accept(value, token)) displayed = value; };
  const a = selection.select("A");
  let release;
  const delayedA = new Promise(resolve => { release = resolve; }).then(value => commit(value, a));
  const b = selection.select("B");
  commit({ id: "B", revision: 4 }, b);
  release({ id: "A", revision: 100 });
  await delayedA;
  assert.equal(displayed.id, "B");
  assert.equal(selection.current(a), false); // old sockets are rejected by the same generation
}
// An old GET cannot undo a newer editing/WS response for the same session.
{
  const selection = new DictationSelection();
  const token = selection.select("A");
  assert(selection.accept({ id: "A", revision: 2, event_sequence: 3 }, token));
  let release;
  const delayed = new Promise(resolve => { release = resolve; }).then(value => selection.accept(value, token));
  assert(selection.accept({ id: "A", revision: 5, event_sequence: 8 }, token));
  release({ id: "A", revision: 3, event_sequence: 6 });
  assert.equal(await delayed, false);
  assert.equal(selection.accept({ id: "A", revision: 5, event_sequence: 7 }, token), false);
  const reselected = selection.select("A");
  assert.equal(selection.accept({ id: "A", revision: 999 }, token), false);
  assert(selection.accept({ id: "A", revision: 5 }, reselected));
}
console.log("dictation selection: delayed HTTP, stale WS, revision and generation checks passed");
