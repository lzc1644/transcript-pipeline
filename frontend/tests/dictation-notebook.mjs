// Real editor + isolated Python API/storage. Speech/cleanup are deterministic fixtures,
// not model quality or physical microphone evidence.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, open, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const root = resolve(import.meta.dirname, '../..');
const output = resolve(root, process.env.DICTATION_TEST_OUTPUT || `tmp/dictation-notebook-${Date.now()}`);
await mkdir(output, { recursive: false });
const api = 'http://127.0.0.1:8027', base = 'http://127.0.0.1:5177';
const apiLog = await open(`${output}/api.log`, 'w'), viteLog = await open(`${output}/vite.log`, 'w');
const server = spawn(`${root}/.venv/bin/python`, ['-m', 'tests.dictation_notebook_server', '--root', `${output}/project`, '--port', '8027'], { cwd: root, stdio: ['ignore', apiLog.fd, apiLog.fd] });
const vite = spawn(process.execPath, [`${root}/frontend/node_modules/vite/bin/vite.js`, '--host', '127.0.0.1', '--port', '5177', '--strictPort'],
  { cwd: `${root}/frontend`, env: { ...process.env, TRANSCRIPT_API_PROXY_TARGET: api }, stdio: ['ignore', viteLog.fd, viteLog.fd] });
let browser;
const errors = [], checks = [];
try {
  for (const url of [api + '/api/dictation/config', base]) {
    let ready = false;
    for (let i = 0; i < 100; i++) { try { if ((await fetch(url)).ok) { ready = true; break; } } catch {} await new Promise(r => setTimeout(r, 100)); }
    assert(ready, `server unavailable: ${url}`);
  }
  browser = await chromium.launch({ headless: true, executablePath: process.env.DICTATION_TEST_CHROMIUM || undefined,
    args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] });
  const context = await browser.newContext({ viewport: { width: 1159, height: 756 }, permissions: ['microphone', 'clipboard-read', 'clipboard-write'] });
  await context.addInitScript(origin => {
    if (location.origin !== origin) return;
    import('/node_modules/.vite/deps/@codemirror_view.js').then(m => {
      window.notebookView = () => m.EditorView.findFromDOM(document.querySelector('.cm-editor'));
    });
  }, base);
  // Use a genuinely untrusted HTTP origin. localhost is a secure context and
  // would hide missing secure-context-only APIs such as crypto.randomUUID.
  const httpOrigin = 'http://dictation-http.test';
  const httpContext = await browser.newContext({ viewport: { width:1159, height:756 } });
  await httpContext.addInitScript(origin => {
    if (location.origin !== origin) return;
    import('/node_modules/.vite/deps/@codemirror_view.js').then(m => {
      window.notebookView = () => m.EditorView.findFromDOM(document.querySelector('.cm-editor'));
    });
  }, httpOrigin);
  await httpContext.route(`${httpOrigin}/**`, async route => {
    const url = new URL(route.request().url()), isAPI = url.pathname.startsWith('/api/');
    const target = isAPI ? api : base;
    const response = await route.fetch({ url:target + url.pathname + url.search,
      headers:{ ...route.request().headers(), host:isAPI ? url.host : new URL(base).host } });
    await route.fulfill({ response });
  });
  const httpPage = await httpContext.newPage();
  httpPage.on('pageerror', error => errors.push(error.message));
  const httpOperations = [], liveRequests = [];
  httpPage.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === 'PATCH' && path.endsWith('/document')) httpOperations.push(request.postDataJSON().operation_id);
    if (request.method() === 'POST' && path === '/api/dictation/sessions') liveRequests.push(request.url());
  });
  await httpPage.goto(`${httpOrigin}/jobs`);
  await httpPage.getByRole('heading', { name:'任务列表', exact:true, level:2 }).waitFor();
  await httpPage.getByText('还没有任务记录。', { exact:true }).waitFor();
  const httpCapabilities = await httpPage.evaluate(() => ({ secure:isSecureContext, uuid:typeof crypto.randomUUID,
    randomValues:typeof crypto.getRandomValues, microphone:typeof navigator.mediaDevices?.getUserMedia, writer:window.name }));
  assert.equal(httpCapabilities.secure, false);
  assert.equal(httpCapabilities.uuid, 'undefined');
  assert.equal(httpCapabilities.randomValues, 'function');
  assert.equal(httpCapabilities.microphone, 'undefined');
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  assert(uuid.test(httpCapabilities.writer.replace('dictation-editor-', '')));
  await httpPage.screenshot({ path:`${output}/http-jobs.png`, fullPage:true });
  checks.push('ordinary LAN-like HTTP renders existing jobs page with isSecureContext=false and native randomUUID absent');

  const httpSeed = await httpPage.evaluate(async () => (await fetch('/api/notebook-fixture/seed?legacy=true', {method:'POST'})).json());
  await httpPage.goto(`${httpOrigin}/dictation?session=${httpSeed.id}`);
  const httpEditor = httpPage.getByRole('textbox', { name:'正文编辑器', exact:true });
  await httpEditor.waitFor();
  const httpDocText = () => httpEditor.evaluate(() => window.notebookView().state.doc.toString());
  assert.equal(await httpDocText(), httpSeed.draft_text);
  await httpEditor.click(); await httpPage.keyboard.press('Control+Home'); await httpPage.keyboard.insertText('HTTP人工校订😀。\n');
  await httpPage.getByRole('status').filter({hasText:/^已保存$/}).waitFor();
  const httpText = await httpDocText();
  assert.equal(httpText, 'HTTP人工校订😀。\n' + httpSeed.draft_text);
  assert(httpOperations.length > 0 && httpOperations.every(id => uuid.test(id)));
  const httpTXT = await httpPage.evaluate(async id => (await fetch(`/api/dictation/sessions/${id}/draft.txt`)).text(), httpSeed.id);
  assert.equal(httpTXT, httpText);
  await httpPage.reload(); await httpEditor.waitFor();
  assert.equal(await httpDocText(), httpText);
  assert.equal(await httpPage.evaluate(() => window.name), httpCapabilities.writer);
  const [httpDownload] = await Promise.all([httpPage.waitForEvent('download'), httpPage.getByRole('button', {name:'下载 TXT',exact:true}).click()]);
  assert.equal(await readFile(await httpDownload.path(), 'utf8'), httpText);
  await httpPage.screenshot({ path:`${output}/http-history-editor.png`, fullPage:true });
  checks.push('insecure HTTP converts legacy history, edits with UUID operation IDs, autosaves, reloads and downloads matching TXT');

  await httpPage.getByRole('button', {name:'开始听写',exact:true}).click();
  await httpPage.getByRole('alert').filter({hasText:'麦克风需要 HTTPS 或本机 localhost；也可上传录音。'}).waitFor();
  assert.deepEqual(liveRequests, []);
  await httpPage.getByRole('button', {name:'录音转写',exact:true}).click();
  const localStart = httpPage.getByRole('button', {name:'新建本地录音',exact:true});
  await localStart.click();
  await httpPage.getByRole('alert').filter({hasText:'此浏览器不支持跨窗口录音锁'}).waitFor();
  assert(await localStart.isEnabled(), 'HTTP draft ID creation must not leave the recorder stuck busy');
  checks.push('insecure HTTP blocks microphone capture, creates no live session and releases local-recorder busy state');

  // Observe the actual browser XHR and bytes, but fixture its result: this
  // regression must not start an ASR worker or call an external model.
  const uploadBytes = Buffer.from('HTTP upload regression fixture');
  let httpUpload;
  await httpContext.route(`${httpOrigin}/api/dictation/uploads?**`, async route => {
    httpUpload = { url:route.request().url(), bytes:route.request().postDataBuffer() };
    await route.fulfill({json:{ ...httpSeed, editor_version:1, draft_text:httpText, mode:'upload', status:'completed' }});
  });
  await httpPage.getByRole('button', {name:'导入音频',exact:true}).click();
  await httpPage.getByLabel('选择录音文件', {exact:true}).setInputFiles({name:'http.wav',mimeType:'audio/wav',buffer:uploadBytes});
  await httpEditor.waitFor();
  assert.deepEqual(httpUpload.bytes, uploadBytes);
  assert.equal(new URL(httpUpload.url).searchParams.get('editor_version'), '1');
  assert.deepEqual(liveRequests, []);
  checks.push('insecure HTTP sends original upload bytes and editor_version=1 without requesting microphone (ASR response fixture)');
  await writeFile(`${output}/http-capabilities.json`, JSON.stringify({ ...httpCapabilities, operation_ids:httpOperations,
    microphone_sessions:liveRequests.length, upload_bytes:httpUpload.bytes.length },null,2));
  await httpContext.close();

  const settingsPage = await context.newPage();
  settingsPage.on('pageerror', error => errors.push(error.message));
  await settingsPage.goto(`${base}/dictation`);
  assert(await settingsPage.evaluate(() => isSecureContext && typeof crypto.randomUUID === 'function'));
  await settingsPage.getByRole('button', {name:'设置',exact:true}).click();
  assert(await settingsPage.getByRole('checkbox', {name:'使用逐句轻度整理'}).isChecked());
  for (const [selector,expected] of [['#dictation-cleanup-model','gemini-3.5-flash-lite'],['#dictation-cleanup-effort','medium'],
    ['#dictation-document-model','gemini-3.8-flash-high'],['#dictation-document-effort','high']]) {
    assert.equal(await settingsPage.locator(selector).inputValue(), '');
    assert((await settingsPage.locator(`${selector} option:checked`).textContent()).includes(expected));
  }
  await settingsPage.locator('#dictation-cleanup-model').selectOption('gpt-6-luna');
  await settingsPage.locator('#dictation-cleanup-effort').selectOption('low');
  await settingsPage.reload();
  await settingsPage.getByRole('button', {name:'设置',exact:true}).click();
  assert.equal(await settingsPage.locator('#dictation-cleanup-model').inputValue(), 'gpt-6-luna');
  assert.equal(await settingsPage.locator('#dictation-cleanup-effort').inputValue(), 'low');
  await settingsPage.getByRole('button', {name:'恢复默认',exact:true}).click();
  for (const selector of ['#dictation-cleanup-model','#dictation-cleanup-effort','#dictation-document-model','#dictation-document-effort'])
    assert.equal(await settingsPage.locator(selector).inputValue(), '');
  await settingsPage.locator('.dictation-model-grid').scrollIntoViewIfNeeded();
  await settingsPage.screenshot({path:`${output}/default-model-settings.png`,fullPage:true});
  await settingsPage.close();
  checks.push('settings defaults/reset use Flash Lite medium and Flash High high; saved manual choices survive reload');

  const legacyPage = await context.newPage();
  legacyPage.on('pageerror', error => errors.push(error.message));
  const legacy = await (await context.request.post(`${base}/api/notebook-fixture/seed?legacy=true`)).json();
  await legacyPage.goto(`${base}/dictation?session=${legacy.id}`);
  await legacyPage.getByRole('textbox', { name: '正文编辑器', exact: true }).waitFor();
  await legacyPage.waitForFunction(text => window.notebookView?.()?.state.doc.toString() === text, legacy.draft_text, {timeout:5000});
  await legacyPage.close();
  checks.push('legacy conversion preserves the original text on first opening');

  const page = await context.newPage();
  page.on('pageerror', e => errors.push(e.stack || e.message));
  const seeded = await (await context.request.post(`${base}/api/notebook-fixture/seed`)).json();
  const id = seeded.id, endpoint = `${base}/api/dictation/sessions/${id}`;
  const snapshot = async () => (await context.request.get(endpoint)).json();
  await page.goto(`${base}/dictation?session=${id}`);
  const content = () => page.getByRole('textbox', { name: '正文编辑器', exact: true });
  await content().waitFor();
  // A later conflicting pending must not leave an earlier pending rebased
  // against a base/version that hasn't been accepted.
  await page.evaluate(async () => {
    const { DictationDocument } = await import('/src/composables/dictationDocument.ts');
    const { ChangeSet } = await import('/node_modules/.vite/deps/@codemirror_state.js');
    let reject;
    const buffer = new DictationDocument('two-pending-conflict', () => new Promise((_, fail) => { reject = fail; }));
    const base = { id:buffer.id, editor_version:1, document_version:0, content_revision:0, draft_text:'甲乙丙丁', document_operations:[] };
    buffer.receive(base);
    buffer.local(ChangeSet.of({ from:4, insert:'一' }, 4));
    const flight = buffer.flush().catch(() => {});
    buffer.local(ChangeSet.of({ from:1, insert:'二' }, 5));
    const key = Object.keys(localStorage).find(key => key.includes('two-pending-conflict'));
    const before = JSON.parse(localStorage.getItem(key));
    buffer.receive({ ...base, document_version:1, content_revision:1, draft_text:'远端稿丙丁',
      document_operations:[{ id:'remote',version:1,kind:'user',changes:[{from:0,to:2,insert:'远端稿'}] }] });
    const after = JSON.parse(localStorage.getItem(key));
    if (buffer.status.value !== 'conflict' || JSON.stringify(before) !== JSON.stringify(after)) throw new Error('conflict mutated recovery base or pending queue');
    reject(new Error('fixture complete')); await flight;
    localStorage.removeItem(key);
  });
  checks.push('two-pending overlap leaves the original recovery base, text and both operations intact');
  await page.evaluate(async () => {
    const { DictationDocument } = await import('/src/composables/dictationDocument.ts');
    const buffer = new DictationDocument('stale-save-response', async () => {});
    const base = { id:buffer.id, revision:1, event_sequence:1, editor_version:1,
      document_version:0, content_revision:0, draft_text:'原稿', document_operations:[] };
    const withPreview = { ...base, document_suggestion:{id:'old', original:'原稿',text:'建议',base_content_revision:0} };
    buffer.receive(withPreview);
    buffer.receive({ ...base, revision:2, event_sequence:2 });
    buffer.receive(withPreview);
    if (buffer.preview.value || buffer.snapshot.value.revision !== 2) throw new Error('old save response restored a discarded preview');
    buffer.composition(true);
    buffer.receive({ ...base, revision:4, event_sequence:4 });
    buffer.receive({ ...withPreview, revision:3, event_sequence:3 });
    buffer.composition(false);
    if (buffer.preview.value || buffer.snapshot.value.revision !== 4) throw new Error('IME deferred snapshot regressed');
  });
  checks.push('late save responses and IME-deferred snapshots cannot resurrect discarded suggestions');
  const docText = () => content().evaluate(() => window.notebookView().state.doc.toString());
  const saved = async () => { await page.getByRole('status', { name: '', exact: true }).filter({ hasText: /^已保存$/ }).waitFor(); };
  await content().click(); await page.keyboard.press('Control+Home');
  await page.keyboard.insertText('人工😀修改。\n');
  await saved();
  assert((await snapshot()).draft_text.startsWith('人工😀修改。\n文章😀'));
  await page.keyboard.press('Control+z'); await saved();
  assert.equal((await snapshot()).draft_text, seeded.draft_text);
  await page.keyboard.press('Control+Shift+z'); await saved();
  assert((await snapshot()).draft_text.startsWith('人工😀修改。\n'));
  assert.equal(await (await context.request.get(endpoint + '/draft.txt')).text(), await docText());
  checks.push('continuous editing, emoji/newlines, undo/redo, autosave and GET/TXT agree');

  // Hold an edit request while a disjoint speech insertion commits on the server.
  let release, held;
  const gate = new Promise(r => release = r), intercepted = new Promise(r => held = r);
  await page.route('**/api/dictation/sessions/*/document', async route => {
    if (route.request().method() !== 'PATCH') return route.continue();
    held(); await gate; await route.continue();
  });
  await page.keyboard.press('Control+Home'); await page.keyboard.insertText('并发。');
  await intercepted;
  await context.request.post(`${base}/api/notebook-fixture/${id}/speech`);
  await page.waitForFunction(() => window.notebookView().state.doc.toString().endsWith('语音落字。'));
  assert((await docText()).startsWith('并发。'));
  release(); await saved(); await page.unroute('**/api/dictation/sessions/*/document');
  assert.equal((await snapshot()).draft_text, await docText());
  // Undo the most recent speech insert (separate from the manual edit), then a late cleanup.
  await page.keyboard.press('Control+z'); await saved();
  assert(!(await snapshot()).draft_text.endsWith('语音落字。'));
  await context.request.post(`${base}/api/notebook-fixture/${id}/clean`);
  await page.waitForTimeout(1100);
  assert(!(await docText()).endsWith('语音落字。。'));
  checks.push('in-flight local edit rebases around speech; speech undo is independent and late cleanup does not resurrect it');

  const beforeCleanedSpeech = (await snapshot()).draft_text;
  await context.request.post(`${base}/api/notebook-fixture/${id}/speech`);
  await page.waitForFunction(() => window.notebookView().state.doc.toString().endsWith('语音落字。'));
  await context.request.post(`${base}/api/notebook-fixture/${id}/clean`);
  await page.waitForFunction(() => window.notebookView().state.doc.toString().endsWith('语音落字。。'));
  await page.keyboard.press('Control+z'); await saved();
  assert.equal((await snapshot()).draft_text, beforeCleanedSpeech + '语音落字。');
  await page.keyboard.press('Control+z'); await saved();
  assert.equal((await snapshot()).draft_text, beforeCleanedSpeech);
  checks.push('cleanup is undoable without erasing the original speech undo step');

  // A failed save stays editable, survives refresh and can be explicitly retried.
  await page.route('**/api/dictation/sessions/*/document', route => route.request().method() === 'PATCH'
    ? route.fulfill({ status: 503, json: { detail: 'fixture: disk unavailable' } }) : route.continue());
  await page.keyboard.press('Control+Home'); await page.keyboard.insertText('失败保留。');
  await page.getByRole('button', { name: '重试保存', exact: true }).waitFor();
  assert((await docText()).startsWith('失败保留。'));
  await page.reload(); await content().waitFor();
  assert((await docText()).startsWith('失败保留。'));
  await page.unroute('**/api/dictation/sessions/*/document');
  await page.getByRole('button', { name: '重试保存', exact: true }).click(); await saved();
  checks.push('failed save retains local text across refresh; retry commits the same operation once');

  // Same-source second window reads/writes through the same document version contract.
  const other = await context.newPage(); other.on('pageerror', e => errors.push(e.stack || e.message));
  await other.goto(`${base}/dictation?compact=1&session=${id}`);
  const otherContent = other.getByRole('textbox', { name: '正文编辑器', exact: true }); await otherContent.waitFor();
  await otherContent.click(); await other.keyboard.press('Control+End'); await other.keyboard.insertText('小窗补充。');
  await page.waitForFunction(() => window.notebookView().state.doc.toString().endsWith('小窗补充。'));
  assert.equal((await snapshot()).draft_text, await docText());
  checks.push('compact second window converges on the same server document');

  // Scroll and selection stay where the reader left them while background text arrives.
  await content().click(); await page.keyboard.press('Control+Home'); await page.keyboard.press('Shift+ArrowRight');
  await page.locator('.cm-scroller').evaluate(e => e.scrollTop = 0);
  const selection = await content().evaluate(() => window.notebookView().state.selection.main.toJSON());
  await context.request.post(`${base}/api/notebook-fixture/${id}/speech`);
  await page.waitForFunction(() => window.notebookView().state.doc.toString().endsWith('语音落字。'));
  assert.equal(await page.locator('.cm-scroller').evaluate(e => e.scrollTop), 0);
  assert.deepEqual(await content().evaluate(() => window.notebookView().state.selection.main.toJSON()), selection);
  assert(await content().evaluate(e => e === document.activeElement));
  checks.push('background insertion preserves selection, focus and upward reading position');

  await page.getByRole('button', { name: '整理全文', exact: true }).click();
  const previewEditor = () => page.getByRole('textbox', { name: '整理预览编辑器', exact: true });
  await previewEditor().waitFor();
  const original = (await snapshot()).draft_text;
  await previewEditor().click(); await page.keyboard.press('Control+a'); await page.keyboard.insertText('人工确认的整理结果😀\n第二段。');
  await page.getByRole('button', { name: '原稿', exact: true }).click();
  assert.equal(await page.getByRole('textbox', { name: '原稿', exact: true }).getAttribute('contenteditable'), 'false');
  await page.getByRole('button', { name: '显示修改', exact: true }).click();
  assert.equal(await page.locator('.cm-editor').count(), 1);
  await page.getByRole('button', { name: '整理结果', exact: true }).click();
  assert.match(await previewEditor().innerText(), /人工确认/);
  await page.screenshot({path:`${output}/preview-editor.png`,fullPage:true});
  await page.getByRole('button', { name: '复制预览', exact: true }).click();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), '人工确认的整理结果😀\n第二段。');
  await page.getByRole('button', { name: '应用到正文', exact: true }).click(); await content().waitFor(); await saved();
  assert.equal((await snapshot()).draft_text, '人工确认的整理结果😀\n第二段。');
  await content().click(); await page.keyboard.press('Control+z'); await saved();
  assert.equal((await snapshot()).draft_text, original);
  checks.push('single-area result/original/diff, editable candidate, copy-preview, apply-user-text and one-step undo');

  // Existing preview becomes stale when another window edits the base.
  // Keep this window on its pre-preview snapshot until its edit is sent;
  // otherwise polling can put it into preview before the click below.
  const beforePreview = await snapshot();
  await other.route(endpoint, route => route.request().method() === 'GET'
    ? route.fulfill({json:beforePreview}) : route.continue());
  await other.reload(); await otherContent.waitFor();
  await page.getByRole('button', { name: '整理全文', exact: true }).click(); await previewEditor().waitFor();
  await otherContent.click(); await other.keyboard.press('Control+Home'); await other.keyboard.insertText('另一窗口新稿。');
  await page.getByText('正文在整理期间或另一窗口中已变化', { exact: false }).waitFor();
  assert(await page.getByRole('button', { name: '应用到正文', exact: true }).isDisabled());
  await page.getByRole('button', { name: '放弃', exact: true }).click(); await content().waitFor();
  assert((await snapshot()).draft_text.startsWith('另一窗口新稿。'));
  await other.unroute(endpoint);
  checks.push('stale preview cannot replace a newer document; discard preserves the base');

  // Applying an earlier candidate must not discard edits made while its request is in flight.
  await page.getByRole('button', { name: '整理全文', exact: true }).click(); await previewEditor().waitFor();
  let releaseApply, applyHeld;
  const applyGate = new Promise(resolve => releaseApply = resolve);
  const applying = new Promise(resolve => applyHeld = resolve);
  await page.route('**/api/dictation/sessions/*/cleanup', async route => {
    if (route.request().postDataJSON()?.scope !== 'apply') return route.continue();
    applyHeld(); await applyGate; await route.continue();
  });
  await page.getByRole('button', { name: '应用到正文', exact: true }).click(); await applying;
  await previewEditor().click(); await page.keyboard.press('Control+End'); await page.keyboard.insertText('请求中新增的修改。');
  releaseApply();
  await page.getByText('应用期间预览又有修改', { exact:false }).waitFor();
  assert((await previewEditor().innerText()).endsWith('请求中新增的修改。'));
  assert(!(await snapshot()).draft_text.endsWith('请求中新增的修改。'));
  assert(await page.getByRole('button', { name:'应用到正文', exact:true }).isDisabled());
  await page.unroute('**/api/dictation/sessions/*/cleanup');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name:'放弃', exact:true }).click(); await content().waitFor();
  checks.push('editing a preview during apply retains the newer local candidate instead of silently losing it');

  await page.getByRole('button', { name: '切换至深色模式', exact: true }).click();
  for (const [name, width, height] of [['desktop',1159,756],['narrow',390,844]]) {
    await page.setViewportSize({ width, height });
    await page.evaluate(() => document.fonts.ready); await page.waitForTimeout(150);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const box = await page.locator('.dictation-notebook').boundingBox();
    assert(box.width > width * (width < 600 ? .8 : .4));
    assert(box.height >= 300);
    assert(box.y + box.height < height, `${name}: editor actions must remain visible`);
    const termsButton = page.getByRole('button', { name: '存为术语', exact: true });
    assert(await termsButton.evaluate(e => { const r = e.getBoundingClientRect(); return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)); }));
    await page.screenshot({ path: `${output}/${name}.png`, fullPage:true });
  }
  checks.push('1159x756 and 390px dark layout, one editor, internal scrolling and unobscured toolbar');

  // Start from inside an empty editor, then measure the actual recording UI.
  // The microphone is Chromium's virtual device; HTTP/WS session events are fixtures.
  const recordingLayouts = [];
  for (const [name, width, height] of [['desktop',1159,756],['narrow',390,844]]) {
    const livePage = await context.newPage();
    livePage.on('pageerror', error => errors.push(error.message));
    await livePage.setViewportSize({ width, height });
    const liveId = (name === 'desktop' ? 'c' : 'd').repeat(32);
    const liveState = { ...seeded, document_operations:[], id:liveId, status:'listening', phase:'正在听音', revision:1, event_sequence:1,
      samples:0, next_sequence:0, dictation_turns:[{id:1,start_sample:0,position:seeded.draft_text.length}],
      utterances:[{id:'1',revision:0,status:'partial',text:'',raw_text:'',
        preview:'正在识别的长句没有预先插入换行。'.repeat(160) + 'https://example.test/' + 'a'.repeat(600),
        edited:false,needs_review:false}] };
    let starts = 0;
    const resumeChecks = [];
    await livePage.route('**/api/dictation/sessions', route => {
      if (route.request().method() !== 'POST') return route.continue();
      starts++; return route.fulfill({ json:liveState });
    });
    await livePage.route(`**/api/dictation/sessions/${liveId}`, route => route.fulfill({ json:liveState }));
    await livePage.route(`**/api/dictation/sessions/${liveId}/document`, route => {
      const request = route.request().postDataJSON();
      assert.equal(request.base_version, liveState.document_version);
      for (const change of [...request.changes].reverse())
        liveState.draft_text = liveState.draft_text.slice(0,change.from) + change.insert + liveState.draft_text.slice(change.to);
      liveState.document_version++; liveState.content_revision = (liveState.content_revision ?? 0) + 1;
      liveState.revision++; liveState.event_sequence++;
      liveState.document_operations.push({id:request.operation_id,version:liveState.document_version,changes:request.changes,kind:'user'});
      return route.fulfill({json:liveState});
    });
    await livePage.routeWebSocket('**/api/dictation/sessions/*/stream', socket => {
      const sendSnapshot = () => socket.send(JSON.stringify({type:'session.snapshot',snapshot:liveState}));
      sendSnapshot();
      socket.onMessage(message => {
        if (typeof message !== 'string') {
          const view = new DataView(message.buffer, message.byteOffset, message.byteLength);
          liveState.next_sequence = view.getUint32(0,true) + 1;
          liveState.samples = Number(view.getBigUint64(4,true)) + (message.byteLength - 12) / 2;
          socket.send(JSON.stringify({type:'audio.ack',next_sequence:liveState.next_sequence,samples:liveState.samples}));
          return;
        }
        const command = JSON.parse(message);
        if (!['stop','pause','resume'].includes(command.type)) return;
        if (command.type === 'resume') resumeChecks.push({version:command.document_version,
          position:command.insertion_position, serverVersion:liveState.document_version, serverLength:liveState.draft_text.length});
        liveState.status = command.type === 'stop' ? 'completed' : command.type === 'pause' ? 'paused' : 'listening';
        liveState.phase = liveState.status === 'completed' ? '已停止，处理完成' : liveState.status === 'paused' ? '已暂停' : '正在听音';
        liveState.revision++; liveState.event_sequence++; sendSnapshot();
      });
    });
    await livePage.goto(`${base}/dictation`);
    const start = livePage.locator('.notebook-editor').getByRole('button', { name:'开始听写', exact:true });
    await start.waitFor();
    await livePage.waitForFunction(() => !document.querySelector('.page-enter-active, .page-leave-active'));
    await livePage.evaluate(() => document.fonts.ready);
    assert.equal(await livePage.getByRole('button', { name:'开始听写', exact:true }).count(), 1);
    const idle = await livePage.locator('.cm-scroller').boundingBox();
    await livePage.screenshot({ path:`${output}/${name}-empty-start.png`, fullPage:true });
    if (name === 'desktop') {
      await livePage.evaluate(() => {
        const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        navigator.mediaDevices.getUserMedia = async constraints => {
          await new Promise(resolve => window.releaseStartPermission = resolve);
          navigator.mediaDevices.getUserMedia = original;
          return original(constraints);
        };
      });
    }
    await start.click();
    if (name === 'desktop') {
      await livePage.waitForFunction(() => !!window.releaseStartPermission);
      assert.equal(await livePage.getByRole('textbox',{name:'正文编辑器',exact:true}).getAttribute('contenteditable'), 'false');
      await livePage.evaluate(() => window.releaseStartPermission());
    }
    const dock = livePage.getByRole('complementary', { name:'常驻语音控制' });
    await livePage.waitForFunction(() => document.querySelector('.dictation-dock__signal')?.classList.contains('is-on'));
    assert.equal(await livePage.getByRole('textbox',{name:'正文编辑器',exact:true}).getAttribute('contenteditable'), 'true');
    await livePage.waitForFunction(() => {
      const dock = document.querySelector('.dictation-dock'), main = document.querySelector('.app-content');
      return document.querySelector('.notebook-partial') && !document.querySelector('.notebook-empty')
        && parseFloat(getComputedStyle(main).getPropertyValue('--dictation-dock-height')) >= dock.getBoundingClientRect().height;
    });
    await livePage.evaluate(() => scrollTo(0,0));
    const editor = await livePage.locator('.cm-scroller').boundingBox();
    const panel = await dock.boundingBox();
    assert.equal(starts, 1);
    assert(editor.height >= idle.height - 64, `${name}: floating panel must not take height away from the editor`);
    assert(editor.height >= 320, `${name}: recording needs a usable writing viewport with a two-line partial`);
    const textLayout = () => livePage.locator('.notebook-partial p').evaluate(p => {
      const scroller = document.querySelector('.cm-scroller');
      return { pageWidth:document.documentElement.scrollWidth, viewport:innerWidth,
        editorWidth:scroller.clientWidth, editorScrollWidth:scroller.scrollWidth,
        partialWidth:p.clientWidth, partialScrollWidth:p.scrollWidth,
        partialHeight:p.clientHeight, partialScrollHeight:p.scrollHeight, lineHeight:parseFloat(getComputedStyle(p).lineHeight) };
    });
    const wrapped = await textLayout();
    assert(wrapped.pageWidth <= width, `${name}: long partial must not widen the page`);
    assert(wrapped.editorScrollWidth <= wrapped.editorWidth + 1, `${name}: body must keep wrapping`);
    assert(wrapped.partialScrollWidth <= wrapped.partialWidth + 1, `${name}: long Chinese and URL must wrap in the partial`);
    assert(wrapped.partialHeight <= wrapped.lineHeight * 2 + 1 && wrapped.partialScrollHeight > wrapped.partialHeight,
      `${name}: partial must scroll vertically within two lines`);
    assert.equal(panel.width, 310, `${name}: original floating-panel width must be preserved`);
    const handle = await dock.locator('.dictation-dock__drag-bar').boundingBox();
    await livePage.mouse.move(handle.x + handle.width/2, handle.y + handle.height/2);
    await livePage.mouse.down();
    await livePage.mouse.move(width - panel.width/2 - 8, 90, {steps:6});
    await livePage.mouse.up();
    const moved = await dock.boundingBox();
    const afterDrag = await livePage.locator('.cm-scroller').boundingBox();
    assert(Math.abs(moved.y - panel.y) > 30, `${name}: panel must remain draggable`);
    assert.equal(moved.width, panel.width); assert.equal(moved.height, panel.height);
    assert(Math.abs(afterDrag.height - editor.height) < 1, `${name}: dragging must not resize the editor`);
    await livePage.screenshot({ path:`${output}/${name}-recording-dragged.png`, fullPage:true });
    recordingLayouts.push({name,viewport:{width,height},idleEditor:idle,recordingEditor:editor,floatingPanel:panel,draggedPanel:moved,wrapped});
    if (name === 'narrow') {
      await livePage.setViewportSize({width:375,height:667});
      const phone = await textLayout();
      assert(phone.pageWidth <= 375 && phone.partialScrollWidth <= phone.partialWidth + 1, '375px phone: no horizontal overflow');
      await livePage.screenshot({path:`${output}/phone-long-partial.png`,fullPage:true});
      recordingLayouts.push({name:'375px phone long partial',wrapped:phone});
    }
    if (name === 'desktop') {
      await dock.getByRole('button', { name:'暂停听写', exact:true }).click();
      await dock.getByRole('button', { name:'继续听写', exact:true }).waitFor();
      await livePage.evaluate(() => {
        const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        navigator.mediaDevices.getUserMedia = async constraints => {
          await new Promise(resolve => window.releaseResumePermission = resolve);
          navigator.mediaDevices.getUserMedia = original;
          return original(constraints);
        };
      });
      await dock.getByRole('button', { name:'继续听写', exact:true }).click();
      await livePage.waitForFunction(() => !!window.releaseResumePermission);
      // Dispatch and release in one browser task: the 450ms autosave timer has
      // not fired, so resume itself must flush the permission-wait edit.
      await livePage.evaluate(() => {
        const view = window.notebookView(), end = view.state.doc.length, text = '权限等待期间补写。';
        view.dispatch({changes:{from:end,insert:text},selection:{anchor:end + text.length}});
        window.releaseResumePermission();
      });
      await livePage.waitForFunction(() => document.querySelector('.dictation-dock__signal')?.classList.contains('is-on'));
      assert.equal(resumeChecks.length,1);
      assert.equal(resumeChecks[0].version,resumeChecks[0].serverVersion);
      assert.equal(resumeChecks[0].position,resumeChecks[0].serverLength);
      assert(liveState.draft_text.endsWith('权限等待期间补写。'));
      checks.push('resume saves edits made during microphone permission before sending its insertion anchor');
    }
    await dock.getByRole('button', { name:'结束听写', exact:true }).click();
    await dock.waitFor({ state:'hidden' });
    await livePage.close();
  }
  checks.push('empty-editor start, tall active editor, two-line long partial wrapping at 1159/390/375px; floating panel keeps its size and drag behavior');
  await writeFile(`${output}/recording-layouts.json`, JSON.stringify(recordingLayouts,null,2));
  assert.deepEqual(errors, []);
  await writeFile(`${output}/report.json`, JSON.stringify({ checks, errors, real_editor_api_storage:true, speech_and_cleanup:'deterministic fixtures', physical_microphone:false },null,2));
  console.log(output + '\n' + checks.join('\n'));
} catch (error) {
  await writeFile(`${output}/failure.json`, JSON.stringify({ error: String(error), checks, errors },null,2));
  throw error;
} finally {
  await browser?.close();
  for (const child of [vite, server]) { child.kill('SIGTERM'); await new Promise(r => child.exitCode !== null ? r() : child.once('exit', r)); }
  await apiLog.close(); await viteLog.close();
}
