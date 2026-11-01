// Browser regression against an explicitly isolated fixture: never a production service.
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const base = process.env.LIBRARY_TEST_URL;
const output = process.env.LIBRARY_TEST_OUTPUT;
assert(base && output, 'Set LIBRARY_TEST_URL and LIBRARY_TEST_OUTPUT for the isolated library fixture');
const marker = await fetch(`${base}/api/__library_test__`).then(r => r.json());
assert(marker.isolated && marker.model_execution === false && marker.pdf_a && marker.pdf_b, 'Refusing to test an unprotected service');
await mkdir(output, { recursive: true });
const browser = process.env.CDP_URL ? await chromium.connectOverCDP(process.env.CDP_URL) : await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(12000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));
if (process.env.LIBRARY_MOBILE_STICKY_ONLY === '1') {
  try {
    const pdf = Buffer.concat([await readFile(marker.pdf_a), Buffer.from(`\n% mobile sticky ${Date.now()}\n`)]);
    const uploaded = await fetch(`${base}/api/library/books?filename=mobile-sticky.pdf`, { method: 'POST', headers: { 'Content-Type': 'application/pdf' }, body: pdf });
    assert.equal(uploaded.status, 201);
    const book = await uploaded.json();
    const text = ['正文开始：这是一段足够长的文字。', ...Array.from({ length: 100 }, (_, i) => `第 ${i + 1} 行内容，用来检验手机页面滚动时的选页操作与完整阅读。`), '正文结束：最后一行。'].join('\n');
    const prior = await fetch(`${base}/api/library/books/${book.id}/pages/1`).then(r => r.json());
    const created = await fetch(`${base}/api/library/books/${book.id}/pages/1/text`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, base_revision_id: prior.current_revision_id }) });
    assert.equal(created.status, 200);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${base}/library`);
    await page.getByRole('button', { name: book.title, exact: true }).click();
    await page.getByRole('button', { name: '文字', exact: true }).click();
    const textarea = page.getByPlaceholder('输入或修改本页文字');
    await textarea.waitFor();
    assert.equal(await textarea.inputValue(), text);

    await page.evaluate(() => window.scrollTo(0, 1400));
    await page.waitForTimeout(250);
    const middle = await page.evaluate(() => {
      const controls = document.querySelector('.page-controls');
      const nodes = [...controls.querySelectorAll('button, input')];
      return {
        scroll: window.scrollY,
        viewport: { width: window.innerWidth, height: window.innerHeight, scrollWidth: document.documentElement.scrollWidth },
        sticky: getComputedStyle(controls).position,
        controls: controls.getBoundingClientRect().toJSON(),
        nodes: nodes.map(node => ({ name: node.getAttribute('aria-label') || node.textContent.trim() || '页码', rect: node.getBoundingClientRect().toJSON() })),
      };
    });
    assert(middle.scroll > 500 && middle.sticky === 'sticky' && Math.abs(middle.controls.top) <= 1, JSON.stringify(middle));
    assert(middle.viewport.scrollWidth <= middle.viewport.width + 1, JSON.stringify(middle.viewport));
    assert(middle.nodes.some(node => node.name === '起始 PDF 页码') && middle.nodes.some(node => node.name === '结束 PDF 页码'));
    assert(middle.nodes.some(node => node.name === '加入页段') && middle.nodes.some(node => node.name === '跳转'));
    for (const node of middle.nodes) {
      const r = node.rect, c = middle.controls;
      assert(r.top >= c.top - 1 && r.bottom <= c.bottom + 1 && r.left >= -1 && r.right <= middle.viewport.width + 1, JSON.stringify(node));
    }
    for (let i = 0; i < middle.nodes.length; i++) for (let j = i + 1; j < middle.nodes.length; j++) {
      const a = middle.nodes[i].rect, b = middle.nodes[j].rect;
      assert(!(a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top), `Controls overlap: ${middle.nodes[i].name}, ${middle.nodes[j].name}`);
    }
    await page.screenshot({ path: `${output}/book-mobile-sticky.png` });
    await page.evaluate(() => {
      const element = document.querySelector('textarea[placeholder="输入或修改本页文字"]');
      window.scrollTo(0, window.scrollY + element.getBoundingClientRect().bottom - window.innerHeight + 80);
    });
    const bottom = await page.evaluate(() => ({
      textarea: document.querySelector('textarea[placeholder="输入或修改本页文字"]').getBoundingClientRect().toJSON(),
      controls: document.querySelector('.page-controls').getBoundingClientRect().toJSON(),
      height: window.innerHeight,
    }));
    assert(bottom.textarea.bottom <= bottom.height && bottom.textarea.bottom > bottom.controls.bottom, JSON.stringify(bottom));
    assert((await textarea.inputValue()).endsWith('正文结束：最后一行。'));

    let release;
    const hold = new Promise(resolve => { release = resolve; });
    let captured;
    const putSent = new Promise(resolve => { captured = resolve; });
    await page.route('**/api/library/books/*/pages/*/text', async route => {
      const response = await route.fetch(); captured(); await hold; await route.fulfill({ response });
    });
    const revised = text + '\n用户新增的末尾。';
    await textarea.fill(revised);
    await page.getByRole('button', { name: '保存修订' }).click();
    await putSent;
    assert(await page.getByRole('button', { name: '下一页' }).isDisabled());
    assert(await page.getByRole('button', { name: '跳转' }).isDisabled());
    assert(await page.locator('.page-toolbar input').isDisabled());
    assert(await textarea.isDisabled());
    assert(await page.getByPlaceholder('修订说明（可选）').isDisabled());
    assert(await page.getByRole('button', { name: '刷新文字' }).isDisabled());
    release();
    await page.getByRole('button', { name: '下一页' }).waitFor({ state: 'visible' });
    await page.waitForFunction(() => !document.querySelector('.page-toolbar button:last-child').disabled);
    assert.equal(await textarea.inputValue(), revised);
    const persisted = await fetch(`${base}/api/library/books/${book.id}/pages/1`).then(r => r.json());
    assert.equal(persisted.text, revised);
    await page.getByRole('button', { name: '下一页' }).click();
    await page.getByRole('heading', { name: '第 2 页文字' }).waitFor();
    assert.equal(await textarea.inputValue(), '');
    await page.getByRole('button', { name: '上一页' }).click();
    await page.getByRole('heading', { name: '第 1 页文字' }).waitFor();
    const latest = await fetch(`${base}/api/library/books/${book.id}/pages/1`).then(r => r.json());
    const other = await fetch(`${base}/api/library/books/${book.id}/pages/1/text`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: '另一位成员的修订', base_revision_id: latest.current_revision_id }) });
    assert.equal(other.status, 200);
    await textarea.fill(revised + '\n再次修改。');
    await page.getByRole('button', { name: '保存修订' }).click();
    const adopt = page.getByRole('button', { name: '以最新版本为基准继续编辑' });
    await adopt.waitFor();
    await page.unroute('**/api/library/books/*/pages/*/text');
    let releaseConflict;
    const holdConflict = new Promise(resolve => { releaseConflict = resolve; });
    let capturedConflict;
    const conflictSent = new Promise(resolve => { capturedConflict = resolve; });
    await page.route('**/api/library/books/*/pages/*/text', async route => {
      const response = await route.fetch(); capturedConflict(); await holdConflict; await route.fulfill({ response });
    });
    await page.getByRole('button', { name: '保存修订' }).click();
    await conflictSent;
    assert(await adopt.isDisabled());
    releaseConflict();
    await page.waitForFunction(() => !document.querySelector('.text-pane .n-alert button').disabled);
    assert.deepEqual(errors, []);
    await writeFile(`${output}/mobile-sticky-report.json`, JSON.stringify({ result: 'passed', viewport: '390x844', checks: ['sticky-page-controls', 'visible-range', 'no-control-overlap', 'no-horizontal-overflow', 'complete-text-scroll', 'save-navigation-lock', 'save-input-lock', 'save-conflict-base-lock', 'page-identity'] }, null, 2) + '\n');
    console.log('PASS library mobile sticky controls and save page identity');
  } finally { await context.close(); await browser.close(); }
  process.exit(0);
}
if (process.env.LIBRARY_CLEANUP_ONLY === '1') {
  let bookRemoved = false, materialRemoved = false, materialDeleting = false;
  const book = { id: 'cleanup-book', title: '清理中的书', author: '', edition: '', tags: [], page_count: 1, ready_pages: 0, recognized_pages: 0, edited_pages: 0, failed_pages: 0, version: 1, archived: true, updated_at: '', state: 'deleting', cleanup_error: '模拟磁盘故障' };
  const material = { id: 'cleanup-material', name: '清理中的材料', version: 1, ranges: [], page_count: 0, ready_pages: 0, missing_pages: 0, missing_page_items: [], duplicate_pages: [], archived: false, state: 'ready', cleanup_error: '' };
  await page.route('**/api/library/**', async route => {
    const url = new URL(route.request().url()), method = route.request().method();
    if (url.pathname === '/api/library/books' && method === 'GET') return route.fulfill({ json: { items: bookRemoved ? [] : [book], total: bookRemoved ? 0 : 1 } });
    if (url.pathname === '/api/library/materials' && method === 'GET') return route.fulfill({ json: { items: materialRemoved ? [] : [{ ...material, state: materialDeleting ? 'deleting' : 'ready', cleanup_error: materialDeleting ? '模拟材料清理中断' : '' }], total: materialRemoved ? 0 : 1 } });
    if (url.pathname === '/api/library/materials/cleanup-material' && method === 'GET') return route.fulfill({ json: material });
    if (url.pathname === '/api/library/materials/cleanup-material/revisions' && method === 'GET') return route.fulfill({ json: { items: [], total: 0 } });
    if (url.pathname === '/api/library/books/cleanup-book' && method === 'DELETE' && url.searchParams.get('purge') === 'true') { bookRemoved = true; return route.fulfill({ json: { success: true } }); }
    if (url.pathname === '/api/library/materials/cleanup-material' && method === 'DELETE' && url.searchParams.get('purge') === 'true') { materialRemoved = true; return route.fulfill({ json: { success: true } }); }
    return route.continue();
  });
  try {
    await page.goto(`${base}/library`);
    await page.getByText('清理未完成：模拟磁盘故障').waitFor();
    assert.equal(await page.getByRole('button', { name: '清理中的书', exact: true }).count(), 0);
    const row = page.locator('.book-row');
    assert.equal(await row.getByRole('button', { name: '查看', exact: true }).count(), 0);
    assert.equal(await row.getByRole('button', { name: /归档|恢复/ }).count(), 0);
    await row.getByRole('button', { name: '重试永久清理' }).click();
    await page.getByRole('button', { name: '继续', exact: true }).click();
    await page.getByRole('button', { name: '确认清理', exact: true }).click();
    await page.getByText('清理未完成：模拟磁盘故障').waitFor({ state: 'hidden' });
    assert(bookRemoved);
    await page.getByRole('button', { name: /本次材料/ }).click();
    await page.getByRole('button', { name: '清理中的材料', exact: true }).click();
    await page.getByPlaceholder('例如：第十二次读书会').waitFor();
    materialDeleting = true;
    await page.getByText('查看归档', { exact: true }).click();
    await page.getByText('清理未完成：模拟材料清理中断').waitFor();
    await page.getByText('材料清理未完成', { exact: true }).waitFor();
    assert.equal(await page.getByPlaceholder('例如：第十二次读书会').count(), 0);
    assert.equal(await page.getByRole('button', { name: '清理中的材料', exact: true }).count(), 0);
    await page.getByRole('button', { name: '重试永久清理' }).click();
    await page.getByRole('button', { name: '继续', exact: true }).click();
    await page.getByRole('button', { name: '确认清理', exact: true }).click();
    await page.getByText('清理未完成：模拟材料清理中断').waitFor({ state: 'hidden' });
    assert(materialRemoved); assert.deepEqual(errors, []);
    await writeFile(`${output}/cleanup-report.json`, JSON.stringify({ result: 'passed', checks: ['deleting-book', 'two-confirmations', 'deleting-selected-material', 'retry-cleanup'] }, null, 2) + '\n');
    console.log('PASS library cleanup: failed rows, protected actions, retry and selected material');
  } finally { await context.close(); await browser.close(); }
  process.exit(0);
}
async function noOverflow() {
  const dim = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
  assert(dim.scroll <= dim.width + 1, `Horizontal overflow: ${JSON.stringify(dim)}`);
}
try {
  await page.goto(`${base}/library`);
  await page.getByRole('heading', { name: 'PDF 书库', level: 2 }).waitFor();
  await page.getByRole('button', { name: '清理预览缓存' }).click();
  await page.getByText('仅删除可重新生成的页面预览，不会删除 PDF 原件或逐页文字。').waitFor();
  await page.getByRole('button', { name: '清理缓存' }).click();
  await page.locator('input[type=file]').setInputFiles([marker.pdf_a, marker.pdf_b]);
  await page.getByRole('button', { name: 'A', exact: true }).waitFor();
  await page.getByRole('button', { name: 'B', exact: true }).waitFor();
  await page.getByRole('button', { name: 'A', exact: true }).click();
  await page.getByText('尚无文字', { exact: true }).waitFor();
  // The fixture rejects every actual OCR invocation; intercept one explicit action to inspect its request.
  let ocrRequest;
  await page.route('**/api/library/books/*/ocr-jobs', async route => {
    ocrRequest = route.request().postDataJSON();
    await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ id: 'mock-job', status: 'pending' }) });
  });
  await page.getByRole('button', { name: '识别本页' }).click();
  await page.getByText(/需要识别 1 页/).waitFor();
  await page.getByRole('button', { name: '启动识别' }).click();
  await page.waitForTimeout(100);
  assert.deepEqual(ocrRequest.pages, [1]);
  assert.equal(ocrRequest.model, 'fixture-model');
  assert.equal(ocrRequest.max_concurrency, 2);

  // Blank pages are revisions, not missing pages.
  await page.getByRole('button', { name: '保存修订' }).click();
  await page.getByText('人工修订', { exact: true }).waitFor();
  const items = await fetch(`${base}/api/library/books?page=1&page_size=20`).then(r => r.json());
  const a = items.items.find(x => x.title === 'A');
  const pageBefore = await fetch(`${base}/api/library/books/${a.id}/pages/1`).then(r => r.json());
  const changed = await fetch(`${base}/api/library/books/${a.id}/pages/1/text`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: '他人的修改', base_revision_id: pageBefore.current_revision_id, note: '' }) });
  assert.equal(changed.status, 200);
  await page.getByPlaceholder('输入或修改本页文字').fill('我的修改');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: /本次材料/ }).click();
  assert.equal(await page.getByPlaceholder('输入或修改本页文字').inputValue(), '我的修改');
  await page.getByRole('button', { name: '保存修订' }).click();
  await page.getByText('此页已被其他人修改', { exact: true }).first().waitFor();
  assert.equal(await page.getByPlaceholder('输入或修改本页文字').inputValue(), '我的修改');
  await page.getByRole('button', { name: '以最新版本为基准继续编辑' }).click();
  await page.getByRole('button', { name: '保存修订' }).click();
  await page.getByRole('button', { name: '加入页段' }).click();
  await page.getByRole('button', { name: '下一页' }).click();
  await page.getByPlaceholder('输入或修改本页文字').fill('A第二页');
  await page.getByRole('button', { name: '保存修订' }).click();
  await page.getByRole('button', { name: '加入页段' }).click();
  await page.getByRole('button', { name: '← 返回书库' }).click();
  await page.getByRole('button', { name: 'B', exact: true }).click();
  await page.getByPlaceholder('输入或修改本页文字').fill('B第一页');
  await page.getByRole('button', { name: '保存修订' }).click();
  await page.getByRole('button', { name: '加入页段' }).click();
  await page.getByRole('button', { name: /本次材料/ }).click();
  await page.getByPlaceholder('例如：第十二次读书会').fill('第十二次读书会');
  await page.getByRole('button', { name: '上移第 3 段' }).click();
  const text = await page.locator('.ranges li').allTextContents();
  assert.match(text[0], /A.*1—1/); assert.match(text[1], /B.*1—1/); assert.match(text[2], /A.*2—2/);
  await page.getByRole('button', { name: '保存材料' }).click();
  await page.getByText(/已准备 3 页 · 缺少 0 页/).waitFor();
  const publish = page.waitForResponse(r => r.url().includes('/revisions') && r.request().method() === 'POST');
  await page.getByRole('button', { name: '生成固定版本' }).click();
  const response = await publish; assert.equal(response.status(), 201);
  const revision = await response.json();
  const sources = await fetch(`${base}/api/library/revisions/${revision.id}/sources`).then(r => r.json());
  assert.deepEqual(sources.pages.map(p => p.book_id), [a.id, items.items.find(x => x.title === 'B').id, a.id]);
  assert.deepEqual(sources.pages.map(p => p.pdf_page), [1, 1, 2]);
  assert.equal(revision.book_count, 2);
  await page.waitForTimeout(2200);
  await page.screenshot({ path: `${output}/materials-desktop.png`, fullPage: true });
  await noOverflow();

  // The fixture's task endpoint captures the request and never executes ASR, OCR or LLM.
  await page.goto(`${base}/single-job`);
  await page.locator('input[type=file]').first().setInputFiles({ name: 'sample.mp4', mimeType: 'video/mp4', buffer: Buffer.from('synthetic') });
  await page.getByText('从书库选择', { exact: true }).click();
  await page.getByRole('button', { name: '返回任务' }).click();
  await page.getByRole('button', { name: '复用已有材料' }).click();
  await page.locator('.library-picker .n-base-selection').nth(0).click();
  await page.getByText('第十二次读书会', { exact: true }).last().click();
  await page.locator('.library-picker .n-base-selection').nth(1).click();
  await page.getByText('第 1 版 · 3 页', { exact: true }).last().click();
  await page.getByText(/2 本书，3 页，文字已准备完成/).waitFor();
  const submitted = page.waitForResponse(r => r.url().endsWith('/api/jobs') && r.request().method() === 'POST');
  await page.getByRole('button', { name: '开始整理' }).click();
  assert.equal((await submitted).status(), 202);
  const jobRequest = await fetch(`${base}/api/__last_job__`).then(r => r.json());
  assert.equal(jobRequest.library_material_revision_id, revision.id);
  assert.equal(jobRequest.reference, null);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}/library`);
  await page.getByRole('button', { name: 'A', exact: true }).click();
  await page.getByRole('button', { name: '文字', exact: true }).click();
  await page.getByPlaceholder('输入或修改本页文字').waitFor();
  await noOverflow();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${output}/book-mobile.png`, fullPage: true });
  assert.deepEqual(errors, []);
  await writeFile(`${output}/report.json`, JSON.stringify({ result: 'passed', ocr: 'mocked', model_execution: false, checks: ['upload', 'preview-cache', 'blank-page', 'edit-conflict', 'unsaved-navigation', 'multi-book-order', 'publish', 'job-source', 'mobile-overflow'] }, null, 2) + '\n');
  console.log('PASS library browser: upload, page conflict, multi-book material, fixed version, job selection, mobile');
} finally {
  await context.close();
  await browser.close();
}
