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
  let seeded;
  if (!process.env.DICTATION_TEST_UNIFIED_ONLY) {
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
  seeded = await (await context.request.post(`${base}/api/notebook-fixture/seed`)).json();
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

  }
  seeded ??= await (await context.request.post(`${base}/api/notebook-fixture/seed`)).json();

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
    let starts = 0, sendLiveSnapshot, releaseControl;
    let holdNextControl, heldControl, rejectResume = false, rejectPause = false, failSave = false, holdSave = false, releaseSave;
    const controlQueue = [], controls = [], resumeChecks = [];
    const mapPosition = (position, changes, assoc=1) => {
      let offset = 0;
      for (const c of changes) {
        if (position < c.from) break;
        if (position <= c.to) return c.from + offset + (assoc > 0 ? c.insert.length : 0);
        offset += c.insert.length - (c.to - c.from);
      }
      return position + offset;
    };
    const applyChanges = (changes, kind, operationId, utteranceId) => {
      for (const change of [...changes].reverse())
        liveState.draft_text = liveState.draft_text.slice(0,change.from) + change.insert + liveState.draft_text.slice(change.to);
      for (const turn of liveState.dictation_turns) turn.position = mapPosition(turn.position, changes);
      liveState.document_version++; liveState.content_revision = (liveState.content_revision ?? 0) + 1;
      liveState.revision++; liveState.event_sequence++;
      liveState.document_operations.push({id:operationId,version:liveState.document_version,changes,kind,utterance_id:utteranceId});
    };
    const saveDocument = async route => {
      if (holdSave) { holdSave=false; await new Promise(resolve => releaseSave=resolve); }
      if (failSave) return route.fulfill({status:503,json:{detail:'fixture: cursor save unavailable'}});
      const request = route.request().postDataJSON();
      let changes=request.changes;
      // Match the real API's versioned rebase when ASR wins a held save race.
      for (const operation of liveState.document_operations.filter(op => op.version > request.base_version))
        changes=changes.map(change => ({...change,from:mapPosition(change.from,operation.changes),
          to:mapPosition(change.to,operation.changes,change.from===change.to ? 1 : -1)}));
      applyChanges(changes, 'user', request.operation_id);
      return route.fulfill({json:liveState});
    };
    const speech = (text, turn = liveState.dictation_turns.at(-1)) => {
      const utteranceId = `fixture-speech-${liveState.document_version}`;
      liveState.utterances.push({id:utteranceId,text,raw_text:text,revision:1,start_sample:turn.start_sample,
        status:'raw',edited:false,needs_review:false,preview:''});
      applyChanges([{from:turn.position,to:turn.position,insert:text}], 'speech', utteranceId, utteranceId);
      sendLiveSnapshot();
    };
    await livePage.route('**/api/dictation/sessions', route => {
      if (route.request().method() !== 'POST') return route.continue();
      starts++; return route.fulfill({ json:liveState });
    });
    await livePage.route(`**/api/dictation/sessions/${liveId}`, route => route.fulfill({ json:liveState }));
    await livePage.route(`**/api/dictation/sessions/${liveId}/document`, saveDocument);
    await livePage.routeWebSocket('**/api/dictation/sessions/*/stream', socket => {
      sendLiveSnapshot = () => socket.send(JSON.stringify({type:'session.snapshot',snapshot:liveState}));
      const applyControl = command => {
        if (command.type === 'pause' && rejectPause) {
          rejectPause = false;
          socket.send(JSON.stringify({type:'session.control_rejected',error:'fixture: pause not confirmed'}));
          return;
        }
        if (command.type === 'resume') {
          resumeChecks.push({version:command.document_version, position:command.insertion_position,
            serverVersion:liveState.document_version, serverLength:liveState.draft_text.length});
          if (rejectResume) {
            rejectResume = false;
            socket.send(JSON.stringify({type:'session.control_rejected',error:'fixture: insertion version rejected'}));
            return;
          }
          assert.equal(command.document_version, liveState.document_version);
          liveState.dictation_turns.push({id:liveState.dictation_turns.length+1,start_sample:command.samples,position:command.insertion_position});
        }
        liveState.status = command.type === 'stop' ? 'completed' : command.type === 'pause' ? 'paused' : 'listening';
        liveState.phase = liveState.status === 'completed' ? '已停止，处理完成' : liveState.status === 'paused' ? '已暂停' : '正在听音';
        liveState.revision++; liveState.event_sequence++; sendLiveSnapshot();
      };
      releaseControl = () => {
        const command = heldControl; heldControl = undefined;
        applyControl(command);
        for (const queued of controlQueue.splice(0)) applyControl(queued);
      };
      sendLiveSnapshot();
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
        controls.push(command);
        if (heldControl) { controlQueue.push(command); return; }
        if (holdNextControl === command.type) {
          holdNextControl = undefined; heldControl = command;
          if (command.type === 'pause') {
            liveState.status='pausing'; liveState.phase='正在暂停，完成暂停前的尾句';
            liveState.revision++; liveState.event_sequence++; sendLiveSnapshot();
          }
          return;
        }
        applyControl(command);
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
    assert.equal(panel.width, 236, `${name}: compact floating-panel width must be preserved`);
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
      const caret = () => livePage.evaluate(() => window.notebookView().state.selection.main.to);
      const liveText = () => livePage.evaluate(() => window.notebookView().state.doc.toString());
      const listening = () => livePage.waitForFunction(() => document.querySelector('.dictation-dock__signal')?.classList.contains('is-on'));
      const selectAt = position => livePage.evaluate(position => {
        window.notebookView().dispatch({selection:{anchor:position},scrollIntoView:true});
      }, position);
      const until = async predicate => {
        for (let i=0; i<200; i++) { if (predicate()) return; await new Promise(r => setTimeout(r,20)); }
        assert.fail('fixture condition did not settle');
      };
      const waitText = text => livePage.waitForFunction(text => window.notebookView().state.doc.toString() === text, text);
      const waitSaved = () => livePage.locator('.notebook-save').filter({hasText:/^已保存$/}).waitFor();
      assert.equal(await livePage.locator('.dictation-insertion-anchor').count(),0);
      assert.equal(await caret(),liveState.dictation_turns.at(-1).position);
      speech('连续语音一。'); await waitText(liveState.draft_text);
      assert.equal(await caret(), liveState.dictation_turns.at(-1).position);
      speech('连续语音二。'); await waitText(liveState.draft_text);
      assert.equal(await caret(), liveState.dictation_turns.at(-1).position);
      await livePage.getByRole('textbox',{name:'正文编辑器',exact:true}).focus();
      const beforeTyping = resumeChecks.length, typingOldTurn=liveState.dictation_turns.at(-1);
      holdSave=true;
      await livePage.keyboard.type('键盘补充。',{delay:160}); await livePage.keyboard.press('Enter');
      await until(() => !!releaseSave);
      assert.equal(liveState.status,'paused');
      assert.equal(await dock.locator('.dictation-dock__signal.is-on').count(),0);
      speech('键入前迟到尾句。',typingOldTurn);
      await livePage.waitForFunction(() => window.notebookView().state.doc.toString().includes('键入前迟到尾句。'));
      assert((await liveText()).endsWith('键盘补充。\n'));
      assert.equal(resumeChecks.length,beforeTyping,'new speech capture must wait for the blocked keyboard/Enter save');
      releaseSave(); releaseSave=undefined;
      await waitSaved(); await listening();
      assert.equal(resumeChecks.length,beforeTyping+1,'continuous typing/Enter should share one new boundary');
      assert.equal(await caret(),liveState.dictation_turns.at(-1).position);
      speech('键入后新语音。'); await waitText(liveState.draft_text);
      assert(liveState.draft_text.endsWith('键盘补充。\n键入后新语音。'));
      assert.equal(await caret(),liveState.dictation_turns.at(-1).position);
      checks.push('one caret advances through continuous speech; typing/Enter pause until a blocked save commits, old tails stay old and newly captured speech follows the typed newline');

      const oldTurn = liveState.dictation_turns[0];
      await livePage.getByRole('button',{name:'回到光标',exact:true}).click();
      // Actual mouse click into a different paragraph, not a synthetic selection.
      await livePage.locator('.cm-scroller').evaluate(e => e.scrollTop = 0);
      await livePage.locator('.cm-line').nth(2).click({position:{x:65,y:10}});
      await listening();
      assert.equal(await caret(),liveState.dictation_turns.at(-1).position);
      const clicked = await caret();
      speech('这里开始听写。'); await waitText(liveState.draft_text);
      assert.equal(await caret(),clicked + '这里开始听写。'.length);
      const beforeOld = await caret();
      speech('迟到旧句。',oldTurn); await waitText(liveState.draft_text);
      assert.equal(await caret(),beforeOld,'old-turn speech must not steal the moved caret');
      await livePage.screenshot({path:`${output}/unified-cursor-desktop.png`,fullPage:true,caret:'initial' });
      checks.push('real mouse relocation binds a new audio turn; late old speech stays at its original paragraph without stealing the caret');

      let before = resumeChecks.length;
      await livePage.keyboard.press('ArrowLeft'); await livePage.keyboard.press('ArrowLeft'); await livePage.keyboard.press('ArrowLeft');
      await listening();
      assert.equal(resumeChecks.length,before+1,'rapid arrow moves should coalesce into one boundary');
      assert.equal(await caret(),liveState.dictation_turns.at(-1).position);
      await livePage.keyboard.press('Enter'); await livePage.keyboard.insertText('移动后键入。'); await waitSaved(); await listening();
      speech('换行后语音。'); await waitText(liveState.draft_text);
      assert.equal(await caret(),liveState.dictation_turns.at(-1).position);
      checks.push('rapid keyboard moves coalesce; Enter and subsequent typing/speech share the new visible insertion point');

      const selectedOriginal = liveState.draft_text.slice(1,4), selectionBase = liveState.draft_text;
      await livePage.evaluate(() => window.notebookView().dispatch({selection:{anchor:1,head:4},scrollIntoView:true}));
      await listening();
      speech('选区末尾。'); await waitText(liveState.draft_text);
      assert.equal(liveState.draft_text,selectionBase.slice(0,4)+'选区末尾。'+selectionBase.slice(4));
      assert.equal(liveState.draft_text.slice(1,4),selectedOriginal);
      assert.equal(await caret(),4+'选区末尾。'.length);
      await livePage.keyboard.press('Control+z'); await waitSaved();
      assert.equal(await liveText(),selectionBase);
      await livePage.keyboard.press('Control+Shift+z'); await waitSaved(); await listening();
      assert.equal(await caret(),liveState.dictation_turns.at(-1).position);
      checks.push('selection-end speech preserves selected original text, advances to one caret and remains independently undoable/redoable');

      // IME events + CodeMirror transactions exercise the integration; physical
      // Chinese IME candidate-window behavior remains a device acceptance step.
      before = resumeChecks.length;
      await livePage.evaluate(() => {
        const view=window.notebookView();
        view.dispatch({selection:{anchor:25},scrollIntoView:true});
        view.contentDOM.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));
        view.dispatch({changes:{from:25,insert:'中文组词'},selection:{anchor:29}});
      });
      await until(() => liveState.status === 'paused');
      const duringIME = await liveText();
      speech('组词时旧句。');
      await livePage.waitForTimeout(180);
      assert.equal(await liveText(),duringIME,'remote speech must remain deferred while composing');
      assert.equal(resumeChecks.length,before,'composition must not automatically resume capture');
      await livePage.evaluate(() => window.notebookView().contentDOM.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true})));
      await listening(); await waitSaved();
      assert.equal(await liveText(),liveState.draft_text);
      assert.equal(await caret(),liveState.dictation_turns.at(-1).position);
      checks.push('composition holds automatic resume and buffers remote speech until compositionend, then saves/rebinds the resulting caret');

      holdNextControl='resume';
      await selectAt(45); await until(() => !!heldControl);
      assert(await dock.getByText('正在切换输入位置 · 麦克风暂时关闭，请稍候再说',{exact:true}).isVisible());
      await selectAt(60); await selectAt(75);
      releaseControl(); await listening();
      assert.equal(resumeChecks.at(-1).position,75);
      assert.equal(await caret(),75);
      checks.push('movement during delayed resume confirmation never starts at the obsolete target; latest cursor wins');

      await livePage.evaluate(() => {
        const NativeContext=window.AudioContext;
        window.AudioContext=class extends NativeContext {
          constructor(...args) {
            super(...args); window.AudioContext=NativeContext; window.movingContext=this;
            const add=this.audioWorklet.addModule.bind(this.audioWorklet);
            this.audioWorklet.addModule=async (...args) => {
              await new Promise(resolve => window.releaseMoveWorklet=resolve); return add(...args);
            };
          }
        };
        window.notebookView().dispatch({selection:{anchor:80},scrollIntoView:true});
      });
      await livePage.waitForFunction(() => !!window.releaseMoveWorklet);
      await selectAt(85);
      await livePage.evaluate(() => window.releaseMoveWorklet());
      await listening();
      assert.equal(resumeChecks.at(-1).position,85);
      assert.equal(await livePage.evaluate(() => window.movingContext.state),'closed');
      checks.push('movement during worklet startup closes the obsolete lease and restarts the same transition at the latest caret');

      for (const phase of ['pause','resume']) {
        if (phase === 'pause') await dock.getByTitle('收起面板',{exact:true}).click();
        holdNextControl=phase;
        await selectAt(phase === 'pause' ? 90 : 100); await until(() => !!heldControl);
        const resumeCount = resumeChecks.length;
        await dock.getByRole('button',{name:'暂停听写',exact:true}).click();
        releaseControl();
        await dock.getByRole('button',{name:'继续听写',exact:true}).waitFor();
        await livePage.waitForTimeout(180);
        assert.equal(liveState.status,'paused');
        assert.equal(await dock.locator('.dictation-dock__signal.is-on').count(),0);
        assert.equal(resumeChecks.length,resumeCount+(phase === 'resume' ? 1 : 0));
        await dock.getByRole('button',{name:'继续听写',exact:true}).click(); await listening();
        if (phase === 'pause') await dock.getByTitle('展开面板',{exact:true}).click();
      }
      checks.push('expanded/collapsed explicit pause cancels automatic recovery during server pausing and resume acknowledgement races');

      await livePage.evaluate(() => {
        const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        window.cursorPermissionRequests=0;
        navigator.mediaDevices.getUserMedia=constraints => { window.cursorPermissionRequests++; return original(constraints); };
      });
      holdSave=true;
      await selectAt(110); await livePage.keyboard.insertText('等待保存。');
      await until(() => !!releaseSave);
      const permissionBeforeSave=await livePage.evaluate(() => window.cursorPermissionRequests);
      await dock.getByRole('button',{name:'暂停听写',exact:true}).click();
      releaseSave(); releaseSave=undefined;
      await dock.getByRole('button',{name:'继续听写',exact:true}).waitFor();
      await livePage.waitForTimeout(180);
      assert.equal(await livePage.evaluate(() => window.cursorPermissionRequests),permissionBeforeSave,
        'a cancelled save wait must not acquire a fresh microphone lease');
      assert.equal(await dock.locator('.dictation-dock__signal.is-on').count(),0);
      await dock.getByRole('button',{name:'继续听写',exact:true}).click(); await listening();
      checks.push('explicit pause while relocation is saving invalidates the transition before any new microphone permission/lease');

      failSave=true;
      await selectAt(120); await livePage.keyboard.insertText('保存失败保留。');
      await livePage.getByRole('alert').filter({hasText:'输入位置未切换'}).first().waitFor();
      assert.equal(liveState.status,'paused');
      assert.equal(await dock.locator('.dictation-dock__signal.is-on').count(),0);
      assert((await liveText()).includes('保存失败保留。'));
      failSave=false;
      await livePage.getByRole('button',{name:'重试保存',exact:true}).click(); await waitSaved();
      await dock.getByRole('button',{name:'继续听写',exact:true}).click(); await listening();
      rejectResume=true;
      await selectAt(135);
      await livePage.getByRole('alert').filter({hasText:'fixture: insertion version rejected'}).first().waitFor();
      assert.equal(liveState.status,'paused');
      assert.equal(await dock.locator('.dictation-dock__signal.is-on').count(),0);
      await livePage.screenshot({path:`${output}/unified-cursor-recoverable-error.png`,fullPage:true});
      await dock.getByRole('button',{name:'继续听写',exact:true}).click(); await listening();
      rejectPause=true;
      await selectAt(140);
      await livePage.getByRole('alert').filter({hasText:'fixture: pause not confirmed'}).first().waitFor();
      assert.equal(liveState.status,'listening');
      assert.equal(await dock.locator('.dictation-dock__signal.is-on').count(),0);
      await dock.getByRole('button',{name:'暂停听写',exact:true}).click();
      await dock.getByRole('button',{name:'继续听写',exact:true}).waitFor();
      await dock.getByRole('button',{name:'继续听写',exact:true}).click(); await listening();
      checks.push('failed save, rejected resume and unconfirmed pause keep audio off with local text and explicit recovery controls');

      const reader = await context.newPage(); reader.on('pageerror',error => errors.push(error.message));
      await reader.route(`**/api/dictation/sessions/${liveId}`,route => route.fulfill({json:liveState}));
      await reader.route(`**/api/dictation/sessions/${liveId}/document`,saveDocument);
      await reader.goto(`${base}/dictation?compact=1&session=${liveId}`);
      await reader.getByRole('textbox',{name:'正文编辑器',exact:true}).waitFor();
      await reader.waitForFunction(text => window.notebookView?.()?.state.doc.toString() === text,liveState.draft_text);
      before=controls.length;
      await reader.evaluate(() => window.notebookView().dispatch({selection:{anchor:5}}));
      await reader.getByRole('textbox',{name:'正文编辑器',exact:true}).focus();
      await reader.keyboard.insertText('旁窗。');
      await reader.locator('.notebook-save').filter({hasText:/^已保存$/}).waitFor();
      await waitText(liveState.draft_text);
      assert.equal(controls.length,before);
      assert.equal(await dock.locator('.dictation-dock__signal.is-on').count(),1);
      await reader.close();
      await livePage.getByRole('button',{name:'历史结果',exact:true}).click();
      await livePage.getByRole('button',{name:'打开',exact:true}).first().click();
      await livePage.getByRole('textbox',{name:'正文编辑器',exact:true}).waitFor();
      await livePage.evaluate(() => window.notebookView().dispatch({selection:{anchor:2}}));
      await livePage.waitForTimeout(180);
      assert.equal(controls.length,before);
      await livePage.getByRole('button',{name:'整理全文',exact:true}).click();
      await livePage.getByRole('textbox',{name:'整理预览编辑器',exact:true}).waitFor();
      await livePage.evaluate(() => window.notebookView().dispatch({changes:{from:0,insert:'历史预览。'},selection:{anchor:5}}));
      assert.equal(controls.length,before);
      livePage.once('dialog',dialog => dialog.accept());
      await livePage.getByRole('button',{name:'放弃',exact:true}).click();
      await dock.getByRole('button',{name:'正文',exact:true}).click();
      await waitText(liveState.draft_text);
      checks.push('a second window, history and cleanup-preview editing cannot rebind the owner microphone or interrupt its capture');
    }
    if (name === 'desktop') {
      const resumeBeforePermission = resumeChecks.length;
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
      assert.equal(resumeChecks.length,resumeBeforePermission+1);
      assert.equal(resumeChecks.at(-1).version,resumeChecks.at(-1).serverVersion);
      assert.equal(resumeChecks.at(-1).position,resumeChecks.at(-1).serverLength);
      assert(liveState.draft_text.endsWith('权限等待期间补写。'));
      checks.push('resume saves edits made during microphone permission before sending its insertion anchor');
    }
    if (name === 'desktop') {
      await livePage.evaluate(() => {
        const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        navigator.mediaDevices.getUserMedia=async constraints => {
          await new Promise(resolve => window.releaseMovePermission=resolve);
          navigator.mediaDevices.getUserMedia=original;
          const stream=await original(constraints); window.lateMoveTracks=stream.getTracks(); return stream;
        };
        window.notebookView().dispatch({selection:{anchor:165},scrollIntoView:true});
      });
      await livePage.waitForFunction(() => !!window.releaseMovePermission);
      const resumeBeforeStop=resumeChecks.length;
      await dock.getByRole('button',{name:'结束听写',exact:true}).click();
      await livePage.evaluate(() => window.releaseMovePermission());
      await livePage.waitForFunction(() => window.lateMoveTracks?.every(track => track.readyState==='ended'));
      assert.equal(resumeChecks.length,resumeBeforeStop);
      checks.push('ending during automatic microphone preparation cancels its lease; late permission closes every track and cannot resume');
    } else {
    await dock.getByRole('button', { name:'结束听写', exact:true }).click();
    }
    await dock.waitFor({ state:'hidden' });
    await livePage.close();
  }
  checks.push('empty-editor start, tall active editor, two-line long partial wrapping at 1159/390/375px; floating panel keeps its size and drag behavior');
  await writeFile(`${output}/recording-layouts.json`, JSON.stringify(recordingLayouts,null,2));
  assert.deepEqual(errors, []);
  await writeFile(`${output}/report.json`, JSON.stringify({ checks, errors, real_editor_api_storage:true, speech_and_cleanup:'deterministic fixtures', physical_microphone:false },null,2));
  console.log(output + '\n' + checks.join('\n'));
} catch (error) {
  for (const [index,page] of (browser?.contexts().flatMap(context => context.pages()) ?? []).entries())
    await page.screenshot({path:`${output}/failure-page-${index}.png`,fullPage:true}).catch(() => {});
  await writeFile(`${output}/failure.json`, JSON.stringify({ error: String(error), checks, errors },null,2));
  throw error;
} finally {
  await browser?.close();
  for (const child of [vite, server]) { child.kill('SIGTERM'); await new Promise(r => child.exitCode !== null ? r() : child.once('exit', r)); }
  await apiLog.close(); await viteLog.close();
}
