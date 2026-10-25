import browser from 'webextension-polyfill';
let binding: { tabId?: number; documentInstanceId: string; phase: string; op?: string } | null = null, loaded = false;
const ready = browser.storage.session.get('binding').then(value => { binding = value.binding as typeof binding ?? null; loaded = true; });
browser.runtime.onMessage.addListener((body: unknown, sender: browser.Runtime.MessageSender) => {
  if (sender.id !== browser.runtime.id || (sender.frameId !== undefined && sender.frameId !== 0) || sender.url !== browser.runtime.getURL('recorder.html') || !body || typeof body !== 'object') return undefined;
  const b = body as Record<string, unknown>;
  return ready.then(async () => {
    if (b.type === 'BIND' && typeof b.documentInstanceId === 'string') binding = { tabId: sender.tab?.id, documentInstanceId: b.documentInstanceId, phase: 'unarmed' };
    if (b.type === 'STATE' && binding && binding.documentInstanceId === b.documentInstanceId) {
      binding.phase = String(b.phase); if (typeof b.op === 'string') binding.op = b.op;
    }
    await browser.storage.session.set({ binding }); return { accepted: true };
  });
});
browser.commands.onCommand.addListener(() => {
  if (loaded && !binding && browser.runtime.getManifest().sidebar_action) { void browser.sidebarAction.open(); return; }
  void ready.then(async () => {
  if (!binding) return;
  const type = ['preparing', 'capturing'].includes(binding.phase) ? 'STOP' : 'START';
  binding.op = type === 'START' ? crypto.randomUUID() : binding.op;
  binding.phase = type === 'START' ? 'preparing' : 'stopping';
  await browser.storage.session.set({ binding });
  const body = { type, documentInstanceId: binding.documentInstanceId, op: binding.op };
  if (binding.tabId) await browser.tabs.sendMessage(binding.tabId, body, { frameId: 0 });
  else await browser.runtime.sendMessage(body);
  });
});
