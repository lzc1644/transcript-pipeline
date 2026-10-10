// Run against tests.frontend_workbench_server, never a production API.
// Connect to an existing layout-capable CDP browser; no bundled browser launch.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const base = process.env.WORKBENCH_URL ?? 'http://127.0.0.1:5179';
const endpoint = process.env.CDP_URL;
const output = process.env.WORKBENCH_OUTPUT;
assert(endpoint && output, 'Set CDP_URL and WORKBENCH_OUTPUT (scratch directory)');
const marker = await fetch(`${base}/api/__workbench_test__`).then(r => r.json());
assert(marker.isolated && marker.model_execution === false, 'Refusing to test a non-isolated API');
await mkdir(output, { recursive: true });
const browser = await chromium.connectOverCDP(endpoint);
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(10000);
const errors = [];
const results = [];
page.on('pageerror', error => errors.push(error.message));
const routes = ['single-job', 'batch-job', 'jobs', 'stage-runner', 'pdf-book-ocr', 'proofreading-memory', 'settings'];
async function check(name, run) {
  if (process.env.WORKBENCH_FILTER && !new RegExp(process.env.WORKBENCH_FILTER).test(name)) return;
  await run(); results.push(name); console.log(`PASS ${name}`);
}
async function visit(route) {
  await page.goto(`${base}/${route}`);
  await page.locator('.page-heading').waitFor();
  await page.waitForTimeout(250);
}
async function overflow() {
  const value = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
  assert(value.scroll <= value.width + 1, `Horizontal overflow: ${JSON.stringify(value)}`);
}
try {
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const theme of ['light', 'dark']) {
      await visit('single-job');
      const isDark = await page.evaluate(() => document.documentElement.classList.contains('dark-mode'));
      if (isDark !== (theme === 'dark')) await page.locator('.theme-toggle-btn').click();
      for (const route of routes) {
        await check(`${route}: ${width}px ${theme}`, async () => {
          await visit(route);
          await overflow();
          assert(await page.locator('#main-content').isVisible());
          assert.equal(await page.evaluate(() => document.documentElement.classList.contains('dark-mode')), theme === 'dark');
          await page.evaluate(() => scrollTo(0, 0));
          await page.screenshot({ path: `${output}/${route}-${width}-${theme}.png`, fullPage: true });
        });
      }
    }
  }
  await check('mobile drawer: keyboard, close, focus restoration, all routes', async () => {
    await visit('single-job');
    const menu = page.getByRole('button', { name: '打开导航菜单' });
    await menu.focus(); await page.keyboard.press('Enter');
    await page.locator('#mobile-navigation').waitFor();
    await page.keyboard.press('Tab');
    assert(await page.evaluate(() => Boolean(document.activeElement?.closest('.n-drawer'))));
    await page.keyboard.press('Escape');
    await page.locator('#mobile-navigation').waitFor({ state: 'hidden' });
    await page.waitForTimeout(250);
    assert.equal(await menu.evaluate(e => e === document.activeElement), true);
    for (const route of routes) {
      await menu.click();
      await page.locator(`#mobile-navigation a[href="/${route}"]`).click();
      await page.locator('#mobile-navigation').waitFor({ state: 'hidden' });
      assert.equal(new URL(page.url()).pathname, `/${route}`);
    }
  });
  await check('advanced options preserve edits and theme does not reset form', async () => {
    await visit('single-job');
    const details = page.locator('details.advanced-options').first();
    const summary = details.locator('summary');
    await summary.click();
    const input = page.getByPlaceholder('例如：第 1 章 导言');
    await input.fill('界面回归章节');
    await summary.click(); await summary.click();
    assert.equal(await input.inputValue(), '界面回归章节');
    await page.locator('.theme-toggle-btn').click();
    assert.equal(await input.inputValue(), '界面回归章节');
    await overflow();
  });
  await check('select popup fits mobile viewport', async () => {
    await visit('single-job');
    await page.locator('.asr-candidate-selector .n-base-selection').click();
    const popup = page.locator('.n-base-select-menu:visible');
    await popup.waitFor();
    const box = await popup.boundingBox();
    assert(box && box.x >= -1 && box.x + box.width <= 391, `Popup clipped: ${JSON.stringify(box)}`);
    await page.keyboard.press('Escape');
  });
  await check('settings save uses real isolated API', async () => {
    await visit('settings');
    const response = page.waitForResponse(r => r.url().endsWith('/api/frontend-settings') && r.request().method() === 'PUT');
    await page.getByRole('button', { name: /保存/ }).click();
    assert.equal((await response).status(), 200);
  });
  await check('real upload → existing export stage → download', async () => {
    await visit('stage-runner');
    await page.locator('.select-stage .n-base-selection').click();
    await page.getByText('导出文档 (export-markdown)', { exact: true }).click();
    const input = page.locator('input[type=file]').first();
    await input.setInputFiles({ name: 'workbench-refined.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({ final_markdown: '# 工作台验证\n\n本地文件导出验证。' })) });
    await page.getByText('workbench-refined.json', { exact: true }).waitFor();
    const submitted = page.waitForResponse(r => r.url().includes('/export-markdown/file-run') && r.request().method() === 'POST');
    await page.getByRole('button', { name: '运行并生成下载包', exact: true }).click();
    const response = await submitted;
    assert.equal(response.status(), 202);
    const { run_id } = await response.json();
    let state;
    for (let attempt = 0; attempt < 30; attempt++) {
      state = await fetch(`${base}/api/stage-runs/${run_id}`).then(r => r.json());
      if (state.status === 'success' || state.status === 'failed') break;
      await page.waitForTimeout(200);
    }
    assert.equal(state.status, 'success', JSON.stringify(state));
    const result = await fetch(`${base}/api/stage-runs/${run_id}/result`);
    assert.equal(result.status, 200);
    const bytes = Buffer.from(await result.arrayBuffer());
    assert.equal(bytes.subarray(0, 2).toString(), 'PK');
    await writeFile(`${output}/real-export.zip`, bytes);
    await writeFile(`${output}/real-export-state.json`, JSON.stringify(state, null, 2));
    await page.locator('.file-result-panel').waitFor();
    const downloadEvent = page.waitForEvent('download');
    await page.getByRole('button', { name: '下载结果 ZIP', exact: true }).click();
    const download = await downloadEvent;
    await download.saveAs(`${output}/real-export-button.zip`);
    await overflow();
  });
  await check('task status samples and delete cancel', async () => {
    await visit('jobs');
    const failed = page.locator('.status-card').filter({ hasText: 'ui-example-failed' });
    assert.equal(await failed.count(), 1);
    await failed.getByRole('button', { name: /删除/ }).click();
    await page.locator('.n-dialog').waitFor();
    await page.getByRole('button', { name: '不删除', exact: true }).click();
    assert.equal(await failed.count(), 1);
    const running = page.locator('.status-card').filter({ hasText: 'ui-example-running' });
    assert.equal(await running.count(), 1);
    await overflow();
  });
  await check('task search: keywords, clear, refresh and categories', async () => {
    await page.route('**/api/batches', route => route.fulfill({ json: { items: [{ id: 'search-batch', kind: 'batch', status: 'success', items: [{ job_id: 'child-1', video_source: '/sample/批量检索样本.mp4', status: 'success' }] }] } }));
    await visit('jobs');
    const input = page.getByRole('textbox', { name: '搜索任务' });
    await input.fill('  UI-EXAMPLE   第二章  ');
    await page.waitForFunction(() => document.querySelectorAll('.status-card').length === 1);
    assert((await page.locator('.status-card').innerText()).includes('ui-example-failed'));
    await page.getByRole('button', { name: '刷新任务列表' }).click();
    assert.equal(await input.inputValue(), '  UI-EXAMPLE   第二章  ');
    await input.fill('找不到的任务xyz');
    await page.getByText('没有匹配的单任务，试试其他关键词。', { exact: true }).waitFor();
    await page.getByRole('button', { name: '清空搜索', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.status-card').length === 3);
    await input.fill('批量检索样本');
    await page.locator('.n-tabs-tab[data-name="batches"]').click();
    await page.locator('.status-card').filter({ hasText: 'search-batch' }).waitFor();
    await input.fill('export-markdown');
    await page.locator('.n-tabs-tab[data-name="stage-runs"]').click();
    await page.locator('.status-card').first().waitFor();
    await overflow();
    await page.screenshot({ path: `${output}/task-search-mobile.png`, fullPage: true });
    await page.unroute('**/api/batches');
  });
  await check('soft light surfaces and readable text', async () => {
    await visit('single-job');
    if (await page.evaluate(() => document.documentElement.classList.contains('dark-mode'))) await page.locator('.theme-toggle-btn').click();
    await page.waitForFunction(() => getComputedStyle(document.querySelector('.n-input')).backgroundColor === 'rgb(245, 244, 239)');
    const colors = await page.evaluate(() => ({
      canvas: getComputedStyle(document.documentElement).backgroundColor,
      input: getComputedStyle(document.querySelector('.n-input')).backgroundColor,
      text: getComputedStyle(document.documentElement).getPropertyValue('--text-muted').trim(),
    }));
    assert.equal(colors.canvas, 'rgb(238, 237, 232)');
    assert.equal(colors.input, 'rgb(245, 244, 239)');
    const luminance = rgb => rgb.map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4).reduce((s, v, i) => s + v * [.2126, .7152, .0722][i], 0);
    const muted = colors.text.match(/[a-f\d]{2}/gi).map(v => parseInt(v, 16));
    assert((luminance([229, 228, 222]) + .05) / (luminance(muted) + .05) >= 4.5, 'Muted text must remain readable on the darkest light surface');
  });
  await check('reduced motion', async () => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await visit('single-job');
    const duration = await page.locator('.nav-link').first().evaluate(e => getComputedStyle(e).transitionDuration);
    assert(duration.split(',').every(value => parseFloat(value) <= 0.001), duration);
  });
  await check('proofreading-memory: real isolated upload, review, occurrence and frozen context', async () => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await visit('proofreading-memory');
    if (await page.evaluate(() => document.documentElement.classList.contains('dark-mode'))) await page.locator('.theme-toggle-btn').click();
    const testBook = `browser-book-${Date.now().toString(36)}`;
    await page.getByRole('textbox', { name: '书籍标识', exact: true }).fill(testBook);
    await page.getByRole('textbox', { name: '讲者标识', exact: true }).fill('browser-speaker');
    await page.locator('.workspace-primary .n-base-selection').nth(1).click();
    await page.getByText('构造实验（synthetic）', { exact: true }).click();
    const texts = { asr: '🙂开场。\n劳动有价值。\n', system: '🙂开场。\n劳动有假值。\n', human: '🙂开场。\n劳动有价值。\n' };
    for (const [role, text] of Object.entries(texts)) {
      await page.locator(`#memory-${role}`).setInputFiles({ name: `${role}.txt`, mimeType: 'text/plain', buffer: Buffer.from(text) });
      await page.getByText(`已上传：${role}.txt`, { exact: true }).waitFor();
    }
    async function prepareBrowserExperiment() {
      const response = page.waitForResponse(r => r.url().endsWith('/proofreading-memory/experiments') && r.request().method() === 'POST');
      await page.getByRole('button', { name: '准备分析请求（不联网）', exact: true }).click();
      const actual = await response; assert.equal(actual.status(), 201);
      const result = await actual.json();
      await page.getByRole('button', { name: '导出冻结上下文', exact: true }).waitFor();
      return result;
    }
    function proposal(experiment, reason = '构造修改，仅验证工程行为。') {
      const request = experiment.request;
      const evidence = [['system', '假值'], ['human', '价值']].map(([role, excerpt]) => {
        // Match Python's code-point offsets, including the leading non-BMP emoji.
        const start = Array.from(request.sources[role].text.slice(0, request.sources[role].text.indexOf(excerpt))).length;
        return { source_id: role, start, end: start + Array.from(excerpt).length, excerpt, pdf_page: null };
      });
      return { schema_version: 1, request_id: request.request_id, candidates: [{
        kind: 'correction_case', scope: request.metadata.scope, text: '仅在保真证据支持时复核假值为价值。',
        reason, retrieval_keys: ['假值', '价值'], observed_form: '假值', preferred_form: '价值', fragment_ids: ['f0001'], evidence,
      }] };
    }
    async function importBrowserResponse(experiment, reason) {
      const details = page.locator('details').filter({ has: page.getByText('离线：导入响应 JSON', { exact: true }) });
      if (!await details.evaluate(el => el.open)) await details.locator('summary').click();
      await page.getByRole('textbox', { name: '响应 JSON 内容', exact: true }).fill(JSON.stringify(proposal(experiment, reason)));
      const pending = page.waitForResponse(r => r.url().endsWith('/import-response') && r.request().method() === 'POST');
      await page.getByRole('button', { name: '导入为待审核候选', exact: true }).click();
      const actual = await pending; assert.equal(actual.status(), 200);
      await page.locator('.memory-entry').first().waitFor();
      return actual.json();
    }
    async function freezeBrowserContext() {
      const pending = page.waitForResponse(r => r.url().endsWith('/contexts') && r.request().method() === 'POST');
      await page.getByRole('button', { name: '导出冻结上下文', exact: true }).click();
      const actual = await pending; assert.equal(actual.status(), 201);
      await page.getByText('下载冻结 JSON', { exact: true }).waitFor();
      return actual.json();
    }
    const first = await prepareBrowserExperiment();
    assert.equal(first.request.metadata.dataset_kind, 'synthetic');
    assert(await page.getByRole('button', { name: '联网分析并提出候选', exact: true }).isDisabled());
    await page.getByText('查看修改片段', { exact: true }).click();
    const shown = await page.locator('.memory-fragment pre').allTextContents();
    const expected = first.request.fragments.flatMap(f => ['system', 'human'].map(role => Array.from(first.request.sources[role].text).slice(...f[role]).join('')));
    assert.deepEqual(shown, expected, 'Unicode excerpts must match Python offsets');
    await page.getByRole('textbox', { name: '书籍标识', exact: true }).fill('unsaved-book');
    await importBrowserResponse(first);
    const empty = await freezeBrowserContext(); assert.equal(empty.context.selected.length, 0);
    assert.equal(empty.context.task_scope.book_id, testBook, 'Unsaved form cannot change the active scope');
    await page.getByRole('textbox', { name: '审核人', exact: true }).fill('browser-test-reviewer');
    await page.getByRole('textbox', { name: '审核理由', exact: true }).fill('synthetic工程测试，不是语义质量确认。');
    await page.getByRole('button', { name: '批准此版本', exact: true }).click();
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await page.getByRole('button', { name: '批准此版本', exact: true }).click();
    await page.getByRole('button', { name: '确认批准', exact: true }).click();
    await page.getByRole('button', { name: '停用此记忆', exact: true }).waitFor();
    const approved = await freezeBrowserContext(); assert.equal(approved.context.selected.length, 1);
    const frozenUrl = `${base}/api/proofreading-memory/experiments/${first.id}/contexts/${approved.context_id}/json`;
    const beforeBytes = await fetch(frozenUrl).then(r => r.text());
    const downloadEvent = page.waitForEvent('download');
    await page.getByText('下载冻结 JSON', { exact: true }).click();
    await (await downloadEvent).saveAs(`${output}/memory-approved-context.json`);
    await page.getByRole('textbox', { name: '书籍标识', exact: true }).fill(testBook);
    await page.locator('#memory-human').setInputFiles({ name: 'human-second.txt', mimeType: 'text/plain', buffer: Buffer.from(texts.human + '追加的实验说明。\n') });
    await page.getByText('已上传：human-second.txt', { exact: true }).waitFor();
    const second = await prepareBrowserExperiment();
    const imported = await importBrowserResponse(second, '第二请求未审核佐证，不是独立来源确认。');
    assert.equal(imported.inserted.length, 0); assert.equal(imported.duplicates.length, 1); assert.equal(imported.occurrence_inserted.length, 1);
    await page.getByText('2 条未独立审核的出现记录', { exact: true }).waitFor();
    const after = await freezeBrowserContext(); assert.deepEqual(after.context, approved.context);
    await page.getByText('查看当前版本证据与审核来源', { exact: true }).click();
    await page.getByText('查看未独立审核的提案出现记录（非置信度）', { exact: true }).click();
    await page.getByText('预览冻结模型数据块', { exact: true }).click();
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      if (width === 390) {
        await page.locator('.workspace-primary .n-base-selection').nth(1).click();
        const popup = page.locator('.n-base-select-menu:visible');
        await popup.waitFor();
        const bounds = await popup.boundingBox();
        assert(bounds && bounds.x >= -1 && bounds.x + bounds.width <= 391, `Memory selection popup clipped: ${JSON.stringify(bounds)}`);
        await page.keyboard.press('Escape');
      }
      await overflow();
      await page.evaluate(() => scrollTo(0, 0));
      await page.screenshot({ path: `${output}/memory-approved-expanded-${width}.png`, fullPage: true });
    }
    await page.locator('.theme-toggle-btn').click(); await overflow();
    await page.screenshot({ path: `${output}/memory-approved-expanded-390-dark.png`, fullPage: true });
    await page.getByRole('button', { name: '停用此记忆', exact: true }).click();
    await page.getByRole('button', { name: '确认停用', exact: true }).click();
    await page.getByText('已停用 · v3', { exact: true }).waitFor();
    assert.equal((await freezeBrowserContext()).context.selected.length, 0);
    assert.equal(await fetch(frozenUrl).then(r => r.text()), beforeBytes, 'Old frozen download must not drift');
    await page.getByRole('textbox', { name: '响应 JSON 内容', exact: true }).fill('{bad-json');
    await page.getByRole('button', { name: '导入为待审核候选', exact: true }).click();
    await page.getByText('invalid JSON', { exact: true }).waitFor();
    assert.equal(await page.locator('.memory-entry').count(), 1);
    await page.getByRole('checkbox', { name: '我同意将上述文本发送至已配置的模型后端' }).check();
    const denied = page.waitForResponse(r => r.url().endsWith('/analyses') && r.request().method() === 'POST');
    await page.getByRole('button', { name: '联网分析并提出候选', exact: true }).click();
    assert.equal((await denied).status(), 403, 'Fixture must prohibit real model execution');
    await page.reload();
    await page.getByText('已停用 · v3', { exact: true }).waitFor();
    await page.getByText('2 条未独立审核的出现记录', { exact: true }).waitFor();
    await overflow();
    await writeFile(`${output}/memory-flow.json`, JSON.stringify({ dataset_kind: 'synthetic', response_mode: 'offline/replay', first: first.id, second: second.id, frozen: approved.context_id, originalFrozenBytesUnchanged: true, remoteModelCalls: 0 }, null, 2));
  });
  assert.deepEqual(errors, [], 'Browser runtime errors');
  await writeFile(`${output}/results.json`, JSON.stringify({ passed: results, runtimeErrors: errors }, null, 2));
} finally {
  await context.close();
  await browser.close();
}
