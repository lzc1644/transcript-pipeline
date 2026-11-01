// Run only against tests/library_composer_server.py: real copied PDFs and library
// persistence, with model execution blocked by the fixture. No production API.
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const base = process.env.LIBRARY_TEST_URL;
const output = process.env.LIBRARY_TEST_OUTPUT;
assert(base && output, 'Set LIBRARY_TEST_URL and LIBRARY_TEST_OUTPUT for the isolated fixture');
const marker = await fetch(`${base}/api/__library_test__`).then(response => response.json());
assert(marker.isolated && marker.model_execution === false, 'Refusing to test a non-isolated server');
await mkdir(output, { recursive: true });
const browser = process.env.CDP_URL ? await chromium.connectOverCDP(process.env.CDP_URL) : await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(12000);
const errors = [], checks = [], ocrRequests = [], resumeRequests = [];
page.on('pageerror', error => errors.push(error.message));
const books = await fetch(`${base}/api/library/books?page=1&page_size=20`).then(response => response.json());
const realA = books.items.find(item => item.title === '20260801欧洲哲学史');
const realB = books.items.find(item => item.title === 'zbl-259');
assert(realA && realB && marker.pdf_missing && realA.ready_pages === 9 && realB.ready_pages === 1, 'Fixture requires two real books and a synthetic PDF');
// A distinct, isolated book per run lets the recovery scenario be repeated.
const missingTitle = `缺页测试书 ${Date.now()}`;
const missingPdf = Buffer.concat([await readFile(marker.pdf_missing), Buffer.from(`\n% browser fixture ${missingTitle}\n`)]);
const uploaded = await fetch(`${base}/api/library/books?filename=${encodeURIComponent(`${missingTitle}.pdf`)}`, { method: 'POST', headers: { 'Content-Type': 'application/pdf' }, body: missingPdf });
assert.equal(uploaded.status, 201);
const missing = await uploaded.json();
const confirmedBlank = await fetch(`${base}/api/library/books/${missing.id}/pages/1/text`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: '', base_revision_id: null }) });
assert.equal(confirmedBlank.status, 200);
let mockJob = null;
await page.route('**/api/library/books/*/ocr-jobs', async route => {
  ocrRequests.push({ url: route.request().url(), body: route.request().postDataJSON() });
  mockJob = { id: 'fixture-missing-job', kind: 'ocr', book_id: missing.id, status: 'partial',
    total: 2, completed: 1, failed_pages: 1, errors: { 2: '模拟缺页失败' }, error: '', created_at: '' };
  await route.fulfill({ status: 202, json: mockJob });
});
await page.route('**/api/library/jobs', async route => {
  const response = await route.fetch();
  const body = await response.json();
  if (mockJob) body.items.push(mockJob);
  body.total = body.items.length;
  await route.fulfill({ response, json: body });
});
await page.route('**/api/library/jobs/fixture-missing-job/resume', async route => {
  resumeRequests.push(route.request().postDataJSON());
  mockJob = { ...mockJob, status: 'pending', error: '', errors: {} };
  await route.fulfill({ status: 200, json: mockJob });
});
const composer = page.locator('.composer');
const preview = composer.locator('.preview-panel');
const material = composer.locator('.material-panel');
const footer = composer.locator('.composer-footer');
async function selectBook(book) {
  // On desktop books remain visible; on mobile switch to the book tab first.
  const tabs = composer.locator('.mobile-tabs');
  if (await tabs.isVisible()) await tabs.getByRole('button', { name: '选书', exact: true }).click();
  await composer.locator('.book-choice').filter({ hasText: book.title }).click();
  await preview.locator('img:visible').waitFor();
}
async function addRange(start, end) {
  await preview.getByLabel('起始 PDF 页码').fill(String(start));
  await preview.getByLabel('结束 PDF 页码').fill(String(end));
  await preview.getByRole('button', { name: '加入页段' }).click();
}
async function record(name, run) {
  await run(); checks.push(name); console.log(`PASS ${name}`);
}
function rectsOverlap(a, b) { return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top; }

try {
  await record('新建任务直接组装，不离开表单', async () => {
    await page.goto(`${base}/single-job`);
    await page.locator('input[type=file]').first().setInputFiles({ name: 'sample.mp4', mimeType: 'video/mp4', buffer: Buffer.from('fixture') });
    await page.getByText('sample.mp4', { exact: true }).waitFor();
    await page.getByText('从书库选择', { exact: true }).click();
    await composer.waitFor({ state: 'visible' });
    assert.equal(new URL(page.url()).pathname, '/single-job');
    assert.equal(await page.getByPlaceholder('上传后自动生成服务器路径').first().inputValue(), '/fixture/sample.mp4');
    assert.equal(await composer.getByRole('heading', { name: '选择书籍', exact: true }).count(), 1);
    assert.equal(await composer.getByText('历史版本', { exact: true }).count(), 0);
    await selectBook(realA);
    const area = await preview.evaluate(element => {
      const controls = element.querySelector('.preview-controls').getBoundingClientRect();
      const range = element.querySelector('.select-range').getBoundingClientRect();
      const scroll = element.querySelector('.preview-scroll').getBoundingClientRect();
      return { controls: controls.toJSON(), range: range.toJSON(), scroll: scroll.toJSON(), viewport: innerHeight };
    });
    assert(area.range.bottom <= area.scroll.top + 1 && area.range.bottom < area.viewport, JSON.stringify(area));
    await page.screenshot({ path: `${output}/composer-desktop.png` });
  });

  const materialName = `组装浏览器回归 ${Date.now()}`;
  await record('多书、多段、重复与调整顺序', async () => {
    await material.locator('.material-name input').fill(materialName);
    await addRange(1, 2);
    await addRange(4, 4);
    await selectBook(realB); await addRange(1, 1);
    await selectBook(realA); await addRange(1, 1);
    await material.getByRole('button', { name: '上移第 3 段' }).click();
    const rows = await material.locator('.ranges > li').allTextContents();
    assert.match(rows[0], /欧洲哲学史.*1—2/s);
    assert.match(rows[1], /zbl-259.*1—1/s);
    assert.match(rows[2], /欧洲哲学史.*4—4/s);
    assert.match(rows[3], /欧洲哲学史.*1—1/s);
    await material.getByText(/原页被重复选择/).waitFor();
    assert.equal(await page.getByText(/已加入.*PDF 第/).count(), 0, 'Adding ranges must not stack transient messages');
    assert.match(await footer.innerText(), /已选 5 页/);
    await page.locator('.n-message').last().waitFor({ state: 'hidden' });
    await page.waitForTimeout(350);
    await page.screenshot({ path: `${output}/composer-desktop-selected.png` });
    await composer.getByRole('button', { name: '返回任务' }).click();
    await composer.waitFor({ state: 'hidden' });
    assert.equal(await page.getByPlaceholder('上传后自动生成服务器路径').first().inputValue(), '/fixture/sample.mp4');
    await page.getByRole('button', { name: '选书与组合参考材料' }).click();
    assert.equal(await material.locator('.ranges > li').count(), 4);
    assert.equal(await material.locator('.material-name input').inputValue(), materialName);
  });

  let revision;
  await record('完整页面零 OCR，发布并回填固定版本', async () => {
    const published = page.waitForResponse(response => /\/api\/library\/materials\/[^/]+\/revisions$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST');
    await footer.getByRole('button', { name: '用于本次任务' }).click();
    const response = await published;
    assert.equal(response.status(), 201, await response.text());
    revision = await response.json();
    await composer.waitFor({ state: 'hidden' });
    await page.getByText('2 本书，5 页，文字已准备完成', { exact: true }).waitFor();
    assert.equal(ocrRequests.length, 0, 'Ready pages must not call OCR');
    const sources = await fetch(`${base}/api/library/revisions/${revision.id}/sources`).then(response => response.json());
    assert.deepEqual(sources.pages.map(item => [item.book_id, item.pdf_page]), [
      [realA.id, 1], [realA.id, 2], [realB.id, 1], [realA.id, 4], [realA.id, 1],
    ]);
    const txt = await fetch(`${base}/api/library/revisions/${revision.id}/txt`).then(response => response.text());
    assert(txt.includes('〔离线验收人工修订〕') && txt.includes('〔离线验收发布后更新〕'));
    const pdf = await fetch(`${base}/api/library/revisions/${revision.id}/pdf`).then(response => response.arrayBuffer());
    assert.equal(Buffer.from(pdf).subarray(0, 4).toString(), '%PDF');
    await page.screenshot({ path: `${output}/selected-task-desktop.png` });
    const submitted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/jobs' && response.request().method() === 'POST');
    await page.getByRole('button', { name: '开始整理' }).click();
    assert.equal((await submitted).status(), 202);
    const body = await fetch(`${base}/api/__last_job__`).then(response => response.json());
    assert.equal(body.library_material_revision_id, revision.id);
    assert.equal(body.reference, null);
    assert.equal(body.video, '/fixture/sample.mp4');
    assert.equal(ocrRequests.length, 0);
  });

  await record('已有材料作为次级入口，固定版本可复用', async () => {
    await page.getByRole('button', { name: '复用已有材料' }).click();
    await page.locator('.library-picker .n-base-selection').nth(0).click();
    await page.getByText('真实两书接线验收', { exact: true }).last().click();
    await page.locator('.library-picker .n-base-selection').nth(1).click();
    await page.getByText(/第 1 版 · 4 页/).last().click();
    await page.getByText('2 本书，4 页，文字已准备完成', { exact: true }).waitFor();
    await page.getByRole('button', { name: '选书组装新材料' }).click();
    assert.equal(await material.locator('.ranges > li').count(), 0, 'Reused version must not edit a stale local composition');
  });

  let draftId;
  await record('缺页不能发布，重复页只请求一次并需确认', async () => {
    await selectBook(missing); await addRange(1, 3); await addRange(2, 2);
    const before = ocrRequests.length;
    let publishCalls = 0;
    const countPublish = request => {
      if (/\/api\/library\/materials\/[^/]+\/revisions$/.test(new URL(request.url()).pathname) && request.method() === 'POST') publishCalls++;
    };
    page.on('request', countPublish);
    await footer.getByRole('button', { name: '用于本次任务' }).click();
    await page.getByText(/还有 3 页尚未准备/).waitFor();
    assert.match(await footer.innerText(), /已准备 1 页 · 缺少 3 页/);
    assert(await footer.getByRole('button', { name: '用于本次任务' }).isDisabled());
    assert.equal(publishCalls, 0);
    assert.equal(ocrRequests.length, before);
    // A previous window may already be recognizing the same physical page.
    mockJob = { id: 'existing-job', kind: 'ocr', book_id: missing.id, pages: [2], status: 'running',
      total: 1, completed: 0, failed_pages: 0, errors: {}, error: '', created_at: '' };
    await footer.getByRole('button', { name: '刷新状态' }).click();
    await material.getByText(/识别中 · 0\/1 页/).waitFor();
    assert(await footer.getByRole('button', { name: '识别缺页' }).isDisabled());
    await composer.getByRole('button', { name: '返回任务' }).click();
    await page.getByRole('button', { name: '选书组装新材料' }).click();
    await material.getByText(/识别中 · 0\/1 页/).waitFor();
    mockJob = { ...mockJob, status: 'interrupted' };
    await footer.getByRole('button', { name: '刷新状态' }).click();
    await material.getByText(/已中断 · 0\/1 页/).waitFor();
    assert(await material.getByRole('button', { name: '继续 / 重试' }).isVisible());
    mockJob = null;
    await footer.getByRole('button', { name: '刷新状态' }).click();
    await material.getByText(/已中断 · 0\/1 页/).waitFor({ state: 'hidden' });
    assert(await footer.getByRole('button', { name: '识别缺页' }).isEnabled());
    assert.equal(ocrRequests.length, before);
    const all = await fetch(`${base}/api/library/materials`).then(response => response.json());
    const candidate = all.items.find(item => item.ranges.length === 2 && item.ranges[0].book_id === missing.id && item.ranges[0].end_page === 3);
    assert(candidate); draftId = candidate.id;
    await footer.getByRole('button', { name: '识别缺页' }).click();
    await page.getByText(/需要识别 2 个不同原页/).waitFor();
    await page.getByRole('button', { name: '取消', exact: true }).click();
    assert.equal(ocrRequests.length, before);
    await footer.getByRole('button', { name: '识别缺页' }).click();
    await page.getByText(/需要识别 2 个不同原页/).waitFor();
    await page.getByText(/模型：fixture-model/).waitFor();
    await page.getByText(/最大并发：2/).waitFor();
    await page.getByText(/快速模式：关闭/).waitFor();
    await page.getByRole('button', { name: '启动识别', exact: true }).click();
    await material.getByText('模拟缺页失败').waitFor();
    assert.equal(ocrRequests.length, before + 1);
    assert.match(ocrRequests.at(-1).url, new RegExp(`/books/${missing.id}/ocr-jobs$`));
    assert.deepEqual(ocrRequests.at(-1).body.pages.sort((a, b) => a - b), [2, 3]);
    assert.equal(ocrRequests.at(-1).body.model, 'fixture-model');
    assert.equal(ocrRequests.at(-1).body.max_concurrency, 2);
    assert.equal(ocrRequests.at(-1).body.fast_mode, false);
    await material.getByRole('button', { name: '继续 / 重试' }).click();
    await page.getByRole('button', { name: '继续', exact: true }).click();
    await page.waitForTimeout(100);
    assert.equal(resumeRequests.length, 1);
    assert.equal(ocrRequests.length, before + 1, 'Resume must not create a new OCR request');
    page.off('request', countPublish);
  });

  await record('材料冲突保留本地名称和页段', async () => {
    const current = await fetch(`${base}/api/library/materials/${draftId}`).then(response => response.json());
    const changed = await fetch(`${base}/api/library/materials/${draftId}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_version: current.version, name: '另一位成员修改', ranges: current.ranges }),
    });
    assert.equal(changed.status, 200);
    await material.locator('.material-name input').fill('我的材料修改');
    await footer.getByRole('button', { name: '保存草稿' }).click();
    await material.getByText('材料草稿已被其他人修改').waitFor();
    assert.equal(await material.locator('.material-name input').inputValue(), '我的材料修改');
    assert.equal(await material.locator('.ranges > li').count(), 2);
    await material.getByRole('button', { name: '以最新版本为基准继续编辑' }).click();
    const saved = page.waitForResponse(response => new URL(response.url()).pathname === `/api/library/materials/${draftId}` && response.request().method() === 'PATCH');
    await footer.getByRole('button', { name: '保存草稿' }).click();
    assert.equal((await saved).status(), 200);
    const stored = await fetch(`${base}/api/library/materials/${draftId}`).then(response => response.json());
    assert.equal(stored.name, '我的材料修改');
    assert.deepEqual(stored.ranges, current.ranges);
  });

  await record('他处补齐后刷新并使用，不发新的 OCR', async () => {
    // Stop the fake in-flight job so the explicit refresh, rather than a timer,
    // is what observes these external edits.
    mockJob = { ...mockJob, status: 'partial' };
    await footer.getByRole('button', { name: '刷新状态' }).click();
    await material.getByText(/部分完成 · 1\/2 页/).waitFor();
    for (const number of [2, 3]) {
      const response = await fetch(`${base}/api/library/books/${missing.id}/pages/${number}/text`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: `人工补齐第 ${number} 页`, base_revision_id: null }),
      });
      assert.equal(response.status, 200);
    }
    await footer.getByRole('button', { name: '刷新状态' }).click();
    await page.waitForFunction(() => document.querySelector('.composer-footer')?.textContent.includes('缺少 0 页'));
    assert(await footer.getByRole('button', { name: '用于本次任务' }).isEnabled());
    const before = ocrRequests.length;
    const published = page.waitForResponse(response => new URL(response.url()).pathname === `/api/library/materials/${draftId}/revisions` && response.request().method() === 'POST');
    await footer.getByRole('button', { name: '用于本次任务' }).click();
    assert.equal((await published).status(), 201);
    await composer.waitFor({ state: 'hidden' });
    await page.getByText('1 本书，4 页，文字已准备完成', { exact: true }).waitFor();
    assert.equal(ocrRequests.length, before);
  });

  await record('手机页码/选页始终可见，预览独立滚动', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${base}/single-job`);
    await page.getByText('从书库选择', { exact: true }).click();
    await composer.waitFor({ state: 'visible' });
    await selectBook(realA);
    const dimensions = await composer.evaluate(element => {
      const nodes = [...element.querySelectorAll('.preview-controls button, .preview-controls input, .composer-footer button')];
      return { width: innerWidth, height: innerHeight, documentWidth: document.documentElement.scrollWidth,
        nodes: nodes.map(node => ({ name: node.getAttribute('aria-label') || node.textContent.trim(), rect: node.getBoundingClientRect().toJSON() })),
        range: element.querySelector('.select-range').getBoundingClientRect().toJSON(),
        scroll: element.querySelector('.preview-scroll').getBoundingClientRect().toJSON() };
    });
    assert(dimensions.documentWidth <= dimensions.width + 1, JSON.stringify(dimensions));
    assert(dimensions.range.bottom <= dimensions.scroll.top + 1, JSON.stringify(dimensions));
    for (const node of dimensions.nodes) {
      const r = node.rect;
      assert(r.left >= -1 && r.right <= dimensions.width + 1 && r.top >= -1 && r.bottom <= dimensions.height + 1, JSON.stringify(node));
    }
    for (let i = 0; i < dimensions.nodes.length; i++) for (let j = i + 1; j < dimensions.nodes.length; j++) {
      assert(!rectsOverlap(dimensions.nodes[i].rect, dimensions.nodes[j].rect), `Controls overlap: ${JSON.stringify([dimensions.nodes[i], dimensions.nodes[j]])}`);
    }
    const before = await composer.locator('.select-range').boundingBox();
    const previewScroll = composer.locator('.preview-scroll');
    const didScroll = await previewScroll.evaluate(element => {
      element.scrollTop = element.scrollHeight;
      return { top: element.scrollTop, height: element.scrollHeight, client: element.clientHeight };
    });
    assert(didScroll.height > didScroll.client && didScroll.top > 0, JSON.stringify(didScroll));
    const after = await composer.locator('.select-range').boundingBox();
    assert(Math.abs(before.y - after.y) <= 1, JSON.stringify({ before, after }));
    assert.equal(await composer.locator('.mobile-tabs').getByRole('button', { name: '预览选页' }).getAttribute('aria-pressed'), 'true');
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${output}/composer-mobile-preview.png` });
    await addRange(1, 2);
    await composer.locator('.mobile-tabs').getByRole('button', { name: /本次材料/ }).click();
    assert.equal(await material.locator('.ranges > li').count(), 1);
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${output}/composer-mobile-material.png` });
  });

  await record('PDF 统一入口和旧链接查询参数', async () => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(`${base}/pdf-book-ocr?origin=legacy#task`);
    await page.waitForURL('**/pdf/ocr?origin=legacy#task');
    await page.getByRole('link', { name: '整本 OCR', exact: true }).waitFor();
    assert.equal(await page.locator('.workbench-nav a[href="/pdf"]').count(), 1);
    assert.equal(await page.locator('.workbench-nav a[href="/library"]').count(), 0);
    assert.equal(await page.locator('.workbench-nav a[href="/pdf-book-ocr"]').count(), 0);
    await page.goto(`${base}/library?import=1#sources`);
    await page.waitForURL('**/pdf/library?import=1#sources');
    await page.getByRole('heading', { name: '历史 OCR 候选' }).waitFor();
    await page.getByRole('button', { name: '选书组装材料' }).click();
    await composer.waitFor({ state: 'visible' });
    assert(await footer.getByRole('button', { name: '生成材料' }).isVisible());
  });
  assert.deepEqual(errors, []);
  await writeFile(`${output}/composer-report.json`, JSON.stringify({ result: 'passed', isolated: true, model_execution: false,
    real_books: [realA.title, realB.title], selected_revision: revision.id, ocr_test_requests_mocked: ocrRequests,
    resume_test_requests_mocked: resumeRequests, checks }, null, 2) + '\n');
  console.log('PASS composer browser regression');
} catch (error) {
  await page.screenshot({ path: `${output}/composer-failure.png` }).catch(() => {});
  await writeFile(`${output}/composer-report.json`, JSON.stringify({ result: 'failed', checks, error: String(error), pageerrors: errors }, null, 2) + '\n');
  throw error;
} finally {
  await context.close(); await browser.close();
}
