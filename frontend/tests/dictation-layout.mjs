// Synthetic sessions exercise layout and errors without microphone, GPU or gateway calls.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, writeFile, open } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const root = resolve(import.meta.dirname, '../..');
const output = resolve(root, process.env.DICTATION_TEST_OUTPUT || 'tmp/dictation-layout');
await mkdir(output, { recursive: false });
const base = process.env.DICTATION_TEST_BASE_URL || 'http://127.0.0.1:5174';
const log = await open(`${output}/vite.log`, 'w');
const server = process.env.DICTATION_TEST_BASE_URL ? null : spawn(process.execPath,
  [`${root}/frontend/node_modules/vite/bin/vite.js`, '--host', '127.0.0.1', '--port', '5174', '--strictPort'],
  { cwd: `${root}/frontend`, stdio: ['ignore', log.fd, log.fd] });
const id = 'f'.repeat(32);
const utterance = i => ({ id: String(i), revision: 1, status: 'cleaned', text: `第${i}句：预算1500元，不是15000元。等小王确认后再上线，保留 HTTP API。`, raw_text: '原始识别', preview: '', edited: false, needs_review: false });
let state = { id, status: 'listening', phase: '正在听音', revision: 1, event_sequence: 1, next_sequence: 0, samples: 16000,
  created_at: new Date().toISOString(), mode: 'live', config: { terms: [], cleanup: true, cleanup_model: 'gpt-6-luna' },
  utterances: [...Array.from({ length: 60 }, (_, i) => utterance(i + 1)), { ...utterance(61), raw_text: '', text: '', status: 'partial', preview: '临时识别文字'.repeat(300) }] };
const draft = () => state.document_text ?? state.utterances.filter(u => u.raw_text).map(u => u.text).join('\n');
let browser, documentRequests = 0;
const errors = [], checks = [];
try {
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(base)).ok) break; } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.route('**/api/dictation/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith('/cleanup')) {
      const body = request.postDataJSON();
      if (body.scope === 'document') {
        documentRequests++;
        await new Promise(r => setTimeout(r, 150));
        if (documentRequests === 1) return route.fulfill({ status: 504, json: { detail: '全文整理超过3600秒总期限；原稿未改动，可重试。' } });
        state.document_suggestion = { id: String(documentRequests), original: draft(), text: draft() + '\n整体整理末尾。', metrics: { strategy: 'whole', chunk_count: 1 } };
      } else if (body.scope === 'apply') {
        state.document_text = state.document_suggestion.text;
        delete state.document_suggestion;
      }
      state.revision++;
    }
    if (url.pathname.endsWith('/sessions')) return route.fulfill({ json: [{ ...state, draft_text: draft() }] });
    return route.fulfill({ json: { ...state, draft_text: draft() } });
  });
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(`${base}/dictation?session=${id}`);
  await page.locator('.dictation-utterance').nth(59).waitFor();
  const body = page.getByRole('region', { name: '听写文字', exact: true });
  await page.waitForFunction(() => {
    const e = document.querySelector('.dictation-paper__body');
    return e && e.scrollTop > 0 && e.scrollHeight > e.clientHeight;
  });
  async function geometry(label) {
    const paper = await page.locator('.dictation-paper').boundingBox();
    const controls = await page.locator('.dictation-controls').boundingBox();
    assert(controls.y >= paper.y + paper.height - 1, label);
    assert(controls.y + controls.height <= page.viewportSize().height, `${label}: controls outside viewport`);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    return { paper, controls };
  }
  await page.evaluate(() => document.fonts.ready);
  const before = await geometry('desktop');
  await page.screenshot({ path: `${output}/desktop-long-session.png`, fullPage: true });
  checks.push('60 completed utterances plus huge partial stay inside a fixed-height scroll frame; controls remain visible');
  await body.evaluate(e => { e.scrollTop = 0; });
  await page.getByRole('button', { name: '回到最新' }).waitFor();
  state.utterances.splice(60, 0, utterance(62)); state.revision++;
  await page.locator('.dictation-utterance').nth(60).waitFor();
  assert.equal(await body.evaluate(e => e.scrollTop), 0);
  const after = await geometry('after append');
  assert(Math.abs(before.paper.height - after.paper.height) < .1); assert(Math.abs(before.controls.y - after.controls.y) < 2);
  await page.getByRole('button', { name: '回到最新' }).click();
  assert(await body.evaluate(e => e.scrollHeight - e.clientHeight - e.scrollTop < 3));
  checks.push('new text follows only at bottom; scrolling up is preserved; back-to-latest works without moving controls');
  state.status = 'completed'; state.phase = '已停止，处理完成'; state.utterances.pop(); state.revision++;
  await page.getByRole('button', { name: '整理全文', exact: true }).waitFor();
  await page.getByRole('button', { name: '整理全文', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: '总期限' }).waitFor();
  assert.equal(await page.locator('.dictation-utterance').count(), 61);
  await page.getByRole('button', { name: '整理全文', exact: true }).click();
  await page.getByRole('region', { name: '全文整理差异预览' }).waitFor();
  await page.getByRole('button', { name: '确认应用全文稿' }).click();
  await page.getByRole('button', { name: '编辑全文', exact: true }).waitFor();
  await page.getByRole('button', { name: '整理全文', exact: true }).click();
  await page.getByRole('region', { name: '全文整理差异预览' }).waitFor();
  checks.push('clear timeout error, retry, preview/apply and re-organizing an already applied document remain usable');
  for (const [name, width, height] of [['mobile', 390, 844], ['compact', 540, 720]]) {
    await page.setViewportSize({ width, height });
    await page.goto(`${base}/dictation?compact=1&session=${id}`);
    await page.getByRole('region', { name: '听写文字', exact: true }).waitFor();
    await geometry(name);
    await page.screenshot({ path: `${output}/${name}-long-session.png`, fullPage: true });
  }
  checks.push('390px mobile and 540px compact views keep controls visible without horizontal overflow');
  for (const [name, width, height] of [['desktop', 1280, 900], ['mobile', 390, 844]]) {
    await page.setViewportSize({ width, height });
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await page.locator('.dictation-settings summary').click();
    await page.getByRole('textbox', { name: '新常用术语' }).fill('长'.repeat(81));
    await page.getByRole('button', { name: '保存常用术语', exact: true }).click();
    const alert = page.locator('.n-modal [role="alert"]');
    await alert.waitFor();
    assert.match(await alert.textContent(), /术语最多50项，每项1–80字/);
    assert.equal(await page.getByRole('alert').count(), 1);
    // Check the painted error, not merely DOM presence behind the modal mask.
    assert(await alert.evaluate(el => {
      const r = el.getBoundingClientRect();
      return r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth
        && el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    }), `${name}: settings error must be unobscured and inside the viewport`);
    await page.screenshot({ path: `${output}/${name}-settings-error.png`, fullPage: true });
    await page.getByRole('textbox', { name: '新常用术语' }).fill('WebSocket');
    await page.getByRole('button', { name: '保存常用术语', exact: true }).click();
    await page.getByRole('checkbox', { name: 'WebSocket', exact: true }).waitFor();
    assert.deepEqual(await page.evaluate(() => JSON.parse(localStorage.getItem('dictation:terms'))), ['WebSocket']);
    await page.getByRole('button', { name: '完成', exact: true }).click();
    await page.locator('.n-modal').waitFor({ state: 'hidden' });
    assert.equal(await page.locator('.dictation-page > [role="alert"]').count(), 1);
  }
  checks.push('settings validation errors stay visible above the mask on desktop/mobile; corrected terms save and closing preserves page errors');
  assert.deepEqual(errors, []);
  await writeFile(`${output}/report.json`, JSON.stringify({ checks, errors, synthetic_session: true, real_asr_or_llm: false }, null, 2));
  console.log(checks.join('\n'));
} finally {
  await browser?.close();
  if (server) { server.kill('SIGTERM'); await new Promise(r => server.exitCode !== null ? r() : server.once('exit', r)); }
  await log.close();
}
